"""
NBA API Extractor

Fetches data from NBA Stats API via nba_api library.
"""

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
