"""Discovery of Yahoo fantasy basketball leagues and teams."""

from services.providers.http import provider_get
from services.yahoo.oauth import YahooOAuthService

YAHOO_API_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"


class YahooDiscoveryService(YahooOAuthService):
    @classmethod
    async def get_user_leagues(cls, access_token: str) -> list[dict]:
        endpoint = f"{YAHOO_API_BASE}/users;use_login=1/games;game_codes=nba/leagues?format=json"
        data = await provider_get(
            "yahoo", endpoint, headers=cls._get_headers(access_token), expect_key="fantasy_content"
        )

        leagues = []
        users = data.get("fantasy_content", {}).get("users", {})
        if "0" not in users:
            return leagues

        for item in users["0"].get("user", []):
            if not isinstance(item, dict) or "games" not in item:
                continue
            games = item["games"]
            games_iter = games.items() if isinstance(games, dict) else enumerate(games) if isinstance(games, list) else []
            for game_key, game_data in games_iter:
                if game_key == "count" or not isinstance(game_data, dict) or "game" not in game_data:
                    continue
                for game_item in game_data["game"]:
                    if not isinstance(game_item, dict) or "leagues" not in game_item:
                        continue
                    league_list = game_item["leagues"]
                    league_iter = (
                        league_list.items()
                        if isinstance(league_list, dict)
                        else enumerate(league_list)
                        if isinstance(league_list, list)
                        else []
                    )
                    for league_key, league_data in league_iter:
                        if league_key == "count" or not isinstance(league_data, dict) or "league" not in league_data:
                            continue
                        details = {}
                        for league_item in league_data["league"]:
                            if isinstance(league_item, dict):
                                details.update(league_item)
                        leagues.append({
                            "league_key": details.get("league_key", ""),
                            "league_id": details.get("league_id", ""),
                            "name": details.get("name", ""),
                            "season": details.get("season", ""),
                            "num_teams": details.get("num_teams", 0),
                            "scoring_type": details.get("scoring_type", ""),
                        })
        return leagues

    @classmethod
    async def get_user_teams(cls, access_token: str, league_key: str) -> list[dict]:
        endpoint = f"{YAHOO_API_BASE}/league/{league_key}/teams?format=json"
        data = await provider_get(
            "yahoo", endpoint, headers=cls._get_headers(access_token), expect_key="fantasy_content"
        )

        teams = []
        for item in data.get("fantasy_content", {}).get("league", []):
            if not isinstance(item, dict) or "teams" not in item:
                continue
            teams_data = item["teams"]
            teams_iter = (
                teams_data.items()
                if isinstance(teams_data, dict)
                else enumerate(teams_data)
                if isinstance(teams_data, list)
                else []
            )
            for team_key, team_data in teams_iter:
                if team_key == "count" or not isinstance(team_data, dict) or "team" not in team_data:
                    continue
                details = {}
                for team_item in team_data["team"]:
                    if isinstance(team_item, dict):
                        details.update(team_item)
                    elif isinstance(team_item, list):
                        for sub_item in team_item:
                            if isinstance(sub_item, dict):
                                details.update(sub_item)
                teams.append({
                    "team_key": details.get("team_key", ""),
                    "team_id": details.get("team_id", ""),
                    "name": details.get("name", ""),
                    "is_owned_by_current_login": details.get("is_owned_by_current_login", 0) == 1,
                })
        return teams
