"""
nba.availability_reports and nba.player_availability (migration 0035).

`AvailabilityReport` is the append-only log of what each source said about a
player for a game (or, for roster-level sources, a date): one row per status
change, never one per poll. `PlayerAvailability` is the resolved verdict, one
row per (player, game_date); `changed_at` moves only when `verdict` changes and
is what the late-scratch reactor keys on. data-platform's availability pipeline
writes both; the backend reads the verdict.

Mirrored byte-for-byte between backend and data-platform
(scripts/check_backend_mirror.py).
"""

from datetime import datetime, timezone

from peewee import (
    BigAutoField,
    BooleanField,
    CharField,
    CompositeKey,
    DateField,
    DateTimeField,
    ForeignKeyField,
    SmallIntegerField,
    TextField,
    UUIDField,
)

from db.base import BaseModel
from db.models.nba.news import NewsItem
from db.models.nba.players import Player

REPORT_SOURCES = ("nba_boxscore", "nba_report", "espn_injuries", "espn_fantasy", "news_claim")
# `inactive` / `active` only ever come from the box score.
REPORT_STATUSES = ("out", "doubtful", "questionable", "probable", "available", "inactive", "active")
VERDICTS = ("out", "doubtful", "questionable", "probable", "available", "unknown")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AvailabilityReport(BaseModel):
    id = BigAutoField()
    player = ForeignKeyField(Player, on_delete="CASCADE", backref="availability_reports", column_name="player_id")
    game_id = CharField(max_length=20, null=True)  # when the source is per game; no FK, see 0035
    game_date = DateField()
    source = CharField(max_length=16)
    status = CharField(max_length=12)
    reason = CharField(max_length=100, null=True)
    detail = TextField(null=True)
    expected_return = DateField(null=True)
    source_time = DateTimeField(null=True)         # the source's own timestamp
    observed_at = DateTimeField(default=_now)
    confidence = SmallIntegerField()               # 100 box score ... 40-60 news claim
    news_item = ForeignKeyField(NewsItem, null=True, on_delete="SET NULL", column_name="news_item_id")
    pipeline_run_id = UUIDField(null=True)

    class Meta:
        table_name = "availability_reports"
        schema = "nba"


class PlayerAvailability(BaseModel):
    player = ForeignKeyField(Player, on_delete="CASCADE", backref="availability", column_name="player_id")
    game_date = DateField()
    game_id = CharField(max_length=20, null=True)
    verdict = CharField(max_length=12)
    final = BooleanField(default=False)            # the box score has spoken
    decided_by = CharField(max_length=16, null=True)
    reason = CharField(max_length=100, null=True)
    detail = TextField(null=True)
    expected_return = DateField(null=True)
    confidence = SmallIntegerField(null=True)
    previous_verdict = CharField(max_length=12, null=True)
    changed_at = DateTimeField(default=_now)       # last time `verdict` changed
    updated_at = DateTimeField(default=_now)

    class Meta:
        table_name = "player_availability"
        schema = "nba"
        primary_key = CompositeKey("player", "game_date")
