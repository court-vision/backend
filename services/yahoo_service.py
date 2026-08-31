"""
Yahoo Fantasy Basketball API Service.

Handles OAuth 2.0 authentication and Yahoo Fantasy Sports API integration.

Yahoo Fantasy API documentation:
https://developer.yahoo.com/fantasysports/guide/
"""

from datetime import date, datetime, timedelta
from typing import Optional

from schemas.common import ApiStatus, LeagueInfo
from schemas.espn import ValidateLeagueResp
from core.errors import BadRequestError, ProviderAuthError
from services.providers.http import provider_get
from services.yahoo.oauth import YAHOO_AUTH_URL, YAHOO_TOKEN_URL
from services.yahoo.matchups import YahooMatchupService
from services.player_value_service import PlayerValueService


# Yahoo API endpoint
YAHOO_API_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"

class YahooService(YahooMatchupService):
    """Service for Yahoo Fantasy Basketball API integration."""

    @staticmethod
    def _matchup_dates_from_payload(matchup: Optional[dict]) -> Optional[tuple[date, date]]:
        """(week_start, week_end) from a Yahoo matchup object (ISO YYYY-MM-DD), or None."""
        if not isinstance(matchup, dict):
            return None
        try:
            start = date.fromisoformat(str(matchup.get("week_start", ""))[:10])
            end = date.fromisoformat(str(matchup.get("week_end", ""))[:10])
        except ValueError:
            return None
        return (start, end) if start <= end else None

    @staticmethod
    def _value_players(league_info: LeagueInfo, team_id: int | None, player_lookups: list[tuple[str, str]]):
        scoring = PlayerValueService.scoring_for(league_info, team_id)
        return (
            scoring,
            PlayerValueService.value_kind_for(scoring),
            PlayerValueService.avg_points_for(scoring, names=player_lookups),
        )

    @staticmethod
    async def _ensure_valid_token(league_info: LeagueInfo, team_id: int | None = None) -> str:
        """
        Ensure we have a valid access token, refreshing (and persisting) it if needed.

        Args:
            league_info: League info with Yahoo tokens
            team_id: Optional team ID to persist refreshed tokens to database

        Returns:
            Valid access token

        Raises:
            ProviderAuthError("yahoo"): no access token, an expired one with no refresh
                token, or a refresh Yahoo rejected -> 403 PROVIDER_AUTH_EXPIRED
            ProviderError / ProviderTimeout: Yahoo itself was unavailable during the refresh
        """
        if not league_info.yahoo_access_token:
            raise ProviderAuthError("yahoo", "No Yahoo access token — connect your Yahoo account in Manage Teams")

        # Refresh when the token is expired or expires within 5 minutes
        if league_info.yahoo_token_expiry:
            try:
                expiry = datetime.fromisoformat(league_info.yahoo_token_expiry)
            except ValueError:
                raise ProviderAuthError("yahoo", "Stored Yahoo token is unusable — reconnect your Yahoo account")
            if datetime.utcnow() >= expiry - timedelta(minutes=5):
                if not league_info.yahoo_refresh_token:
                    raise ProviderAuthError(
                        "yahoo", "Yahoo session expired and cannot be refreshed — reconnect your Yahoo account"
                    )

                new_tokens = await YahooService.refresh_access_token(league_info.yahoo_refresh_token)
                league_info.yahoo_access_token = new_tokens["access_token"]
                league_info.yahoo_refresh_token = new_tokens["refresh_token"]
                league_info.yahoo_token_expiry = new_tokens["token_expiry"]

                # Persist to database if team_id provided
                if team_id is not None:
                    from services.team_service import TeamService
                    await TeamService.update_yahoo_tokens(
                        team_id,
                        new_tokens["access_token"],
                        new_tokens["refresh_token"],
                        new_tokens["token_expiry"]
                    )

        return league_info.yahoo_access_token

    @staticmethod
    async def check_league(league_info: LeagueInfo, team_id: int | None = None) -> ValidateLeagueResp:
        """
        Validate Yahoo league credentials.

        Args:
            league_info: League info with Yahoo credentials
            team_id: Optional team ID to persist refreshed tokens

        Returns:
            ValidateLeagueResp indicating if credentials are valid

        Raises:
            ProviderAuthError (403 PROVIDER_AUTH_EXPIRED) for a rejected/unrefreshable token,
            ProviderError / ProviderTimeout for a Yahoo outage, BadRequestError without a team key
        """
        access_token = await YahooService._ensure_valid_token(league_info, team_id)

        # Try to fetch the specific team to validate access
        team_key = league_info.yahoo_team_key
        if not team_key:
            raise BadRequestError("LEAGUE_VALIDATION_FAILED", "No Yahoo team key provided")

        endpoint = f"{YAHOO_API_BASE}/team/{team_key}?format=json"
        headers = YahooService._get_headers(access_token)

        data = await provider_get("yahoo", endpoint, headers=headers, expect_key="fantasy_content")

        # Check if team exists in response
        team = data.get("fantasy_content", {}).get("team", {})
        if team:
            return ValidateLeagueResp(
                status=ApiStatus.SUCCESS,
                valid=True,
                message="Yahoo league validated successfully"
            )

        return ValidateLeagueResp(
            status=ApiStatus.NOT_FOUND,
            valid=False,
            error_code="TEAM_NAME_NOT_IN_LEAGUE",
            message="Team not found in Yahoo league"
        )
