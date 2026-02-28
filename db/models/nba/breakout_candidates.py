"""
Breakout Candidates Table

Stores players identified as likely to see increased minutes/production
due to a prominent teammate being injured. Populated daily by the
BreakoutDetectionPipeline.

The key insight: when a high-minutes starter goes OUT, their minutes
redistribute to teammates — especially positional counterparts on the
same team. This table identifies those beneficiaries so they can be
recommended as streamers BEFORE their stats spike.
"""

from datetime import datetime, date
from uuid import UUID

from peewee import (
    AutoField,
    CharField,
    DateField,
    DateTimeField,
    DecimalField,
    SmallIntegerField,
    UUIDField,
    ForeignKeyField,
)

from db.base import BaseModel
from db.models.nba.players import Player
from db.models.nba.teams import NBATeam


class BreakoutCandidate(BaseModel):
    """
    A player predicted to benefit from a prominent teammate's injury.

    One record per beneficiary per pipeline run date. When a new pipeline
    run happens, existing records for that date are overwritten (upsert).

    Attributes:
        id: Auto-incrementing primary key
        injured_player: The OUT/Doubtful player creating the minutes vacuum
        injured_avg_min: Their season avg minutes/game (e.g. 34.2)
        injury_status: "Out" or "Doubtful"
        expected_return: ESPN's listed return date (often null)
        beneficiary: The recommended streamer
        team: NBA team both players are on
        depth_rank: Position-group depth chart rank (1=starter, 2=first backup, etc.)
        beneficiary_avg_min: Beneficiary's current season avg min/game
        beneficiary_avg_fpts: Beneficiary's current season avg fpts/game
        projected_min_boost: Estimated additional minutes they'll absorb
        opp_min_avg: Avg min in position-validated opportunity games
        opp_fpts_avg: Avg fpts in position-validated opportunity games
        opp_game_count: Number of validated opportunity games found
        breakout_score: Composite ranking score (higher = stronger candidate)
        as_of_date: Date this detection was run
        pipeline_run_id: Reference to pipeline run for audit
        created_at: Record creation timestamp
    """

    id = AutoField(primary_key=True)

    # The player who is OUT (creates the minutes vacuum)
    injured_player = ForeignKeyField(
        Player,
        backref="caused_breakouts",
        on_delete="CASCADE",
        column_name="injured_player_id",
    )
    injured_avg_min = DecimalField(max_digits=5, decimal_places=1)
    injury_status = CharField(max_length=20)  # "Out" or "Doubtful"
    expected_return = DateField(null=True)

    # The player who benefits (the streamer recommendation)
    beneficiary = ForeignKeyField(
        Player,
        backref="breakout_opportunities",
        on_delete="CASCADE",
        column_name="beneficiary_player_id",
    )
    team = ForeignKeyField(
        NBATeam,
        on_delete="SET NULL",
        null=True,
        column_name="team_id",
    )

    # Depth chart position on the team (1 = starter, 2 = first backup, etc.)
    depth_rank = SmallIntegerField(default=99)

    # Scoring signals
    beneficiary_avg_min = DecimalField(max_digits=5, decimal_places=1)
    beneficiary_avg_fpts = DecimalField(max_digits=6, decimal_places=1)
    projected_min_boost = DecimalField(max_digits=4, decimal_places=1)
    # Position-validated opportunity game stats (replaces player-specific absence stats)
    opp_min_avg = DecimalField(max_digits=5, decimal_places=1, null=True)
    opp_fpts_avg = DecimalField(max_digits=6, decimal_places=1, null=True)
    opp_game_count = SmallIntegerField(default=0)
    breakout_score = DecimalField(max_digits=6, decimal_places=1)

    # Metadata
    as_of_date = DateField(index=True)
    pipeline_run_id = UUIDField(null=True, index=True)
    created_at = DateTimeField(default=datetime.utcnow)

    class Meta:
        table_name = "breakout_candidates"
        schema = "nba"
        indexes = (
            # One entry per beneficiary per date (prevents duplicates)
            (("beneficiary", "as_of_date"), True),
            # For retrieving all candidates for a given injured player
            (("injured_player", "as_of_date"), False),
            # For ranked retrieval sorted by score
            (("as_of_date", "breakout_score"), False),
        )

    def __repr__(self) -> str:
        return (
            f"<BreakoutCandidate("
            f"beneficiary_id={self.beneficiary_id}, "
            f"injured_id={self.injured_player_id}, "
            f"score={self.breakout_score}, "
            f"date={self.as_of_date})>"
        )

    @classmethod
    def upsert(
        cls,
        injured_player_id: int,
        injured_avg_min: float,
        injury_status: str,
        expected_return: date | None,
        beneficiary_player_id: int,
        team_id: str,
        depth_rank: int,
        beneficiary_avg_min: float,
        beneficiary_avg_fpts: float,
        projected_min_boost: float,
        opp_min_avg: float | None,
        opp_fpts_avg: float | None,
        opp_game_count: int,
        breakout_score: float,
        as_of_date: date,
        pipeline_run_id: UUID | None = None,
    ) -> "BreakoutCandidate":
        """Insert or update a breakout candidate record."""
        defaults = {
            "injured_player_id": injured_player_id,
            "injured_avg_min": injured_avg_min,
            "injury_status": injury_status,
            "expected_return": expected_return,
            "team_id": team_id,
            "depth_rank": depth_rank,
            "beneficiary_avg_min": beneficiary_avg_min,
            "beneficiary_avg_fpts": beneficiary_avg_fpts,
            "projected_min_boost": projected_min_boost,
            "opp_min_avg": opp_min_avg,
            "opp_fpts_avg": opp_fpts_avg,
            "opp_game_count": opp_game_count,
            "breakout_score": breakout_score,
            "pipeline_run_id": pipeline_run_id,
        }

        candidate, created = cls.get_or_create(
            beneficiary_player_id=beneficiary_player_id,
            as_of_date=as_of_date,
            defaults=defaults,
        )

        if not created:
            for key, value in defaults.items():
                setattr(candidate, key, value)
            candidate.save()

        return candidate

    @classmethod
    def get_latest_candidates(
        cls,
        limit: int = 20,
        team_id: str | None = None,
    ) -> list["BreakoutCandidate"]:
        """
        Get the most recent breakout candidates, sorted by score.

        Args:
            limit: Max records to return
            team_id: Optional filter by NBA team abbreviation

        Returns:
            List of BreakoutCandidate records with player data eager-loaded
        """
        from peewee import fn

        # Find the latest date with data
        latest_date = (
            cls.select(fn.MAX(cls.as_of_date))
            .scalar()
        )

        if not latest_date:
            return []

        query = (
            cls.select(cls, Player, NBATeam)
            .join(Player, on=(cls.beneficiary == Player.id))
            .switch(cls)
            .join(NBATeam, on=(cls.team == NBATeam.id))
            .where(cls.as_of_date == latest_date)
            .order_by(cls.breakout_score.desc())
            .limit(limit)
        )

        if team_id:
            query = query.where(cls.team == team_id)

        return list(query)
