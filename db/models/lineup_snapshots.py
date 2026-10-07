"""
usr.lineup_snapshots / usr.lineup_snapshot_players — every fantasy team's lineup
for each finished provider day: who was rostered and the slot each one sat in.

Written by data-platform's `lineup_snapshots` pipeline once a night (ESPN serves
a past day's roster, membership and slots, with `?scoringPeriodId=N`); read by
the backend so a past day of a matchup shows the rosters that actually counted.
Keyed by the provider's own ids, not usr.teams: opponents have no usr.teams row
and two users in one league share one capture (migration 0029).

Mirrored byte-for-byte between backend and data-platform
(scripts/check_backend_mirror.py). Imports are limited to what both repos have.
"""

from datetime import datetime, timezone

from peewee import (
    AutoField,
    BooleanField,
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

SOURCES = ("pipeline", "backfill")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class LineupSnapshot(BaseModel):
    """One team's lineup on one provider day (the header; players hang off it)."""

    id = AutoField()
    provider = CharField(max_length=16)                 # espn | yahoo
    provider_league_id = CharField(max_length=64)       # ESPN: str(league_id); same key as usr.leagues
    season = IntegerField()                             # provider season id (ESPN 2027 = 2026-27)
    provider_team_id = IntegerField()                   # the team's id inside the league
    team_name = CharField(max_length=120)
    scoring_period_id = IntegerField()                  # ESPN day (1 = opening night)
    nba_date = DateField()                              # that day as a calendar date
    matchup_period_id = IntegerField(null=True)         # best effort
    opponent_provider_team_id = IntegerField(null=True) # best effort
    applied_stat_total = DecimalField(max_digits=8, decimal_places=2, null=True)   # the day's points over the active slots
    player_count = SmallIntegerField()
    source = CharField(max_length=16, default="pipeline")
    captured_at = DateTimeField(default=_now)           # the row's write time (a re-capture replaces the row)
    pipeline_run_id = UUIDField(null=True)

    class Meta:
        table_name = "lineup_snapshots"
        schema = "usr"
        indexes = (
            (("provider", "provider_league_id", "season", "provider_team_id", "scoring_period_id"), True),
            (("provider", "provider_league_id", "season", "nba_date"), False),
        )

    def __repr__(self):
        return (
            f"<LineupSnapshot(id={self.id}, league={self.provider}:{self.provider_league_id}/{self.season}, "
            f"team={self.provider_team_id}, period={self.scoring_period_id})>"
        )


class LineupSnapshotPlayer(BaseModel):
    """One rostered player inside a snapshot."""

    id = AutoField()
    snapshot = ForeignKeyField(
        LineupSnapshot, column_name="snapshot_id", backref="players", on_delete="CASCADE",
    )
    player_id = IntegerField()                          # the provider's player id (ESPN)
    player_name = CharField(max_length=120)
    pro_team = CharField(max_length=8, null=True)
    default_position_id = SmallIntegerField(null=True)
    lineup_slot_id = SmallIntegerField()                # ESPN slot ids: 0 PG ... 11 UT, 12 BE, 13 IR
    eligible_slot_ids = BinaryJSONField(null=True)      # [int]
    injured = BooleanField(default=False)
    injury_status = CharField(max_length=24, null=True)
    applied_total = DecimalField(max_digits=8, decimal_places=2, null=True)   # ESPN's points for him that day; NULL = no game

    class Meta:
        table_name = "lineup_snapshot_players"
        schema = "usr"
        indexes = (
            (("snapshot", "player_id"), True),
        )

    def __repr__(self):
        return f"<LineupSnapshotPlayer(snapshot={self.snapshot_id}, player={self.player_id}, slot={self.lineup_slot_id})>"
