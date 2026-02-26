"""
NBA API Extractor

Fetches data from NBA Stats API via nba_api library.
"""

import json
from datetime import date
from typing import Any

import pandas as pd

from core.settings import settings
from core.resilience import (
    with_retry,
    nba_api_circuit,
    NetworkError,
)
from pipelines.extractors.base import BaseExtractor


class NBAApiExtractor(BaseExtractor):
    """
    Extractor for NBA Stats API via nba_api library.

    Provides methods to fetch:
    - Player game logs for specific dates
    - League leaders with season totals
    - Advanced player stats (efficiency, usage, etc.)
    - Player biographical info
    - Game schedule and results
    """

    def __init__(self):
        super().__init__("nba_api")

    def extract(self, **kwargs: Any) -> Any:
        """Not used directly - use specific methods below."""
        raise NotImplementedError("Use specific methods like get_game_logs, get_advanced_stats, etc.")

    @with_retry(
        max_attempts=settings.retry_max_attempts,
        base_delay=settings.retry_base_delay,
        max_delay=settings.retry_max_delay,
    )
    @nba_api_circuit
    def get_game_logs(self, date_str: str, season: str) -> pd.DataFrame:
        """
        Fetch player game logs from NBA API for a specific date.

        Args:
            date_str: Date in MM/DD/YYYY format
            season: Season string like "2025-26"

        Returns:
            DataFrame with player game stats
        """
        from nba_api.stats.endpoints import playergamelogs

        self.log.debug("game_logs_start", date=date_str, season=season)

        try:
            game_logs = playergamelogs.PlayerGameLogs(
                date_from_nullable=date_str,
                date_to_nullable=date_str,
                season_nullable=season,
            )
            stats = game_logs.player_game_logs.get_data_frame()

            self.log.info("game_logs_complete", record_count=len(stats))
            return stats

        except Exception as e:
            error_str = str(e).lower()
            if "timeout" in error_str:
                raise NetworkError(f"NBA API timeout: {e}")
            if "connection" in error_str:
                raise NetworkError(f"NBA API connection error: {e}")
            raise

    @with_retry(
        max_attempts=settings.retry_max_attempts,
        base_delay=settings.retry_base_delay,
        max_delay=settings.retry_max_delay,
    )
    @nba_api_circuit
    def get_league_leaders(self, season: str | None = None) -> list[dict]:
        """
        Fetch league leaders from NBA API.

        Args:
            season: Season string like "2025-26" (defaults to settings.nba_season)

        Returns:
            List of player dicts with stats
        """
        from nba_api.stats.endpoints import leagueleaders

        season = season or settings.nba_season
        self.log.debug("leaders_start", season=season)

        try:
            leaders = leagueleaders.LeagueLeaders(
                season=season,
                per_mode48="Totals",
                stat_category_abbreviation="PTS",
            )
            api_data = leaders.get_normalized_dict()["LeagueLeaders"]

            self.log.info("leaders_complete", player_count=len(api_data))
            return api_data

        except Exception as e:
            error_str = str(e).lower()
            if "timeout" in error_str:
                raise NetworkError(f"NBA API timeout: {e}")
            if "connection" in error_str:
                raise NetworkError(f"NBA API connection error: {e}")
            raise

    @with_retry(
        max_attempts=settings.retry_max_attempts,
        base_delay=settings.retry_base_delay,
        max_delay=settings.retry_max_delay,
    )
    @nba_api_circuit
    def get_advanced_stats(self, season: str | None = None) -> list[dict]:
        """
        Fetch advanced player stats from NBA API.

        Uses LeagueDashPlayerStats with MeasureType="Advanced" to get
        efficiency ratings, usage, and impact metrics.

        Args:
            season: Season string like "2025-26" (defaults to settings.nba_season)

        Returns:
            List of player dicts with advanced stats
        """
        from nba_api.stats.endpoints import leaguedashplayerstats

        season = season or settings.nba_season
        self.log.debug("advanced_stats_start", season=season)

        try:
            stats = leaguedashplayerstats.LeagueDashPlayerStats(
                season=season,
                measure_type_detailed_defense="Advanced",
                per_mode_detailed="Totals",
            )
            api_data = stats.get_normalized_dict()["LeagueDashPlayerStats"]

            self.log.info("advanced_stats_complete", player_count=len(api_data))
            return api_data

        except Exception as e:
            error_str = str(e).lower()
            if "timeout" in error_str:
                raise NetworkError(f"NBA API timeout: {e}")
            if "connection" in error_str:
                raise NetworkError(f"NBA API connection error: {e}")
            raise

    @with_retry(
        max_attempts=settings.retry_max_attempts,
        base_delay=settings.retry_base_delay,
        max_delay=settings.retry_max_delay,
    )
    @nba_api_circuit
    def get_player_info(self, player_id: int) -> dict | None:
        """
        Fetch detailed player info from NBA API.

        Uses CommonPlayerInfo to get biographical data, draft info, etc.

        Args:
            player_id: NBA player ID

        Returns:
            Dict with player info or None if not found
        """
        from nba_api.stats.endpoints import commonplayerinfo

        self.log.debug("player_info_start", player_id=player_id)

        try:
            info = commonplayerinfo.CommonPlayerInfo(player_id=player_id)
            data = info.get_normalized_dict()

            if data.get("CommonPlayerInfo"):
                player_data = data["CommonPlayerInfo"][0]
                self.log.debug("player_info_complete", player_id=player_id)
                return player_data

            return None

        except Exception as e:
            error_str = str(e).lower()
            if "timeout" in error_str:
                raise NetworkError(f"NBA API timeout: {e}")
            if "connection" in error_str:
                raise NetworkError(f"NBA API connection error: {e}")
            # Player not found is not an error - return None
            if "not found" in error_str or "404" in error_str:
                self.log.warning("player_not_found", player_id=player_id)
                return None
            raise

    @with_retry(
        max_attempts=settings.retry_max_attempts,
        base_delay=settings.retry_base_delay,
        max_delay=settings.retry_max_delay,
    )
    @nba_api_circuit
    def get_league_game_log(self, season: str | None = None) -> list[dict]:
        """
        Fetch league-wide game log from NBA API.

        Returns all games for the season with scores and details.

        Args:
            season: Season string like "2025-26" (defaults to settings.nba_season)

        Returns:
            List of game dicts with scores and details
        """
        from nba_api.stats.endpoints import leaguegamelog

        season = season or settings.nba_season
        self.log.debug("league_game_log_start", season=season)

        try:
            game_log = leaguegamelog.LeagueGameLog(
                season=season,
                season_type_all_star="Regular Season",
            )
            api_data = game_log.get_normalized_dict()["LeagueGameLog"]

            self.log.info("league_game_log_complete", game_count=len(api_data))
            return api_data

        except Exception as e:
            error_str = str(e).lower()
            if "timeout" in error_str:
                raise NetworkError(f"NBA API timeout: {e}")
            if "connection" in error_str:
                raise NetworkError(f"NBA API connection error: {e}")
            raise

    @with_retry(
        max_attempts=settings.retry_max_attempts,
        base_delay=settings.retry_base_delay,
        max_delay=settings.retry_max_delay,
    )
    @nba_api_circuit
    def get_player_index(self, season: str | None = None) -> list[dict]:
        """
        Fetch all player profiles in a single bulk API call.

        Uses the PlayerIndex endpoint which returns biographical data
        (height, weight, position, draft info, etc.) for all players in one request.

        Args:
            season: Season string like "2025-26" (defaults to settings.nba_season)

        Returns:
            List of player dicts with profile data
        """
        from nba_api.stats.endpoints import playerindex

        season = season or settings.nba_season
        self.log.debug("player_index_start", season=season)

        try:
            idx = playerindex.PlayerIndex(
                season=season,
                league_id="00",
            )
            api_data = idx.get_normalized_dict()["PlayerIndex"]

            self.log.info("player_index_complete", player_count=len(api_data))
            return api_data

        except Exception as e:
            error_str = str(e).lower()
            if "timeout" in error_str:
                raise NetworkError(f"NBA API timeout: {e}")
            if "connection" in error_str:
                raise NetworkError(f"NBA API connection error: {e}")
            raise

    @with_retry(
        max_attempts=settings.retry_max_attempts,
        base_delay=settings.retry_base_delay,
        max_delay=settings.retry_max_delay,
    )
    @nba_api_circuit
    def check_all_games_final(self, game_date: date) -> bool:
        """
        Check if all NBA games on a given date have finished.

        Uses the live scoreboard endpoint for real-time game status.
        Returns True if all games are Final (gameStatus==3) or the scoreboard
        has already rolled to a different date (games are definitely done).
        Returns False if any game is still in progress.

        Args:
            game_date: The NBA game date to check (ET-based)

        Returns:
            True if all games are final, False if any are still live
        """
        from nba_api.live.nba.endpoints import scoreboard as live_scoreboard

        date_str = game_date.isoformat()
        self.log.debug("scoreboard_check_start", game_date=date_str)

        try:
            board = live_scoreboard.ScoreBoard()
            data = board.get_dict()["scoreboard"]
            scoreboard_date = data.get("gameDate", "")

            if scoreboard_date != date_str:
                # Scoreboard has rolled to a different date — last night's games are done
                self.log.info(
                    "scoreboard_date_rolled",
                    expected=date_str,
                    scoreboard=scoreboard_date,
                )
                return True

            games = data.get("games", [])
            if not games:
                self.log.info("scoreboard_no_games", game_date=date_str)
                return True

            not_final = [g.get("gameId") for g in games if g.get("gameStatus") != 3]
            all_final = len(not_final) == 0

            self.log.info(
                "scoreboard_status",
                game_date=date_str,
                total_games=len(games),
                not_final_count=len(not_final),
            )
            return all_final

        except Exception as e:
            error_str = str(e).lower()
            if "timeout" in error_str:
                raise NetworkError(f"NBA live API timeout: {e}")
            if "connection" in error_str:
                raise NetworkError(f"NBA live API connection error: {e}")
            raise

    @with_retry(
        max_attempts=settings.retry_max_attempts,
        base_delay=settings.retry_base_delay,
        max_delay=settings.retry_max_delay,
    )
    @nba_api_circuit
    def get_scoreboard_games(self, game_date: date) -> list[dict]:
        """
        Fetch today's games from the live scoreboard.

        Returns a list of dicts with game ID, status, period, and clock for
        each game on the given date. Returns an empty list if the scoreboard
        has rolled to a different date (i.e. all games are done for that date).

        Args:
            game_date: The NBA game date to check (ET-based)

        Returns:
            List of dicts with keys: game_id, game_status, period, game_clock
        """
        from nba_api.live.nba.endpoints import scoreboard as live_scoreboard

        date_str = game_date.isoformat()
        self.log.debug("scoreboard_games_start", game_date=date_str)

        try:
            board = live_scoreboard.ScoreBoard()
            data = board.get_dict()["scoreboard"]
            scoreboard_date = data.get("gameDate", "")

            if scoreboard_date != date_str:
                self.log.info(
                    "scoreboard_date_mismatch",
                    expected=date_str,
                    scoreboard=scoreboard_date,
                )
                return []

            games = data.get("games", [])
            result = [
                {
                    "game_id": g.get("gameId"),
                    "game_status": g.get("gameStatus", 1),
                    "period": g.get("period", 0),
                    "game_clock": g.get("gameClock", ""),
                    "home_team": g.get("homeTeam", {}).get("teamTricode", ""),
                    "away_team": g.get("awayTeam", {}).get("teamTricode", ""),
                    "home_score": g.get("homeTeam", {}).get("score"),
                    "away_score": g.get("awayTeam", {}).get("score"),
                }
                for g in games
                if g.get("gameId")
            ]

            self.log.info("scoreboard_games_complete", game_count=len(result))
            return result

        except Exception as e:
            error_str = str(e).lower()
            if "timeout" in error_str:
                raise NetworkError(f"NBA live API timeout: {e}")
            if "connection" in error_str:
                raise NetworkError(f"NBA live API connection error: {e}")
            raise

    @with_retry(
        max_attempts=settings.retry_max_attempts,
        base_delay=settings.retry_base_delay,
        max_delay=settings.retry_max_delay,
    )
    @nba_api_circuit
    def get_live_box_score(self, game_id: str) -> dict | None:
        """
        Fetch the live box score for a specific game.

        Returns the full game dict from NBA's live BoxScore endpoint, which
        contains homeTeam.players[] and awayTeam.players[] with statistics
        sub-dicts per player.

        Args:
            game_id: NBA game ID (e.g. "0022501234")

        Returns:
            Game dict with homeTeam/awayTeam player arrays, or None if not found
        """
        from nba_api.live.nba.endpoints import boxscore as live_boxscore

        self.log.debug("live_box_score_start", game_id=game_id)

        try:
            board = live_boxscore.BoxScore(game_id)
            game = board.get_dict().get("game")

            if not game:
                self.log.warning("live_box_score_empty", game_id=game_id)
                return None

            self.log.debug("live_box_score_complete", game_id=game_id)
            return game

        except Exception as e:
            if isinstance(e, json.JSONDecodeError):
                # NBA live API occasionally returns an empty body (pre-game, between
                # quarters, or transient hiccup). Treat as no data available.
                self.log.warning("live_box_score_empty_response", game_id=game_id, error=str(e))
                return None
            error_str = str(e).lower()
            if "timeout" in error_str:
                raise NetworkError(f"NBA live BoxScore timeout: {e}")
            if "connection" in error_str:
                raise NetworkError(f"NBA live BoxScore connection error: {e}")
            if "404" in error_str or "not found" in error_str:
                self.log.warning("live_box_score_not_found", game_id=game_id)
                return None
            raise

    @with_retry(
        max_attempts=settings.retry_max_attempts,
        base_delay=settings.retry_base_delay,
        max_delay=settings.retry_max_delay,
    )
    @nba_api_circuit
    def get_team_stats(self, season: str | None = None) -> list[dict]:
        """
        Fetch team stats from NBA API combining advanced and base measures.

        Makes two API calls — one for advanced efficiency metrics (OFF_RATING,
        DEF_RATING, NET_RATING, PACE, TS_PCT, etc.) and one for base per-game
        counting stats (PTS, REB, AST, STL, BLK, TOV, shooting percentages).
        Results are merged by TEAM_ABBREVIATION into one dict per team.

        Args:
            season: Season string like "2025-26" (defaults to settings.nba_season)

        Returns:
            List of 30 team dicts with both advanced and base stats merged
        """
        from nba_api.stats.endpoints import leaguedashteamstats

        season = season or settings.nba_season
        self.log.debug("team_stats_start", season=season)

        try:
            adv = leaguedashteamstats.LeagueDashTeamStats(
                season=season,
                measure_type_detailed_defense="Advanced",
                per_mode_detailed="Totals",
            ).get_normalized_dict()["LeagueDashTeamStats"]

            base = leaguedashteamstats.LeagueDashTeamStats(
                season=season,
                measure_type_detailed_defense="Base",
                per_mode_detailed="PerGame",
            ).get_normalized_dict()["LeagueDashTeamStats"]

            base_map = {row["TEAM_ABBREVIATION"]: row for row in base}
            merged = [
                {**base_map.get(row["TEAM_ABBREVIATION"], {}), **row}
                for row in adv
            ]

            self.log.info("team_stats_complete", team_count=len(merged))
            return merged

        except Exception as e:
            error_str = str(e).lower()
            if "timeout" in error_str:
                raise NetworkError(f"NBA API timeout: {e}")
            if "connection" in error_str:
                raise NetworkError(f"NBA API connection error: {e}")
            raise

    def get_all_player_ids(self, season: str | None = None) -> list[int]:
        """
        Get all active player IDs for a season.

        Useful for iterating through players to fetch individual data.

        Args:
            season: Season string (defaults to settings.nba_season)

        Returns:
            List of player IDs
        """
        leaders = self.get_league_leaders(season)
        return [player["PLAYER_ID"] for player in leaders]
