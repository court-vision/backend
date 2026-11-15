"""
nba.daily_rankings (migration 0050): a ranking stored every morning.

One row per player per board per source per day. `source` is whose ranking it
is: `cv` is Court Vision's board as the Draft lab shows it to a room with no
league, `espn` is ESPN's own rank that morning, and any other value is a
candidate Court Vision board kept beside `cv` while it is graded. `board` is
`points` (ESPN's default points weights; ESPN's STANDARD rank) or `categories`
(the standard nine categories; ESPN's ROTO rank). `as_of_date` is the fantasy
day the board was built for: its games count from that day on.

`value`, `score` and `games` are the source's own numbers. For `cv`: the
per-game number the board shows (fantasy points per game, or the nine-category
index), what the rank is ordered by, and the games he is expected to play from
here. For `espn`: its auction value, and nothing for the other two.

data-platform's daily_rankings pipeline writes it (`replace_day`); the backend
reads it. Rows are never pruned: the history is what the rankings are graded
on and what a rank's moves are read from.

Mirrored byte-for-byte between backend and data-platform
(scripts/check_backend_mirror.py).
"""

from datetime import date, datetime, timezone
from typing import Iterable, Mapping, Optional
from uuid import UUID

from peewee import (
    BigAutoField,
    CharField,
    DateField,
    DateTimeField,
    DecimalField,
    ForeignKeyField,
    IntegerField,
    SmallIntegerField,
    UUIDField,
    fn,
)

from db.base import BaseModel
from db.models.nba.players import Player

SOURCE_CV = "cv"
SOURCE_ESPN = "espn"
BOARD_POINTS = "points"
BOARD_CATEGORIES = "categories"
BOARDS = (BOARD_POINTS, BOARD_CATEGORIES)

# Columns a row may carry besides its key; `replace_day` fills what is missing with None
ROW_FIELDS = ("rank", "value", "score", "games", "season_gp", "injury_status", "line_source", "model")
BATCH_SIZE = 500


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DailyRanking(BaseModel):
    id = BigAutoField()
    as_of_date = DateField()
    season = CharField(max_length=7)
    source = CharField(max_length=16)
    board = CharField(max_length=12)
    player = ForeignKeyField(Player, on_delete="CASCADE", backref="daily_rankings", column_name="player_id")
    rank = IntegerField()
    value = DecimalField(max_digits=8, decimal_places=2, null=True)
    score = DecimalField(max_digits=10, decimal_places=3, null=True)
    games = DecimalField(max_digits=6, decimal_places=2, null=True)
    season_gp = SmallIntegerField(null=True)
    injury_status = CharField(max_length=20, null=True)
    line_source = CharField(max_length=12, null=True)
    model = CharField(max_length=32, null=True)
    pipeline_run_id = UUIDField(null=True)
    created_at = DateTimeField(default=_now)

    class Meta:
        table_name = "daily_rankings"
        schema = "nba"
        indexes = (
            (("as_of_date", "source", "board", "player"), True),
            (("player", "source", "board", "as_of_date"), False),
        )

    def __repr__(self) -> str:
        return (
            f"<DailyRanking({self.as_of_date} {self.source}/{self.board} "
            f"player_id={self.player_id} rank={self.rank})>"
        )

    @classmethod
    def replace_day(
        cls,
        as_of_date: date,
        season: str,
        source: str,
        board: str,
        rows: Iterable[Mapping],
        pipeline_run_id: Optional[UUID] = None,
    ) -> int:
        """Store one source's board for one day, replacing what the day held.

        `rows` are mappings with `player_id`, `rank` and any of `ROW_FIELDS`.
        The caller holds the transaction: a day's boards are published together
        or not at all, so a reader never finds half a ranking. Returns the
        number of rows written.
        """
        cls.delete().where(
            (cls.as_of_date == as_of_date) & (cls.source == source) & (cls.board == board)
        ).execute()
        payload = [
            {
                "as_of_date": as_of_date, "season": season, "source": source, "board": board,
                "player": row["player_id"], "pipeline_run_id": pipeline_run_id,
                **{name: row.get(name) for name in ROW_FIELDS},
            }
            for row in rows
        ]
        for start in range(0, len(payload), BATCH_SIZE):
            cls.insert_many(payload[start:start + BATCH_SIZE]).execute()
        return len(payload)

    @classmethod
    def stored(cls, as_of_date: date, source: str) -> int:
        """How many rows `source` has for `as_of_date`, both boards together."""
        return cls.select().where((cls.as_of_date == as_of_date) & (cls.source == source)).count()

    @classmethod
    def latest_date(cls, source: str, season: Optional[str] = None, before: Optional[date] = None) -> Optional[date]:
        """The newest day `source` has a board for; with `before`, the newest earlier than it."""
        cond = cls.source == source
        if season is not None:
            cond &= cls.season == season
        if before is not None:
            cond &= cls.as_of_date < before
        return cls.select(fn.MAX(cls.as_of_date)).where(cond).scalar()

    @classmethod
    def ranks_on(cls, as_of_date: date, source: str, board: str) -> dict[int, int]:
        """Player id -> rank on one source's board for one day."""
        query = cls.select(cls.player, cls.rank).where(
            (cls.as_of_date == as_of_date) & (cls.source == source) & (cls.board == board)
        )
        return {row.player_id: int(row.rank) for row in query}
