"""
Yahoo Extractor

Fetches data from Yahoo Fantasy Basketball API for pipelines.
"""

import base64
from datetime import datetime, timedelta
from typing import Optional, Any

import requests

from core.logging import get_logger
from core.settings import settings
from core.resilience import (
    with_retry,
    NetworkError,
    RateLimitError,
    ServerError,
)
from pipelines.extractors.base import BaseExtractor


YAHOO_TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
YAHOO_API_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"


class YahooExtractor(BaseExtractor):
    """
    Extractor for Yahoo Fantasy Basketball API.

    Provides methods to fetch:
    - Matchup data for fantasy teams (scores only, for pipeline use)

    Handles OAuth token refresh internally.
    """

    def __init__(self):
        super().__init__("yahoo")

    def extract(self, **kwargs: Any) -> Any:
        """Not used directly - use specific methods below."""
        raise NotImplementedError("Use get_matchup_data")

    def _refresh_access_token(self, refresh_token: str) -> dict:
        """
        Refresh an expired Yahoo access token.

        Args:
            refresh_token: Refresh token from previous auth

        Returns:
            Dict with new access_token, refresh_token, token_expiry
        """
        if not settings.yahoo_client_id or not settings.yahoo_client_secret:
            raise ValueError("Yahoo OAuth not configured")

        credentials = f"{settings.yahoo_client_id}:{settings.yahoo_client_secret.get_secret_value()}"
        auth_header = base64.b64encode(credentials.encode()).decode()

        headers = {
            "Authorization": f"Basic {auth_header}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }

        response = requests.post(YAHOO_TOKEN_URL, headers=headers, data=data)
        response.raise_for_status()

        token_data = response.json()
        return {
            "access_token": token_data.get("access_token"),
            "refresh_token": token_data.get("refresh_token", refresh_token),
            "token_expiry": (
                datetime.utcnow() + timedelta(seconds=token_data.get("expires_in", 3600))
            ).isoformat(),
        }

    def _ensure_valid_token(
        self,
        access_token: str,
        refresh_token: Optional[str],
        token_expiry: Optional[str],
    ) -> tuple[str, Optional[dict]]:
        """
        Ensure we have a valid access token, refreshing if needed.

        Args:
            access_token: Current access token
            refresh_token: Refresh token for renewal
            token_expiry: ISO datetime string of token expiry

        Returns:
            Tuple of (valid_access_token, new_tokens_dict_or_none)
            new_tokens_dict is returned if tokens were refreshed (for persistence)
        """
        if not access_token:
            raise ValueError("No Yahoo access token available")

        # Check if token is expired or about to expire (within 5 minutes)
        if token_expiry:
            expiry = datetime.fromisoformat(token_expiry)
            if datetime.utcnow() >= expiry - timedelta(minutes=5):
                # Token expired or expiring soon, need to refresh
                if refresh_token:
                    try:
                        new_tokens = self._refresh_access_token(refresh_token)
                        self.log.debug("token_refreshed")
                        return new_tokens["access_token"], new_tokens
                    except Exception as e:
                        raise ValueError(f"Failed to refresh Yahoo token: {str(e)}")
                else:
                    raise ValueError("Yahoo token expired and no refresh token available")

        return access_token, None

    def _get_headers(self, access_token: str) -> dict:
        """Get headers for Yahoo API requests."""
        return {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }

    def _parse_yahoo_team_key(self, team_key: str) -> dict:
        """
        Parse Yahoo team key into components.

        Format: {game_key}.l.{league_id}.t.{team_id}
        Example: 428.l.12345.t.3
        """
        parts = team_key.split(".")
        if len(parts) >= 5:
            return {
                "game_key": parts[0],
                "league_id": parts[2],
                "team_id": parts[4],
            }
        return {"game_key": "", "league_id": "", "team_id": ""}

    @with_retry(
        max_attempts=settings.retry_max_attempts,
        base_delay=settings.retry_base_delay,
        max_delay=settings.retry_max_delay,
    )
    def get_matchup_data(
        self,
        team_key: str,
        team_name: str,
        access_token: str,
        refresh_token: Optional[str],
        token_expiry: Optional[str],
        matchup_period: int,
    ) -> tuple[Optional[dict], Optional[dict]]:
        """
        Fetch matchup data from Yahoo API for a specific team.

        Args:
            team_key: Yahoo team key (e.g., "428.l.12345.t.3")
            team_name: Name of the team (for response)
            access_token: Yahoo access token
            refresh_token: Yahoo refresh token for renewal
            token_expiry: ISO datetime string of token expiry
            matchup_period: Matchup period (week) number

        Returns:
            Tuple of:
            - Dict with team_name, current_score, opponent_team_name, opponent_current_score
              (or None if not found)
            - Dict with new tokens if refreshed (or None)
        """
        # Ensure valid token
        try:
            valid_token, new_tokens = self._ensure_valid_token(
                access_token, refresh_token, token_expiry
            )
        except ValueError as e:
            self.log.warning("token_error", error=str(e), team=team_name)
            return None, None

        headers = self._get_headers(valid_token)

        # Fetch matchups for the team
        endpoint = f"{YAHOO_API_BASE}/team/{team_key}/matchups?format=json"

        try:
            response = requests.get(
                endpoint,
                headers=headers,
                timeout=settings.http_timeout,
            )

            if response.status_code == 401:
                self.log.warning("auth_expired", team=team_name)
                return None, None

            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 60))
                raise RateLimitError("Yahoo rate limited", retry_after=retry_after)

            if response.status_code >= 500:
                raise ServerError("Yahoo server error", status_code=response.status_code)

            response.raise_for_status()
            data = response.json()

        except requests.exceptions.Timeout:
            raise NetworkError("Yahoo request timed out")
        except requests.exceptions.ConnectionError:
            raise NetworkError("Yahoo connection failed")

        # Parse matchup response to find the current matchup
        fantasy_content = data.get("fantasy_content", {})
        team_data = fantasy_content.get("team", [])

        our_score = 0.0
        opponent_score = 0.0
        opponent_name = "Unknown"
        found_matchup = False
        yahoo_week = matchup_period

        for item in team_data:
            if isinstance(item, dict) and "matchups" in item:
                matchups = item["matchups"]

                # Handle both dict and list responses from Yahoo API
                if isinstance(matchups, dict):
                    matchups_iter = list(matchups.items())
                elif isinstance(matchups, list):
                    matchups_iter = list(enumerate(matchups))
                else:
                    continue

                # Find the current in-progress matchup by status ("midevent"),
                # rather than matching by week number which may differ between
                # Yahoo and the local schedule.
                target_matchup_info = None
                for matchup_key, matchup_val in matchups_iter:
                    if matchup_key == "count":
                        continue
                    if isinstance(matchup_val, dict) and "matchup" in matchup_val:
                        mi = matchup_val["matchup"]
                        status = mi.get("status", "")
                        if status == "midevent":
                            target_matchup_info = mi
                            break

                # Fallback: match by week number if no midevent matchup found
                if not target_matchup_info:
                    for matchup_key, matchup_val in matchups_iter:
                        if matchup_key == "count":
                            continue
                        if isinstance(matchup_val, dict) and "matchup" in matchup_val:
                            mi = matchup_val["matchup"]
                            week = int(mi.get("week", 0))
                            if week == matchup_period:
                                target_matchup_info = mi
                                break

                if target_matchup_info:
                    found_matchup = True
                    yahoo_week = int(target_matchup_info.get("week", matchup_period))

                    # Parse teams in matchup
                    teams_in_matchup = target_matchup_info.get("0", {}).get("teams", {})
                    if isinstance(teams_in_matchup, dict):
                        teams_iter = teams_in_matchup.items()
                    elif isinstance(teams_in_matchup, list):
                        teams_iter = enumerate(teams_in_matchup)
                    else:
                        teams_iter = []

                    for t_key, t_data in teams_iter:
                        if t_key == "count":
                            continue

                        if isinstance(t_data, dict) and "team" in t_data:
                            team_info = t_data["team"]
                            team_details = {}
                            team_points = 0.0

                            # Parse team details from nested structure
                            for t_item in team_info:
                                if isinstance(t_item, list):
                                    for sub in t_item:
                                        if isinstance(sub, dict):
                                            team_details.update(sub)
                                elif isinstance(t_item, dict):
                                    if "team_points" in t_item:
                                        tp = t_item["team_points"]
                                        team_points = float(tp.get("total", 0))
                                    else:
                                        team_details.update(t_item)

                            t_team_key = team_details.get("team_key", "")
                            t_name = team_details.get("name", "Unknown")

                            if t_team_key == team_key:
                                our_score = team_points
                            else:
                                opponent_name = t_name
                                opponent_score = team_points

        if not found_matchup:
            self.log.warning("matchup_not_found", team=team_name, week=matchup_period)
            return None, new_tokens

        return {
            "team_name": team_name,
            "current_score": our_score,
            "opponent_team_name": opponent_name,
            "opponent_current_score": opponent_score,
            "matchup_period": yahoo_week,
        }, new_tokens
