"""
Injuries Extractor

Fetches injury data from nbainjuries package.
"""

from datetime import date
from typing import Any

from core.logging import get_logger
from pipelines.extractors.base import BaseExtractor


class InjuriesExtractor(BaseExtractor):
    """
    Extractor for NBA injury data via nbainjuries package.

    Provides methods to fetch:
    - Current injury report for all players
    - Historical injury data
    """

    def __init__(self):
        super().__init__("injuries")
        self._client = None

    def _get_client(self):
        """Lazy-load the nbainjuries client."""
        if self._client is None:
            try:
                from nbainjuries import Injuries
                self._client = Injuries()
            except ImportError:
                self.log.error(
                    "nbainjuries_not_installed",
                    message="Install with: pip install nbainjuries",
                )
                raise ImportError(
                    "nbainjuries package not installed. "
                    "Install with: pip install nbainjuries"
                )
        return self._client

    def extract(self, **kwargs: Any) -> Any:
        """Not used directly - use specific methods below."""
        raise NotImplementedError("Use get_current_injuries or get_injury_history")

    def get_current_injuries(self) -> list[dict]:
        """
        Fetch current injury report for all NBA players.

        Returns:
            List of injury dicts with player info and status
        """
        self.log.debug("current_injuries_start")

        try:
            client = self._get_client()
            injuries_df = client.current()

            if injuries_df is None or injuries_df.empty:
                self.log.info("no_injuries_found")
                return []

            # Convert DataFrame to list of dicts
            injuries = injuries_df.to_dict("records")

            self.log.info("current_injuries_complete", count=len(injuries))
            return injuries

        except ImportError:
            raise
        except Exception as e:
            self.log.error("current_injuries_error", error=str(e))
            raise

    def get_injury_history(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[dict]:
        """
        Fetch historical injury data within a date range.

        Args:
            start_date: Start of date range (defaults to season start)
            end_date: End of date range (defaults to today)

        Returns:
            List of injury dicts with player info and status
        """
        self.log.debug(
            "injury_history_start",
            start_date=str(start_date),
            end_date=str(end_date),
        )

        try:
            client = self._get_client()

            # nbainjuries historical method signature may vary
            # This is a general approach
            if start_date and end_date:
                injuries_df = client.historical(
                    start_date=start_date.strftime("%Y-%m-%d"),
                    end_date=end_date.strftime("%Y-%m-%d"),
                )
            else:
                injuries_df = client.historical()

            if injuries_df is None or injuries_df.empty:
                self.log.info("no_injury_history_found")
                return []

            injuries = injuries_df.to_dict("records")

            self.log.info("injury_history_complete", count=len(injuries))
            return injuries

        except ImportError:
            raise
        except Exception as e:
            self.log.error("injury_history_error", error=str(e))
            raise

    def normalize_injury_data(self, raw_injury: dict) -> dict:
        """
        Normalize injury data from nbainjuries format to our schema.

        Args:
            raw_injury: Raw injury dict from nbainjuries

        Returns:
            Normalized dict matching our PlayerInjury schema
        """
        # Map nbainjuries fields to our schema
        # Field names may vary - adjust based on actual nbainjuries output
        return {
            "player_name": raw_injury.get("Player", raw_injury.get("player_name")),
            "team": raw_injury.get("Team", raw_injury.get("team")),
            "status": self._normalize_status(
                raw_injury.get("Status", raw_injury.get("status", "Unknown"))
            ),
            "injury_type": raw_injury.get("Injury", raw_injury.get("injury")),
            "injury_detail": raw_injury.get("Description", raw_injury.get("description")),
            "report_date": raw_injury.get("Date", raw_injury.get("date")),
        }

    def _normalize_status(self, status: str) -> str:
        """
        Normalize injury status to standard values.

        Args:
            status: Raw status string

        Returns:
            One of: Out, Doubtful, Questionable, Probable, Available
        """
        status_lower = status.lower().strip()

        if "out" in status_lower:
            return "Out"
        elif "doubtful" in status_lower:
            return "Doubtful"
        elif "questionable" in status_lower:
            return "Questionable"
        elif "probable" in status_lower or "likely" in status_lower:
            return "Probable"
        elif "available" in status_lower or "active" in status_lower:
            return "Available"
        else:
            # Default to Questionable for unknown statuses
            return "Questionable"
