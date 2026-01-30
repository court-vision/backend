"""
Injuries Extractor

Fetches injury data from BALLDONTLIE API.
Requires API key - get free key at https://app.balldontlie.io
"""

from datetime import date
from typing import Any, Optional

import requests

from core.logging import get_logger
from core.settings import settings
from core.resilience import with_retry, NetworkError, RateLimitError, ServerError
from pipelines.extractors.base import BaseExtractor


BALLDONTLIE_BASE_URL = "https://api.balldontlie.io/v1"


class InjuriesExtractor(BaseExtractor):
    """
    Extractor for NBA injury data via BALLDONTLIE API.

    Requires BALLDONTLIE_API_KEY environment variable.
    Free tier: 5 requests/minute.

    See: https://docs.balldontlie.io/
    """

    def __init__(self):
        super().__init__("injuries")
        self._api_key: Optional[str] = None

    def _get_api_key(self) -> str:
        """Get API key from settings."""
        if self._api_key is None:
            # Try to get from settings (will need to add to settings.py)
            api_key = getattr(settings, "balldontlie_api_key", None)
            if api_key:
                # Handle SecretStr
                self._api_key = (
                    api_key.get_secret_value()
                    if hasattr(api_key, "get_secret_value")
                    else str(api_key)
                )
            else:
                raise ValueError(
                    "BALLDONTLIE_API_KEY not configured. "
                    "Get a free key at https://app.balldontlie.io"
                )
        return self._api_key

    def _get_headers(self) -> dict:
        """Get request headers with API key."""
        return {
            "Authorization": self._get_api_key(),
            "Content-Type": "application/json",
        }

    def extract(self, **kwargs: Any) -> Any:
        """Not used directly - use get_current_injuries."""
        raise NotImplementedError("Use get_current_injuries")

    @with_retry(max_attempts=3, base_delay=2.0, max_delay=30.0)
    def get_current_injuries(self) -> list[dict]:
        """
        Fetch current injury report for all NBA players.

        Returns:
            List of injury dicts with player info and status
        """
        self.log.debug("current_injuries_start")

        try:
            response = requests.get(
                f"{BALLDONTLIE_BASE_URL}/player_injuries",
                headers=self._get_headers(),
                timeout=30,
            )

            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 60))
                raise RateLimitError(
                    "BALLDONTLIE rate limited", retry_after=retry_after
                )

            if response.status_code == 401:
                raise ValueError(
                    "Invalid BALLDONTLIE API key. "
                    "Check your BALLDONTLIE_API_KEY setting."
                )

            if response.status_code >= 500:
                raise ServerError(
                    "BALLDONTLIE server error", status_code=response.status_code
                )

            response.raise_for_status()
            data = response.json()

            injuries = data.get("data", [])
            self.log.info("current_injuries_complete", count=len(injuries))
            return injuries

        except requests.exceptions.Timeout:
            raise NetworkError("BALLDONTLIE request timed out")
        except requests.exceptions.ConnectionError:
            raise NetworkError("BALLDONTLIE connection failed")

    def normalize_injury_data(self, raw_injury: dict) -> dict:
        """
        Normalize injury data from BALLDONTLIE format to our schema.

        Args:
            raw_injury: Raw injury dict from BALLDONTLIE

        Returns:
            Normalized dict matching our PlayerInjury schema
        """
        # BALLDONTLIE format (based on typical sports API structure):
        # {
        #   "player": {"id": 123, "first_name": "...", "last_name": "..."},
        #   "team": {"id": 1, "abbreviation": "LAL", ...},
        #   "status": "Out",
        #   "comment": "Knee - Sprain"
        # }
        player = raw_injury.get("player", {})
        team = raw_injury.get("team", {})

        player_name = f"{player.get('first_name', '')} {player.get('last_name', '')}".strip()

        # Parse injury type from comment
        comment = raw_injury.get("comment", "") or ""
        injury_type, injury_detail = self._parse_injury_comment(comment)

        return {
            "player_id": player.get("id"),
            "player_name": player_name,
            "team": team.get("abbreviation"),
            "status": self._normalize_status(raw_injury.get("status", "Unknown")),
            "injury_type": injury_type,
            "injury_detail": injury_detail,
        }

    def _parse_injury_comment(self, comment: str) -> tuple[str | None, str | None]:
        """
        Parse injury type and detail from comment string.

        Examples:
            "Knee - Sprain" -> ("Knee", "Sprain")
            "Illness" -> ("Illness", None)
            "" -> (None, None)
        """
        if not comment:
            return None, None

        if " - " in comment:
            parts = comment.split(" - ", 1)
            return parts[0].strip(), parts[1].strip()

        return comment.strip(), None

    def _normalize_status(self, status: str) -> str:
        """
        Normalize injury status to standard values.

        Args:
            status: Raw status string

        Returns:
            One of: Out, Doubtful, Questionable, Probable, Available
        """
        if not status:
            return "Unknown"

        status_lower = status.lower().strip()

        if "out" in status_lower:
            return "Out"
        elif "doubtful" in status_lower:
            return "Doubtful"
        elif "questionable" in status_lower:
            return "Questionable"
        elif "probable" in status_lower or "likely" in status_lower:
            return "Probable"
        elif "available" in status_lower or "active" in status_lower or "healthy" in status_lower:
            return "Available"
        elif "day-to-day" in status_lower or "dtd" in status_lower:
            return "Questionable"
        else:
            return status  # Return original if unknown
