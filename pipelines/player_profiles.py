"""
Player Profiles Pipeline

Fetches player biographical data (height, weight, position, draft info) from NBA API.
Uses the PlayerIndex bulk endpoint for single-call retrieval of all players.
"""

from datetime import datetime

from db.base import db
from db.models.nba import Player, PlayerProfile
from pipelines.base import BasePipeline
from pipelines.config import PipelineConfig
from pipelines.context import PipelineContext
from pipelines.extractors import NBAApiExtractor

# Batch size for bulk DB upserts
BATCH_SIZE = 100


class PlayerProfilesPipeline(BasePipeline):
    """
    Fetch player profiles using the PlayerIndex bulk endpoint.

    This pipeline:
    1. Fetches all active players in a single PlayerIndex API call
    2. Transforms the response into player dimension + profile records
    3. Bulk upserts all records in batched DB writes

    Previously made one CommonPlayerInfo call per player (~500 calls, 5-10 min).
    Now uses a single PlayerIndex call (~2-5 seconds total).
    """

    config = PipelineConfig(
        name="player_profiles",
        display_name="Player Profiles",
        description="Fetches player biographical data (height, position, draft info)",
        target_table="nba.player_profiles",
        timeout_seconds=120,
    )

    def __init__(self):
        super().__init__()
        self.nba_extractor = NBAApiExtractor()

    def execute(self, ctx: PipelineContext) -> None:
        """Execute the player profiles pipeline."""
        ctx.log.info("starting_profile_fetch")

        # Single API call to get all player profiles
        raw_players = self.nba_extractor.get_player_index()
        total_players = len(raw_players)
        ctx.log.info("players_fetched", count=total_players)

        # Transform into player dimension and profile records
        players_data = []
        profiles_data = []
        current_year = datetime.now().year
        now = datetime.utcnow()

        for raw in raw_players:
            player_id = raw.get("PERSON_ID")
            if not player_id:
                continue

            first_name = raw.get("PLAYER_FIRST_NAME", "")
            last_name = raw.get("PLAYER_LAST_NAME", "")
            full_name = f"{first_name} {last_name}".strip() or f"Player {player_id}"
            position = raw.get("POSITION")

            # Player dimension record
            players_data.append({
                "id": player_id,
                "name": full_name,
                "name_normalized": full_name.lower().strip(),
                "position": position,
                "created_at": now,
                "updated_at": now,
            })

            # Compute season_exp from from_year
            from_year = self._parse_int(raw.get("FROM_YEAR"))
            season_exp = (current_year - from_year) if from_year else None

            # Validate team abbreviation
            team_abbrev = raw.get("TEAM_ABBREVIATION")
            if team_abbrev and len(team_abbrev) > 3:
                team_abbrev = None

            # Profile record
            profiles_data.append({
                "player": player_id,
                "first_name": first_name or None,
                "last_name": last_name or None,
                "height": raw.get("HEIGHT") or None,
                "weight": self._parse_int(raw.get("WEIGHT")),
                "position": position,
                "jersey_number": raw.get("JERSEY_NUMBER") or None,
                "team_id": team_abbrev,
                "draft_year": self._parse_int(raw.get("DRAFT_YEAR")),
                "draft_round": self._parse_int(raw.get("DRAFT_ROUND")),
                "draft_number": self._parse_int(raw.get("DRAFT_NUMBER")),
                "season_exp": season_exp,
                "country": raw.get("COUNTRY") or None,
                "school": raw.get("COLLEGE") or None,
                "from_year": from_year,
                "to_year": self._parse_int(raw.get("TO_YEAR")),
                "updated_at": now,
            })

        # Bulk upsert in a single transaction
        ctx.log.info(
            "bulk_upserting",
            player_count=len(players_data),
            profile_count=len(profiles_data),
        )

        with db.atomic():
            # Upsert player dimension records in batches
            for i in range(0, len(players_data), BATCH_SIZE):
                batch = players_data[i : i + BATCH_SIZE]
                (
                    Player.insert_many(batch)
                    .on_conflict(
                        conflict_target=[Player.id],
                        preserve=[
                            Player.name,
                            Player.name_normalized,
                            Player.position,
                            Player.updated_at,
                        ],
                    )
                    .execute()
                )

            # Upsert profile records in batches
            for i in range(0, len(profiles_data), BATCH_SIZE):
                batch = profiles_data[i : i + BATCH_SIZE]
                (
                    PlayerProfile.insert_many(batch)
                    .on_conflict(
                        conflict_target=[PlayerProfile.player],
                        preserve=[
                            PlayerProfile.first_name,
                            PlayerProfile.last_name,
                            PlayerProfile.height,
                            PlayerProfile.weight,
                            PlayerProfile.position,
                            PlayerProfile.jersey_number,
                            PlayerProfile.team,
                            PlayerProfile.draft_year,
                            PlayerProfile.draft_round,
                            PlayerProfile.draft_number,
                            PlayerProfile.season_exp,
                            PlayerProfile.country,
                            PlayerProfile.school,
                            PlayerProfile.from_year,
                            PlayerProfile.to_year,
                            PlayerProfile.updated_at,
                        ],
                    )
                    .execute()
                )

        ctx.increment_records(len(profiles_data))
        ctx.log.info("processing_complete", records=ctx.records_processed)

    def _parse_int(self, value) -> int | None:
        """Safely parse an integer value."""
        if value is None:
            return None
        try:
            return int(value)
        except (ValueError, TypeError):
            return None
