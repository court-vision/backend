"""
ESPN Extractor

Fetches data from ESPN Fantasy Basketball API.
"""

import json
from typing import Optional, Any

import requests

from core.logging import get_logger
from core.settings import settings
from core.resilience import (
    with_retry,
    espn_api_circuit,
    NetworkError,
    RateLimitError,
    ServerError,
)
from pipelines.extractors.base import BaseExtractor
from pipelines.transformers.names import normalize_name
from utils.espn_helpers import POSITION_MAP, PRO_TEAM_MAP


ESPN_FANTASY_ENDPOINT = (
    "https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/{}/segments/0/leagues/{}"
)


class ESPNExtractor(BaseExtractor):
    """
    Extractor for ESPN Fantasy Basketball API.

    Provides methods to fetch:
    - Player data with ESPN IDs and ownership percentages
    - Matchup data for fantasy teams
    """

    def __init__(self):
        super().__init__("espn")

    def extract(self, **kwargs: Any) -> Any:
        """Not used directly - use specific methods below."""
        raise NotImplementedError("Use get_player_data or get_matchup_data")

    @with_retry(
        max_attempts=settings.retry_max_attempts,
        base_delay=settings.retry_base_delay,
        max_delay=settings.retry_max_delay,
    )
    @espn_api_circuit
    def get_player_data(
        self,
        year: Optional[int] = None,
        league_id: Optional[int] = None,
    ) -> dict[str, dict]:
        """
        Fetch ESPN player data including ESPN ID and roster percentage.

        Args:
            year: ESPN season year (defaults to settings.espn_year)
            league_id: ESPN league ID (defaults to settings.espn_league_id)

        Returns:
            Dict mapping normalized player name to {"espn_id": int, "rost_pct": float}
        """
        year = year or settings.espn_year
        league_id = league_id or settings.espn_league_id

        params = {"view": "kona_player_info", "scoringPeriodId": 0}
        endpoint = ESPN_FANTASY_ENDPOINT.format(year, league_id)
        filters = {
            "players": {
                "filterSlotIds": {"value": []},
                "limit": 750,
                "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
                "sortDraftRanks": {
                    "sortPriority": 2,
                    "sortAsc": True,
                    "value": "STANDARD",
                },
            }
        }
        headers = {"x-fantasy-filter": json.dumps(filters)}

        self.log.debug("request_start", endpoint=endpoint)

        try:
            response = requests.get(
                endpoint,
                params=params,
                headers=headers,
                timeout=settings.http_timeout,
            )

            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 60))
                raise RateLimitError("ESPN rate limited", retry_after=retry_after)

            if response.status_code >= 500:
                raise ServerError("ESPN server error", status_code=response.status_code)

            response.raise_for_status()
            data = response.json()

        except requests.exceptions.Timeout:
            raise NetworkError("ESPN request timed out")
        except requests.exceptions.ConnectionError:
            raise NetworkError("ESPN connection failed")

        players = data.get("players", [])
        players = [x.get("player", x) for x in players]

        cleaned_data = {}
        for player in players:
            if player and "fullName" in player:
                normalized = normalize_name(player["fullName"])
                cleaned_data[normalized] = {
                    "espn_id": player["id"],
                    "rost_pct": player.get("ownership", {}).get("percentOwned", 0),
                }

        self.log.info("request_complete", player_count=len(cleaned_data))
        return cleaned_data

    def get_matchup_data(
        self,
        league_id: int,
        team_name: str,
        espn_s2: str,
        swid: str,
        year: int,
        matchup_period: int,
    ) -> Optional[dict]:
        """
        Fetch matchup data from ESPN API for a specific team.

        Args:
            league_id: ESPN league ID
            team_name: Name of the team to find
            espn_s2: ESPN S2 cookie for authentication
            swid: ESPN SWID cookie for authentication
            year: Season year
            matchup_period: Matchup period number

        Returns:
            Dict with team_name, current_score, opponent_team_name, opponent_current_score
            or None if not found
        """
        params = {"view": ["mTeam", "mMatchup", "mSchedule"]}
        cookies = {"espn_s2": espn_s2, "SWID": swid}
        endpoint = ESPN_FANTASY_ENDPOINT.format(year, league_id)

        try:
            response = requests.get(
                endpoint,
                params=params,
                cookies=cookies,
                timeout=settings.http_timeout,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            self.log.warning("matchup_error", error=str(e), team=team_name)
            return None

        # Find our team
        teams = data.get("teams", [])
        our_team_id = None
        our_team_name = None

        for team in teams:
            if team.get("name", "").strip() == team_name.strip():
                our_team_id = team.get("id")
                our_team_name = team.get("name")
                break

        if not our_team_id:
            self.log.warning("team_not_found", team=team_name)
            return None

        # Find current matchup
        schedule = data.get("schedule", [])
        for matchup in schedule:
            if matchup.get("matchupPeriodId") == matchup_period:
                home_data = matchup.get("home", {})
                away_data = matchup.get("away", {})
                home_id = home_data.get("teamId")
                away_id = away_data.get("teamId")

                if home_id == our_team_id:
                    opponent_id = away_id
                    our_score = home_data.get("totalPoints", 0)
                    opponent_score = away_data.get("totalPoints", 0)
                elif away_id == our_team_id:
                    opponent_id = home_id
                    our_score = away_data.get("totalPoints", 0)
                    opponent_score = home_data.get("totalPoints", 0)
                else:
                    continue

                # Find opponent name
                opponent_name = "Unknown"
                for team in teams:
                    if team.get("id") == opponent_id:
                        opponent_name = team.get("name", "Unknown")
                        break

                return {
                    "team_name": our_team_name,
                    "current_score": our_score,
                    "opponent_team_name": opponent_name,
                    "opponent_current_score": opponent_score,
                }

        return None

    @with_retry(
        max_attempts=settings.retry_max_attempts,
        base_delay=settings.retry_base_delay,
        max_delay=settings.retry_max_delay,
    )
    @espn_api_circuit
    def get_roster_with_slots(
        self,
        league_id: int,
        team_name: str,
        espn_s2: str,
        swid: str,
        year: int,
    ) -> Optional[list[dict]]:
        """
        Fetch a team's roster with lineup slot assignments.

        Returns player data needed for lineup alerts: name, team, lineup_slot,
        injured status, and injury status.

        Args:
            league_id: ESPN league ID
            team_name: Name of the team to find
            espn_s2: ESPN S2 cookie for authentication
            swid: ESPN SWID cookie for authentication
            year: Season year

        Returns:
            List of dicts with keys: name, team, lineup_slot, injured, injury_status
            or None if team not found
        """
        params = {"view": ["mTeam", "mRoster"]}
        cookies = {"espn_s2": espn_s2, "SWID": swid}
        endpoint = ESPN_FANTASY_ENDPOINT.format(year, league_id)

        team_abbrev_corrections = {"PHL": "PHI", "PHO": "PHX"}

        try:
            response = requests.get(
                endpoint,
                params=params,
                cookies=cookies,
                timeout=settings.http_timeout,
            )

            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 60))
                raise RateLimitError("ESPN rate limited", retry_after=retry_after)

            if response.status_code >= 500:
                raise ServerError("ESPN server error", status_code=response.status_code)

            response.raise_for_status()
            data = response.json()

        except requests.exceptions.Timeout:
            raise NetworkError("ESPN request timed out")
        except requests.exceptions.ConnectionError:
            raise NetworkError("ESPN connection failed")

        # Find the team by name
        teams = data.get("teams", [])
        target_team = None
        for team in teams:
            if team.get("name", "").strip() == team_name.strip():
                target_team = team
                break

        if not target_team:
            self.log.warning("team_not_found", team=team_name)
            return None

        # Extract roster with lineup slots
        roster = []
        entries = target_team.get("roster", {}).get("entries", [])

        for entry in entries:
            player_data = entry.get("playerPoolEntry", {}).get("player", {})
            if not player_data:
                player_data = entry.get("player", {})

            name = player_data.get("fullName", "Unknown")

            pro_team_id = player_data.get("proTeamId", 0)
            team_abbrev = PRO_TEAM_MAP.get(pro_team_id, "FA")
            team_abbrev = team_abbrev_corrections.get(team_abbrev, team_abbrev)

            lineup_slot_id = entry.get("lineupSlotId", 0)
            lineup_slot = POSITION_MAP.get(lineup_slot_id, "")

            injured = player_data.get("injured", False)
            injury_status = player_data.get("injuryStatus")

            roster.append({
                "name": name,
                "team": team_abbrev,
                "lineup_slot": lineup_slot,
                "injured": injured,
                "injury_status": injury_status,
            })

        self.log.info("roster_extracted", team=team_name, player_count=len(roster))
        return roster
