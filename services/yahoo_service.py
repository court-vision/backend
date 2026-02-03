"""
Yahoo Fantasy Basketball API Service.

Handles OAuth 2.0 authentication and Yahoo Fantasy Sports API integration.

Yahoo Fantasy API documentation:
https://developer.yahoo.com/fantasysports/guide/
"""

import base64
import secrets
import requests
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlencode

from schemas.common import ApiStatus, LeagueInfo
from schemas.espn import ValidateLeagueResp, PlayerResp, TeamDataResp
from schemas.matchup import MatchupResp, MatchupData, MatchupTeamResp, MatchupPlayerResp
from core.settings import settings
from utils.yahoo_helpers import (
    normalize_team_abbr,
    parse_yahoo_player_positions,
    extract_yahoo_player_stats,
    parse_yahoo_team_key,
    YAHOO_POSITION_MAP,
)
from services.schedule_service import get_remaining_games
from services.player_service import PlayerService


# Yahoo API endpoints
YAHOO_AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
YAHOO_TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
YAHOO_API_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"

# In-memory state storage for OAuth (in production, use Redis or similar)
_oauth_states: dict[str, dict] = {}


class YahooService:
    """Service for Yahoo Fantasy Basketball API integration."""

    @staticmethod
    def get_auth_url(user_id: str) -> tuple[str, str]:
        """
        Generate Yahoo OAuth authorization URL.

        Args:
            user_id: User ID to associate with this OAuth flow

        Returns:
            Tuple of (auth_url, state_token)
        """
        if not settings.yahoo_client_id:
            raise ValueError("Yahoo OAuth not configured: missing YAHOO_CLIENT_ID")

        state = secrets.token_urlsafe(32)

        # Store state with user info and expiry
        _oauth_states[state] = {
            "user_id": user_id,
            "created_at": datetime.utcnow().isoformat(),
            "expires_at": (datetime.utcnow() + timedelta(minutes=10)).isoformat(),
        }

        params = {
            "client_id": settings.yahoo_client_id,
            "redirect_uri": settings.yahoo_redirect_uri,
            "response_type": "code",
            "scope": "fspt-r",  # Fantasy sports read access
            "state": state,
        }

        auth_url = f"{YAHOO_AUTH_URL}?{urlencode(params)}"
        return auth_url, state

    @staticmethod
    def validate_state(state: str) -> Optional[dict]:
        """
        Validate OAuth state token.

        Args:
            state: State token from callback

        Returns:
            State data if valid, None otherwise
        """
        state_data = _oauth_states.get(state)
        if not state_data:
            return None

        # Check expiry
        expires_at = datetime.fromisoformat(state_data["expires_at"])
        if datetime.utcnow() > expires_at:
            del _oauth_states[state]
            return None

        # Clean up used state
        del _oauth_states[state]
        return state_data

    @staticmethod
    async def exchange_code_for_tokens(code: str) -> dict:
        """
        Exchange authorization code for access and refresh tokens.

        Args:
            code: Authorization code from OAuth callback

        Returns:
            Dict with access_token, refresh_token, expires_in
        """
        if not settings.yahoo_client_id or not settings.yahoo_client_secret:
            raise ValueError("Yahoo OAuth not configured")

        # Yahoo requires Basic auth with client credentials
        credentials = f"{settings.yahoo_client_id}:{settings.yahoo_client_secret.get_secret_value()}"
        auth_header = base64.b64encode(credentials.encode()).decode()

        headers = {
            "Authorization": f"Basic {auth_header}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.yahoo_redirect_uri,
        }

        response = requests.post(YAHOO_TOKEN_URL, headers=headers, data=data)
        response.raise_for_status()

        token_data = response.json()
        return {
            "access_token": token_data.get("access_token"),
            "refresh_token": token_data.get("refresh_token"),
            "expires_in": token_data.get("expires_in", 3600),
            "token_expiry": (
                datetime.utcnow() + timedelta(seconds=token_data.get("expires_in", 3600))
            ).isoformat(),
        }

    @staticmethod
    async def refresh_access_token(refresh_token: str) -> dict:
        """
        Refresh an expired access token.

        Args:
            refresh_token: Refresh token from previous auth

        Returns:
            Dict with new access_token, refresh_token, expires_in
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
            "expires_in": token_data.get("expires_in", 3600),
            "token_expiry": (
                datetime.utcnow() + timedelta(seconds=token_data.get("expires_in", 3600))
            ).isoformat(),
        }

    @staticmethod
    def _get_headers(access_token: str) -> dict:
        """Get headers for Yahoo API requests."""
        return {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }

    @staticmethod
    def _ensure_valid_token(league_info: LeagueInfo) -> str:
        """
        Ensure we have a valid access token, refreshing if needed.

        Args:
            league_info: League info with Yahoo tokens

        Returns:
            Valid access token
        """
        if not league_info.yahoo_access_token:
            raise ValueError("No Yahoo access token available")

        # Check if token is expired or about to expire (within 5 minutes)
        if league_info.yahoo_token_expiry:
            expiry = datetime.fromisoformat(league_info.yahoo_token_expiry)
            if datetime.utcnow() >= expiry - timedelta(minutes=5):
                # Token expired or expiring soon, need to refresh
                if league_info.yahoo_refresh_token:
                    # Note: In a real implementation, you'd update the stored tokens
                    # For now, we'll just use the current token and let it fail if expired
                    pass

        return league_info.yahoo_access_token

    @staticmethod
    def check_league(league_info: LeagueInfo) -> ValidateLeagueResp:
        """
        Validate Yahoo league credentials.

        Args:
            league_info: League info with Yahoo credentials

        Returns:
            ValidateLeagueResp indicating if credentials are valid
        """
        try:
            access_token = YahooService._ensure_valid_token(league_info)

            # Try to fetch the specific team to validate access
            team_key = league_info.yahoo_team_key
            if not team_key:
                return ValidateLeagueResp(
                    status=ApiStatus.ERROR,
                    valid=False,
                    message="No Yahoo team key provided"
                )

            endpoint = f"{YAHOO_API_BASE}/team/{team_key}?format=json"
            headers = YahooService._get_headers(access_token)

            response = requests.get(endpoint, headers=headers)

            if response.status_code == 401:
                return ValidateLeagueResp(
                    status=ApiStatus.AUTHENTICATION_ERROR,
                    valid=False,
                    message="Yahoo authentication expired. Please reconnect your Yahoo account."
                )

            response.raise_for_status()
            data = response.json()

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
                message="Team not found in Yahoo league"
            )

        except requests.exceptions.HTTPError as e:
            return ValidateLeagueResp(
                status=ApiStatus.ERROR,
                valid=False,
                message=f"Yahoo API error: {str(e)}"
            )
        except Exception as e:
            return ValidateLeagueResp(
                status=ApiStatus.ERROR,
                valid=False,
                message=f"Error validating Yahoo league: {str(e)}"
            )

    @staticmethod
    async def get_user_leagues(access_token: str) -> list[dict]:
        """
        Get all fantasy basketball leagues for the authenticated user.

        Args:
            access_token: Valid Yahoo access token

        Returns:
            List of league dicts with league_key, name, teams, etc.
        """
        try:
            # Get user's NBA fantasy games and leagues
            # Game key for NBA changes each year (e.g., 418 for 2023-24, 428 for 2024-25)
            endpoint = f"{YAHOO_API_BASE}/users;use_login=1/games;game_codes=nba/leagues?format=json"
            headers = YahooService._get_headers(access_token)

            response = requests.get(endpoint, headers=headers)
            response.raise_for_status()
            data = response.json()

            leagues = []
            fantasy_content = data.get("fantasy_content", {})
            users = fantasy_content.get("users", {})

            # Navigate Yahoo's nested structure
            if "0" in users:
                user_data = users["0"].get("user", [])
                for item in user_data:
                    if isinstance(item, dict) and "games" in item:
                        games = item["games"]
                        # Handle both dict and list responses from Yahoo API
                        if isinstance(games, dict):
                            games_iter = games.items()
                        elif isinstance(games, list):
                            games_iter = enumerate(games)
                        else:
                            continue
                        for game_key, game_data in games_iter:
                            if game_key == "count":
                                continue
                            if isinstance(game_data, dict) and "game" in game_data:
                                game_info = game_data["game"]
                                # Find leagues in this game
                                for game_item in game_info:
                                    if isinstance(game_item, dict) and "leagues" in game_item:
                                        league_list = game_item["leagues"]
                                        # Handle both dict and list responses from Yahoo API
                                        if isinstance(league_list, dict):
                                            leagues_iter = league_list.items()
                                        elif isinstance(league_list, list):
                                            leagues_iter = enumerate(league_list)
                                        else:
                                            continue
                                        for league_key, league_data in leagues_iter:
                                            if league_key == "count":
                                                continue
                                            if isinstance(league_data, dict) and "league" in league_data:
                                                league_info = league_data["league"]
                                                # Extract league details
                                                league_details = {}
                                                for league_item in league_info:
                                                    if isinstance(league_item, dict):
                                                        league_details.update(league_item)

                                                leagues.append({
                                                    "league_key": league_details.get("league_key", ""),
                                                    "league_id": league_details.get("league_id", ""),
                                                    "name": league_details.get("name", ""),
                                                    "season": league_details.get("season", ""),
                                                    "num_teams": league_details.get("num_teams", 0),
                                                    "scoring_type": league_details.get("scoring_type", ""),
                                                })

            return leagues

        except Exception as e:
            print(f"Error fetching Yahoo leagues: {e}")
            return []

    @staticmethod
    async def get_user_teams(access_token: str, league_key: str) -> list[dict]:
        """
        Get user's teams in a specific league.

        Args:
            access_token: Valid Yahoo access token
            league_key: Yahoo league key

        Returns:
            List of team dicts
        """
        try:
            endpoint = f"{YAHOO_API_BASE}/league/{league_key}/teams?format=json"
            headers = YahooService._get_headers(access_token)

            response = requests.get(endpoint, headers=headers)
            response.raise_for_status()
            data = response.json()

            teams = []
            fantasy_content = data.get("fantasy_content", {})
            league = fantasy_content.get("league", [])

            for item in league:
                if isinstance(item, dict) and "teams" in item:
                    teams_data = item["teams"]
                    # Handle both dict and list responses from Yahoo API
                    if isinstance(teams_data, dict):
                        teams_iter = teams_data.items()
                    elif isinstance(teams_data, list):
                        teams_iter = enumerate(teams_data)
                    else:
                        continue
                    for team_key, team_data in teams_iter:
                        if team_key == "count":
                            continue
                        if isinstance(team_data, dict) and "team" in team_data:
                            team_info = team_data["team"]
                            team_details = {}
                            for team_item in team_info:
                                if isinstance(team_item, dict):
                                    team_details.update(team_item)
                                elif isinstance(team_item, list):
                                    for sub_item in team_item:
                                        if isinstance(sub_item, dict):
                                            team_details.update(sub_item)

                            teams.append({
                                "team_key": team_details.get("team_key", ""),
                                "team_id": team_details.get("team_id", ""),
                                "name": team_details.get("name", ""),
                                "is_owned_by_current_login": team_details.get("is_owned_by_current_login", 0) == 1,
                            })

            return teams

        except Exception as e:
            print(f"Error fetching Yahoo teams: {e}")
            return []

    @staticmethod
    async def get_team_data(league_info: LeagueInfo, fa_count: int = 0) -> TeamDataResp:
        """
        Get roster data from Yahoo API.

        Args:
            league_info: League info with Yahoo credentials
            fa_count: Number of free agents to fetch (unused for roster)

        Returns:
            TeamDataResp with roster players
        """
        try:
            access_token = YahooService._ensure_valid_token(league_info)
            team_key = league_info.yahoo_team_key

            if not team_key:
                return TeamDataResp(
                    status=ApiStatus.ERROR,
                    message="No Yahoo team key provided",
                    data=None
                )

            # Fetch roster with player stats
            endpoint = f"{YAHOO_API_BASE}/team/{team_key}/roster/players?format=json"
            headers = YahooService._get_headers(access_token)

            response = requests.get(endpoint, headers=headers)

            if response.status_code == 401:
                return TeamDataResp(
                    status=ApiStatus.AUTHENTICATION_ERROR,
                    message="Yahoo authentication expired. Please reconnect.",
                    data=None
                )

            response.raise_for_status()
            data = response.json()

            # First pass: collect player info for batch stat lookup
            parsed_players = []
            fantasy_content = data.get("fantasy_content", {})
            team = fantasy_content.get("team", [])

            for item in team:
                if isinstance(item, dict) and "roster" in item:
                    roster = item["roster"]
                    players_data = roster.get("0", {}).get("players", {})

                    # Handle both dict and list responses from Yahoo API
                    if isinstance(players_data, dict):
                        players_iter = players_data.items()
                    elif isinstance(players_data, list):
                        players_iter = enumerate(players_data)
                    else:
                        continue
                    for player_key, player_data in players_iter:
                        if player_key == "count":
                            continue
                        if isinstance(player_data, dict) and "player" in player_data:
                            player_info = player_data["player"]
                            player_details = {}
                            eligible_positions = []

                            for player_item in player_info:
                                if isinstance(player_item, list):
                                    for sub_item in player_item:
                                        if isinstance(sub_item, dict):
                                            if "eligible_positions" in sub_item:
                                                eligible_positions = sub_item["eligible_positions"]
                                            else:
                                                player_details.update(sub_item)
                                elif isinstance(player_item, dict):
                                    player_details.update(player_item)

                            # Parse player data
                            player_id = int(player_details.get("player_id", 0))
                            name = player_details.get("name", {})
                            if isinstance(name, dict):
                                full_name = name.get("full", "Unknown")
                            else:
                                full_name = str(name)

                            team_abbrev = normalize_team_abbr(
                                player_details.get("editorial_team_abbr", "FA").upper()
                            )

                            # Get positions
                            positions = parse_yahoo_player_positions(eligible_positions)
                            pos_to_keep = {"PG", "SG", "SF", "PF", "C", "G", "F"}
                            valid_positions = [p for p in positions if p in pos_to_keep]
                            valid_positions.extend(["UT1", "UT2", "UT3"])

                            # Check injury status
                            status = player_details.get("status", "")
                            injured = status in ("IL", "IL+", "O", "GTD", "DTD")

                            parsed_players.append({
                                "player_id": player_id,
                                "name": full_name,
                                "team": team_abbrev,
                                "valid_positions": valid_positions,
                                "injured": injured,
                            })

            # Batch lookup stats by name from our internal database
            player_lookups = [(p["name"], p["team"]) for p in parsed_players]
            name_to_avg = PlayerService.get_last_n_day_avg_batch_by_name(player_lookups, days=7)

            # Build final player list with stats
            players = []
            for p in parsed_players:
                normalized_name = p["name"].lower().strip()
                avg_points = name_to_avg.get(normalized_name) or 0.0

                players.append(PlayerResp(
                    player_id=p["player_id"],
                    name=p["name"],
                    avg_points=avg_points,
                    team=p["team"],
                    valid_positions=p["valid_positions"],
                    injured=p["injured"],
                ))

            return TeamDataResp(
                status=ApiStatus.SUCCESS,
                message="Yahoo roster fetched successfully",
                data=players
            )

        except requests.exceptions.HTTPError as e:
            return TeamDataResp(
                status=ApiStatus.ERROR,
                message=f"Yahoo API error: {str(e)}",
                data=None
            )
        except Exception as e:
            print(f"Error in Yahoo get_team_data: {e}")
            return TeamDataResp(
                status=ApiStatus.ERROR,
                message=f"Internal server error: {str(e)}",
                data=None
            )

    @staticmethod
    async def get_free_agents(league_info: LeagueInfo, fa_count: int) -> TeamDataResp:
        """
        Get available free agents from Yahoo league.

        Args:
            league_info: League info with Yahoo credentials
            fa_count: Number of free agents to fetch

        Returns:
            TeamDataResp with free agent players
        """
        try:
            access_token = YahooService._ensure_valid_token(league_info)
            team_key = league_info.yahoo_team_key

            if not team_key:
                return TeamDataResp(
                    status=ApiStatus.ERROR,
                    message="No Yahoo team key provided",
                    data=None
                )

            # Extract league key from team key
            parsed = parse_yahoo_team_key(team_key)
            league_key = f"{parsed['game_key']}.l.{parsed['league_id']}"

            # Fetch free agents sorted by percent owned
            endpoint = f"{YAHOO_API_BASE}/league/{league_key}/players;status=FA;sort=OR;count={fa_count}?format=json"
            headers = YahooService._get_headers(access_token)

            response = requests.get(endpoint, headers=headers)

            if response.status_code == 401:
                return TeamDataResp(
                    status=ApiStatus.AUTHENTICATION_ERROR,
                    message="Yahoo authentication expired. Please reconnect.",
                    data=None
                )

            response.raise_for_status()
            data = response.json()

            # First pass: collect player info for batch stat lookup
            parsed_players = []
            fantasy_content = data.get("fantasy_content", {})
            league = fantasy_content.get("league", [])

            for item in league:
                if isinstance(item, dict) and "players" in item:
                    players_data = item["players"]

                    # Handle both dict and list responses from Yahoo API
                    if isinstance(players_data, dict):
                        players_iter = players_data.items()
                    elif isinstance(players_data, list):
                        players_iter = enumerate(players_data)
                    else:
                        continue
                    for player_key, player_data in players_iter:
                        if player_key == "count":
                            continue
                        if isinstance(player_data, dict) and "player" in player_data:
                            player_info = player_data["player"]
                            player_details = {}
                            eligible_positions = []

                            for player_item in player_info:
                                if isinstance(player_item, list):
                                    for sub_item in player_item:
                                        if isinstance(sub_item, dict):
                                            if "eligible_positions" in sub_item:
                                                eligible_positions = sub_item["eligible_positions"]
                                            else:
                                                player_details.update(sub_item)
                                elif isinstance(player_item, dict):
                                    player_details.update(player_item)

                            player_id = int(player_details.get("player_id", 0))
                            name = player_details.get("name", {})
                            if isinstance(name, dict):
                                full_name = name.get("full", "Unknown")
                            else:
                                full_name = str(name)

                            team_abbrev = normalize_team_abbr(
                                player_details.get("editorial_team_abbr", "FA").upper()
                            )

                            positions = parse_yahoo_player_positions(eligible_positions)
                            pos_to_keep = {"PG", "SG", "SF", "PF", "C", "G", "F"}
                            valid_positions = [p for p in positions if p in pos_to_keep]
                            valid_positions.extend(["UT1", "UT2", "UT3"])

                            status = player_details.get("status", "")
                            injured = status in ("IL", "IL+", "O", "GTD", "DTD")

                            parsed_players.append({
                                "player_id": player_id,
                                "name": full_name,
                                "team": team_abbrev,
                                "valid_positions": valid_positions,
                                "injured": injured,
                            })

            # Batch lookup stats by name from our internal database
            player_lookups = [(p["name"], p["team"]) for p in parsed_players]
            name_to_avg = PlayerService.get_last_n_day_avg_batch_by_name(player_lookups, days=7)

            # Build final player list with stats
            players = []
            for p in parsed_players:
                normalized_name = p["name"].lower().strip()
                avg_points = name_to_avg.get(normalized_name) or 0.0

                players.append(PlayerResp(
                    player_id=p["player_id"],
                    name=p["name"],
                    avg_points=avg_points,
                    team=p["team"],
                    valid_positions=p["valid_positions"],
                    injured=p["injured"],
                ))

            return TeamDataResp(
                status=ApiStatus.SUCCESS,
                message="Yahoo free agents fetched successfully",
                data=players
            )

        except requests.exceptions.HTTPError as e:
            return TeamDataResp(
                status=ApiStatus.ERROR,
                message=f"Yahoo API error: {str(e)}",
                data=None
            )
        except Exception as e:
            print(f"Error in Yahoo get_free_agents: {e}")
            return TeamDataResp(
                status=ApiStatus.ERROR,
                message=f"Internal server error: {str(e)}",
                data=None
            )

    @staticmethod
    async def get_matchup_data(league_info: LeagueInfo, avg_window: str = "season") -> MatchupResp:
        """
        Get current matchup data from Yahoo API.

        Args:
            league_info: League info with Yahoo credentials
            avg_window: Averaging window for projections

        Returns:
            MatchupResp with current matchup data
        """
        from services.schedule_service import get_matchup_dates

        try:
            access_token = YahooService._ensure_valid_token(league_info)
            team_key = league_info.yahoo_team_key

            if not team_key:
                return MatchupResp(
                    status=ApiStatus.ERROR,
                    message="No Yahoo team key provided",
                    data=None
                )

            parsed_key = parse_yahoo_team_key(team_key)

            # Fetch current matchup
            endpoint = f"{YAHOO_API_BASE}/team/{team_key}/matchups?format=json"
            headers = YahooService._get_headers(access_token)

            response = requests.get(endpoint, headers=headers)

            if response.status_code == 401:
                return MatchupResp(
                    status=ApiStatus.AUTHENTICATION_ERROR,
                    message="Yahoo authentication expired. Please reconnect.",
                    data=None
                )

            response.raise_for_status()
            data = response.json()

            # Parse Yahoo matchup response to find current matchup and opponent
            fantasy_content = data.get("fantasy_content", {})
            team_data = fantasy_content.get("team", [])

            current_matchup = None
            matchup_week = 1
            our_score = 0.0
            opponent_score = 0.0
            opponent_team_key = None
            opponent_name = "Opponent"

            for item in team_data:
                if isinstance(item, dict) and "matchups" in item:
                    matchups = item["matchups"]
                    # Handle both dict and list responses from Yahoo API
                    if isinstance(matchups, dict):
                        matchups_iter = matchups.items()
                    elif isinstance(matchups, list):
                        matchups_iter = enumerate(matchups)
                    else:
                        continue

                    # Find the current/most recent matchup
                    for matchup_key, matchup_data in matchups_iter:
                        if matchup_key == "count":
                            continue
                        if isinstance(matchup_data, dict) and "matchup" in matchup_data:
                            matchup_info = matchup_data["matchup"]

                            # Get matchup week
                            matchup_week = int(matchup_info.get("week", 1))

                            # Parse teams in matchup
                            teams_in_matchup = matchup_info.get("0", {}).get("teams", {})
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
                                        opponent_team_key = t_team_key
                                        opponent_name = t_name
                                        opponent_score = team_points

                            current_matchup = matchup_info
                            break

            if not current_matchup:
                return MatchupResp(
                    status=ApiStatus.NOT_FOUND,
                    message="No current matchup found",
                    data=None
                )

            # Get matchup dates from schedule service
            matchup_dates = get_matchup_dates(matchup_week)
            matchup_start = matchup_dates[0].isoformat() if matchup_dates else ""
            matchup_end = matchup_dates[1].isoformat() if matchup_dates else ""

            # Fetch our team roster
            our_roster = await YahooService._fetch_roster_for_matchup(
                team_key, access_token, avg_window
            )

            # Fetch opponent roster if we have their team key
            opponent_roster = []
            if opponent_team_key:
                opponent_roster = await YahooService._fetch_roster_for_matchup(
                    opponent_team_key, access_token, avg_window
                )

            # Calculate projected scores
            def calc_projected(roster: list[MatchupPlayerResp], current: float) -> float:
                future_pts = 0.0
                for p in roster:
                    if p.lineup_slot not in ("IR", "IL", "IL+", "BE") and not p.injured:
                        future_pts += p.avg_points * p.games_remaining
                return current + future_pts

            our_projected = calc_projected(our_roster, our_score)
            opponent_projected = calc_projected(opponent_roster, opponent_score)

            # Determine winner
            if our_projected > opponent_projected:
                projected_winner = league_info.team_name
            elif opponent_projected > our_projected:
                projected_winner = opponent_name
            else:
                projected_winner = "Tie"

            matchup_data = MatchupData(
                matchup_period=matchup_week,
                matchup_period_start=matchup_start,
                matchup_period_end=matchup_end,
                your_team=MatchupTeamResp(
                    team_name=league_info.team_name,
                    team_id=int(parsed_key.get("team_id", 0)),
                    current_score=our_score,
                    projected_score=round(our_projected, 2),
                    roster=our_roster
                ),
                opponent_team=MatchupTeamResp(
                    team_name=opponent_name,
                    team_id=0,
                    current_score=opponent_score,
                    projected_score=round(opponent_projected, 2),
                    roster=opponent_roster
                ),
                projected_winner=projected_winner,
                projected_margin=round(abs(our_projected - opponent_projected), 2)
            )

            return MatchupResp(
                status=ApiStatus.SUCCESS,
                message="Yahoo matchup data fetched successfully",
                data=matchup_data
            )

        except requests.exceptions.HTTPError as e:
            return MatchupResp(
                status=ApiStatus.ERROR,
                message=f"Yahoo API error: {str(e)}",
                data=None
            )
        except Exception as e:
            print(f"Error in Yahoo get_matchup_data: {e}")
            import traceback
            traceback.print_exc()
            return MatchupResp(
                status=ApiStatus.ERROR,
                message=f"Internal server error: {str(e)}",
                data=None
            )

    @staticmethod
    async def _fetch_roster_for_matchup(
        team_key: str,
        access_token: str,
        avg_window: str
    ) -> list[MatchupPlayerResp]:
        """
        Fetch a team's roster with stats for matchup display.

        Args:
            team_key: Yahoo team key
            access_token: Valid Yahoo access token
            avg_window: Averaging window for stats

        Returns:
            List of MatchupPlayerResp for the team roster
        """
        try:
            endpoint = f"{YAHOO_API_BASE}/team/{team_key}/roster/players?format=json"
            headers = YahooService._get_headers(access_token)

            response = requests.get(endpoint, headers=headers)
            response.raise_for_status()
            data = response.json()

            # Parse roster
            parsed_players = []
            fantasy_content = data.get("fantasy_content", {})
            team = fantasy_content.get("team", [])

            for item in team:
                if isinstance(item, dict) and "roster" in item:
                    roster = item["roster"]
                    players_data = roster.get("0", {}).get("players", {})

                    if isinstance(players_data, dict):
                        players_iter = players_data.items()
                    elif isinstance(players_data, list):
                        players_iter = enumerate(players_data)
                    else:
                        continue

                    for player_key, player_data in players_iter:
                        if player_key == "count":
                            continue
                        if isinstance(player_data, dict) and "player" in player_data:
                            player_info = player_data["player"]
                            player_details = {}
                            eligible_positions = []
                            selected_position = "UT"

                            for player_item in player_info:
                                if isinstance(player_item, list):
                                    for sub_item in player_item:
                                        if isinstance(sub_item, dict):
                                            if "eligible_positions" in sub_item:
                                                eligible_positions = sub_item["eligible_positions"]
                                            elif "selected_position" in sub_item:
                                                sp = sub_item["selected_position"]
                                                if isinstance(sp, list) and len(sp) > 0:
                                                    selected_position = sp[0].get("position", "UT")
                                                elif isinstance(sp, dict):
                                                    selected_position = sp.get("position", "UT")
                                            else:
                                                player_details.update(sub_item)
                                elif isinstance(player_item, dict):
                                    if "selected_position" in player_item:
                                        sp = player_item["selected_position"]
                                        if isinstance(sp, list) and len(sp) > 0:
                                            selected_position = sp[0].get("position", "UT")
                                        elif isinstance(sp, dict):
                                            selected_position = sp.get("position", "UT")
                                    else:
                                        player_details.update(player_item)

                            player_id = int(player_details.get("player_id", 0))
                            name = player_details.get("name", {})
                            if isinstance(name, dict):
                                full_name = name.get("full", "Unknown")
                            else:
                                full_name = str(name)

                            team_abbrev = normalize_team_abbr(
                                player_details.get("editorial_team_abbr", "FA").upper()
                            )

                            # Get primary position
                            positions = parse_yahoo_player_positions(eligible_positions)
                            primary_pos = positions[0] if positions else "UT"

                            # Normalize lineup slot
                            lineup_slot = YAHOO_POSITION_MAP.get(selected_position, selected_position)

                            status = player_details.get("status", "")
                            injured = status in ("IL", "IL+", "O", "GTD", "DTD")
                            injury_status = status if status else None

                            parsed_players.append({
                                "player_id": player_id,
                                "name": full_name,
                                "team": team_abbrev,
                                "position": primary_pos,
                                "lineup_slot": lineup_slot,
                                "injured": injured,
                                "injury_status": injury_status,
                            })

            # Batch lookup stats by name
            player_lookups = [(p["name"], p["team"]) for p in parsed_players]
            name_to_avg = PlayerService.get_last_n_day_avg_batch_by_name(player_lookups, days=7)

            # Build MatchupPlayerResp list
            roster = []
            for p in parsed_players:
                normalized_name = p["name"].lower().strip()
                avg_points = name_to_avg.get(normalized_name) or 0.0
                games_remaining = get_remaining_games(p["team"])

                roster.append(MatchupPlayerResp(
                    player_id=p["player_id"],
                    name=p["name"],
                    team=p["team"],
                    position=p["position"],
                    lineup_slot=p["lineup_slot"],
                    avg_points=avg_points,
                    projected_points=round(avg_points * games_remaining, 2),
                    games_remaining=games_remaining,
                    injured=p["injured"],
                    injury_status=p["injury_status"],
                ))

            return roster

        except Exception as e:
            print(f"Error fetching roster for matchup: {e}")
            return []
