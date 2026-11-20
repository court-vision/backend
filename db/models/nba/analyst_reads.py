"""
nba.analyst_reads (migration 0053): what the AI analyst read each morning.

One row per player it was asked about on a morning (the Live rankings plan,
step 4): why the screen picked him (`reasons`), what the read came to
(`outcome`), the note it left in nba.projection_adjustments' `analyst` scope
(`note_id`), what it would say against a note written by hand (`disagrees`),
and what the read cost. `proposal` is what came back, as it came back, kept
even when the server refused it.

    outcome  note       a new or changed note was stored
             unchanged  the note it already had still says it
             none       nothing a box score cannot say, or nothing to cite
             retired    the note it had no longer holds, and was withdrawn
             refused    the server refused what came back; `summary` says why
             declined   the model declined the request
             failed     the read did not complete; the next run reads again

`fingerprint` is a hash of what the read was shown: a player whose facts have
not changed since his last completed read is not read again.

data-platform's analyst pipeline writes it; its dashboard reads it as the
morning digest. Rows are never pruned: they are the analyst's record.

Mirrored byte-for-byte between backend and data-platform
(scripts/check_backend_mirror.py).
"""

from datetime import datetime, timezone
from typing import Iterable

from peewee import (
    BigAutoField,
    BigIntegerField,
    CharField,
    DateField,
    DateTimeField,
    ForeignKeyField,
    IntegerField,
    TextField,
    UUIDField,
)
from playhouse.postgres_ext import BinaryJSONField

from db.base import BaseModel
from db.models.nba.players import Player
from db.models.nba.projection_adjustments import ProjectionAdjustment

OUTCOME_NOTE = "note"
OUTCOME_UNCHANGED = "unchanged"
OUTCOME_NONE = "none"
OUTCOME_RETIRED = "retired"
OUTCOME_REFUSED = "refused"
OUTCOME_DECLINED = "declined"
OUTCOME_FAILED = "failed"
OUTCOMES = (
    OUTCOME_NOTE, OUTCOME_UNCHANGED, OUTCOME_NONE, OUTCOME_RETIRED,
    OUTCOME_REFUSED, OUTCOME_DECLINED, OUTCOME_FAILED,
)
# A read that came to an answer about the facts it was shown. One that failed
# is read again; so is one the model declined, in case the facts change.
SETTLED = (OUTCOME_NOTE, OUTCOME_UNCHANGED, OUTCOME_NONE, OUTCOME_RETIRED, OUTCOME_REFUSED)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AnalystRead(BaseModel):
    id = BigAutoField(primary_key=True)
    season = CharField(max_length=7)
    as_of_date = DateField()
    player = ForeignKeyField(Player, backref="analyst_reads", on_delete="CASCADE", column_name="player_id")
    reasons = BinaryJSONField()
    fingerprint = CharField(max_length=64)
    outcome = CharField(max_length=12)
    summary = TextField(null=True)
    note = ForeignKeyField(ProjectionAdjustment, null=True, backref="analyst_reads",
                           on_delete="SET NULL", column_name="note_id")
    disagrees = TextField(null=True)
    proposal = BinaryJSONField(null=True)
    model = CharField(max_length=64, null=True)
    prompt_version = CharField(max_length=16, null=True)
    input_tokens = IntegerField(default=0)
    output_tokens = IntegerField(default=0)
    cost_micros = BigIntegerField(default=0)
    created_at = DateTimeField(default=_now)
    pipeline_run_id = UUIDField(null=True)

    class Meta:
        table_name = "analyst_reads"
        schema = "nba"

    def __repr__(self) -> str:
        return (f"<AnalystRead(id={self.id}, player_id={self.player_id}, "
                f"as_of_date={self.as_of_date}, outcome='{self.outcome}')>")

    @classmethod
    def settled_fingerprints(cls, season: str, player_ids: Iterable[int]) -> dict[int, str]:
        """Each player's fingerprint at his newest read that came to an answer."""
        ids = list(player_ids)
        if not ids:
            return {}
        found: dict[int, str] = {}
        rows = (
            cls.select(cls.player, cls.fingerprint)
            .where((cls.season == season) & cls.player.in_(ids) & cls.outcome.in_(SETTLED))
            .order_by(cls.created_at.desc(), cls.id.desc())
            .tuples()
        )
        for player_id, fingerprint in rows:
            found.setdefault(player_id, fingerprint)
        return found

    @classmethod
    def of_day(cls, season: str, day) -> list["AnalystRead"]:
        """The reads of one morning, newest first."""
        return list(
            cls.select()
            .where((cls.season == season) & (cls.as_of_date == day))
            .order_by(cls.created_at.desc(), cls.id.desc())
        )
