"""
Player Profiles Pipeline

Fetches player biographical data (height, weight, position, draft info) from NBA API.
"""

import time
from datetime import datetime

import pytz

from core.settings import settings
from db.models.nba import Player, PlayerProfile
from pipelines.base import BasePipeline
from pipelines.config import PipelineConfig
from pipelines.context import PipelineContext
from pipelines.extractors import NBAApiExtractor


class PlayerProfilesPipeline(BasePipeline):
    """
    Fetch player profiles and insert into player_profiles.

    This pipeline:
    1. Gets all active player IDs
    2. For each player, fetches CommonPlayerInfo
    3. Inserts/updates profile records

    Note: This pipeline makes one API call per player, so it should be
    run infrequently (weekly or on-demand) to avoid rate limiting.
    """

    config = PipelineConfig(
        name="player_profiles",
        display_name="Player Profiles",
        description="Fetches player biographical data (height, position, draft info)",
        target_table="nba.player_profiles",
        timeout_seconds=1800,  # 30 minutes - this pipeline is slow
    )

    # Rate limit: wait between API calls (seconds)
    API_DELAY = 0.6

    def __init__(self):
        super().__init__()
        self.nba_extractor = NBAApiExtractor()

    def execute(self, ctx: PipelineContext) -> None:
        """Execute the player profiles pipeline."""
        ctx.log.info("starting_profile_fetch")

        # Get all active player IDs
        player_ids = self.nba_extractor.get_all_player_ids()
        total_players = len(player_ids)

        ctx.log.info("players_to_process", count=total_players)

        # Process each player with rate limiting
        for i, player_id in enumerate(player_ids, 1):
            try:
                # Fetch player info
                info = self.nba_extractor.get_player_info(player_id)

                if info:
                    self._process_player_info(player_id, info, ctx)
                    ctx.increment_records()

                # Log progress every 50 players
                if i % 50 == 0:
                    ctx.log.info(
                        "progress",
                        processed=i,
                        total=total_players,
                        percent=round(i / total_players * 100, 1),
                    )

                # Rate limit to avoid API throttling
                time.sleep(self.API_DELAY)

            except Exception as e:
                ctx.log.warning(
                    "player_fetch_error",
                    player_id=player_id,
                    error=str(e),
                )
                continue

        ctx.log.info("processing_complete", records=ctx.records_processed)

    def _process_player_info(
        self, player_id: int, info: dict, ctx: PipelineContext
    ) -> None:
        """Process and store player info."""
        # Ensure player exists in dimension table
        full_name = f"{info.get('FIRST_NAME', '')} {info.get('LAST_NAME', '')}".strip()
        if not full_name:
            full_name = info.get("DISPLAY_FIRST_LAST", f"Player {player_id}")

        Player.upsert_player(player_id=player_id, name=full_name)

        # Parse birthdate
        birthdate = None
        if info.get("BIRTHDATE"):
            try:
                # NBA API returns ISO format: "1988-03-14T00:00:00"
                birthdate_str = info["BIRTHDATE"].split("T")[0]
                birthdate = datetime.strptime(birthdate_str, "%Y-%m-%d").date()
            except (ValueError, AttributeError):
                pass

        # Prepare profile data
        profile_data = {
            "first_name": info.get("FIRST_NAME"),
            "last_name": info.get("LAST_NAME"),
            "birthdate": birthdate,
            "height": info.get("HEIGHT"),
            "weight": self._parse_int(info.get("WEIGHT")),
            "position": info.get("POSITION"),
            "jersey_number": info.get("JERSEY"),
            "team_id": self._get_team_abbrev(info),
            "draft_year": self._parse_int(info.get("DRAFT_YEAR")),
            "draft_round": self._parse_int(info.get("DRAFT_ROUND")),
            "draft_number": self._parse_int(info.get("DRAFT_NUMBER")),
            "season_exp": self._parse_int(info.get("SEASON_EXP")),
            "country": info.get("COUNTRY"),
            "school": info.get("SCHOOL"),
            "from_year": self._parse_int(info.get("FROM_YEAR")),
            "to_year": self._parse_int(info.get("TO_YEAR")),
        }

        # Upsert profile
        PlayerProfile.upsert_profile(player_id, profile_data)

    def _parse_int(self, value) -> int | None:
        """Safely parse an integer value."""
        if value is None:
            return None
        try:
            # Handle "Undrafted" or other non-numeric values
            return int(value)
        except (ValueError, TypeError):
            return None

    def _get_team_abbrev(self, info: dict) -> str | None:
        """Extract team abbreviation from player info."""
        abbrev = info.get("TEAM_ABBREVIATION")
        if abbrev and len(abbrev) <= 3:
            return abbrev
        return None
