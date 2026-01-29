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
    """

    def __init__(self):
        super().__init__("nba_api")

    def extract(self, **kwargs: Any) -> Any:
        """Not used directly - use specific methods below."""
        raise NotImplementedError("Use get_game_logs or get_league_leaders")

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
