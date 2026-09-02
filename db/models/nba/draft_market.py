"""
Draft Market Table

Draft-market snapshots: ESPN's editorial draft rank and auction value plus the
crowd averages from real ESPN drafts (average draft position and average auction
price), plus the position fields the draft room needs (primary position,
eligibility, injury). One row per (player, season, source, as_of_date); keeping
snapshots per date makes September rank drift (risers/fallers) queryable. There
is no positional rank on the wire — derive it at read time. Written by the
data-platform preseason-market pipeline; backend reads.
"""

from datetime import datetime
from uuid import UUID

from peewee import (
    AutoField,
    CharField,
    DateField,
    DateTimeField,
    DecimalField,
    ForeignKeyField,
    IntegerField,
    SmallIntegerField,
    UUIDField,
)
from playhouse.postgres_ext import BinaryJSONField

from db.base import BaseModel
from db.models.nba.players import Player


class DraftMarket(BaseModel):
    """
    One player's draft-market snapshot for a season.

    Attributes:
        overall_rank: editorial draftRanksByRankType.STANDARD.rank
        auction_value: editorial auction value
        adp: ownership.averageDraftPosition (mean across real ESPN drafts)
        auction_value_avg: ownership.auctionValueAverage
        default_position_id: ESPN primary position, 1-based (1=PG ... 5=C) — the
            id space usr.leagues.position_limits is keyed by, and what ESPN counts
            hard position caps against
        eligible_slot_ids: ESPN eligibleSlots, 0-based lineup-slot ids
            (0=PG ... 4=C, 5=G, 6=F, 11=UT). Complete and authoritative: a pure
            centre is simply not F-eligible. Never mix with the id space above.
        injury_status: raw ESPN injuryStatus (ACTIVE, OUT, DAY_TO_DAY, ...)
    """

    id = AutoField(primary_key=True)
    player = ForeignKeyField(
        Player,
        backref="draft_market",
        on_delete="CASCADE",
        column_name="player_id",
    )
    season = CharField(max_length=7, index=True)  # e.g., '2026-27'
    source = CharField(max_length=16, default="espn")
    as_of_date = DateField()

    overall_rank = IntegerField(null=True)
    auction_value = DecimalField(max_digits=6, decimal_places=1, null=True)
    adp = DecimalField(max_digits=6, decimal_places=2, null=True)
    auction_value_avg = DecimalField(max_digits=7, decimal_places=2, null=True)

    # Position fields (null on snapshots written before the pipeline captured them)
    default_position_id = SmallIntegerField(null=True)
    eligible_slot_ids = BinaryJSONField(null=True)
    injury_status = CharField(max_length=20, null=True)

    # Audit columns
    pipeline_run_id = UUIDField(null=True, index=True)
    created_at = DateTimeField(default=datetime.utcnow)

    class Meta:
        table_name = "draft_market"
        schema = "nba"
        indexes = (
            # One snapshot per player per season per source per date
            (("player", "season", "source", "as_of_date"), True),
            # Latest-snapshot queries
            (("season", "as_of_date"), False),
        )

    def __repr__(self) -> str:
        return (
            f"<DraftMarket("
            f"player_id={self.player_id}, "
            f"season='{self.season}', "
            f"rank={self.overall_rank}, "
            f"adp={self.adp})>"
        )

    @classmethod
    def record_market(
        cls,
        player_id: int,
        season: str,
        as_of_date,
        overall_rank: int | None = None,
        auction_value: float | None = None,
        adp: float | None = None,
        auction_value_avg: float | None = None,
        default_position_id: int | None = None,
        eligible_slot_ids: list[int] | None = None,
        injury_status: str | None = None,
        source: str = "espn",
        pipeline_run_id: UUID | None = None,
    ) -> "DraftMarket":
        """Record a market snapshot. Re-running the same day updates the row."""
        values = {
            "overall_rank": overall_rank,
            "auction_value": auction_value,
            "adp": adp,
            "auction_value_avg": auction_value_avg,
            "default_position_id": default_position_id,
            "eligible_slot_ids": eligible_slot_ids,
            "injury_status": injury_status,
        }
        market, created = cls.get_or_create(
            player_id=player_id,
            season=season,
            source=source,
            as_of_date=as_of_date,
            defaults={**values, "pipeline_run_id": pipeline_run_id},
        )

        if not created:
            for key, value in values.items():
                setattr(market, key, value)
            market.pipeline_run_id = pipeline_run_id
            market.save()

        return market

    @classmethod
    def latest_for_season(cls, season: str, source: str = "espn") -> list["DraftMarket"]:
        """Every player's row from the most recent snapshot date for `season`."""
        latest = (
            cls.select(cls.as_of_date)
            .where((cls.season == season) & (cls.source == source))
            .order_by(cls.as_of_date.desc())
            .limit(1)
            .scalar()
        )
        if latest is None:
            return []
        return list(
            cls.select().where(
                (cls.season == season) & (cls.source == source) & (cls.as_of_date == latest)
            )
        )
