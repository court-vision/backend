"""Yahoo roster and free-agent retrieval."""

from core.errors import BadRequestError
from db.base import run_db
from schemas.common import ApiStatus, LeagueInfo
from schemas.espn import PlayerResp, TeamDataResp
from services.player_service import _normalize_name
from services.providers.http import provider_get
from services.yahoo.discovery import YahooDiscoveryService
from utils.yahoo_helpers import normalize_team_abbr, parse_yahoo_player_positions, parse_yahoo_team_key

YAHOO_API_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"


class YahooRosterService(YahooDiscoveryService):
    @classmethod
    async def get_team_data(cls, league_info: LeagueInfo, fa_count: int = 0, team_id: int | None = None) -> TeamDataResp:
        """
        Get roster data from Yahoo API.

        Args:
            league_info: League info with Yahoo credentials
            fa_count: Number of free agents to fetch (unused for roster)
            team_id: Optional team ID to persist refreshed tokens

        Returns:
            TeamDataResp with roster players
        """
        access_token = await cls._ensure_valid_token(league_info, team_id)
        team_key = league_info.yahoo_team_key

        if not team_key:
            raise BadRequestError("LEAGUE_VALIDATION_FAILED", "No Yahoo team key provided")

        # Fetch roster with player stats
        endpoint = f"{YAHOO_API_BASE}/team/{team_key}/roster/players?format=json"
        headers = cls._get_headers(access_token)

        data = await provider_get("yahoo", endpoint, headers=headers, expect_key="fantasy_content")

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
                            "injury_status": status if status else None,
                        })

        # Value players from our stored stats under the league's scoring: fantasy
        # points under its weights, or the category value for H2H-category leagues
        # (rolling window, then last season's baseline). Same window as ESPN.
        player_lookups = [(p["name"], p["team"]) for p in parsed_players]
        scoring, value_kind, name_to_value = await run_db(
            "yahoo.value_roster", cls._value_players, league_info, team_id, player_lookups
        )

        # Build final player list with stats
        players = []
        for p in parsed_players:
            normalized_name = _normalize_name(p["name"])
            valued = name_to_value.get(normalized_name)
            avg_points = valued.value if valued is not None and valued.value is not None else 0.0

            players.append(PlayerResp(
                player_id=p["player_id"],
                name=p["name"],
                avg_points=avg_points,
                team=p["team"],
                valid_positions=p["valid_positions"],
                injured=p["injured"],
                injury_status=p["injury_status"],
                value_kind=value_kind,
                value_source=valued.source if valued is not None else None,
            ))

        return TeamDataResp(
            status=ApiStatus.SUCCESS,
            message="Yahoo roster fetched successfully",
            data=players
        )


    @classmethod
    async def get_free_agents(cls, league_info: LeagueInfo, fa_count: int, team_id: int | None = None) -> TeamDataResp:
        """
        Get available free agents from Yahoo league.

        Args:
            league_info: League info with Yahoo credentials
            fa_count: Number of free agents to fetch
            team_id: Optional team ID to persist refreshed tokens

        Returns:
            TeamDataResp with free agent players
        """
        access_token = await cls._ensure_valid_token(league_info, team_id)
        team_key = league_info.yahoo_team_key

        if not team_key:
            raise BadRequestError("LEAGUE_VALIDATION_FAILED", "No Yahoo team key provided")

        # Extract league key from team key
        parsed = parse_yahoo_team_key(team_key)
        league_key = f"{parsed['game_key']}.l.{parsed['league_id']}"

        # Fetch free agents sorted by percent owned
        endpoint = f"{YAHOO_API_BASE}/league/{league_key}/players;status=FA;sort=OR;count={fa_count}?format=json"
        headers = cls._get_headers(access_token)

        data = await provider_get("yahoo", endpoint, headers=headers, expect_key="fantasy_content")

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
                            "injury_status": status if status else None,
                        })

        # Value players from our stored stats under the league's scoring: fantasy
        # points under its weights, or the category value for H2H-category leagues
        # (rolling window, then last season's baseline). Same window as ESPN.
        player_lookups = [(p["name"], p["team"]) for p in parsed_players]
        scoring, value_kind, name_to_value = await run_db(
            "yahoo.value_free_agents", cls._value_players, league_info, team_id, player_lookups
        )

        # Build final player list with stats
        players = []
        for p in parsed_players:
            normalized_name = _normalize_name(p["name"])
            valued = name_to_value.get(normalized_name)
            avg_points = valued.value if valued is not None and valued.value is not None else 0.0

            players.append(PlayerResp(
                player_id=p["player_id"],
                name=p["name"],
                avg_points=avg_points,
                team=p["team"],
                valid_positions=p["valid_positions"],
                injured=p["injured"],
                injury_status=p["injury_status"],
                value_kind=value_kind,
                value_source=valued.source if valued is not None else None,
            ))

        return TeamDataResp(
            status=ApiStatus.SUCCESS,
            message="Yahoo free agents fetched successfully",
            data=players
        )

