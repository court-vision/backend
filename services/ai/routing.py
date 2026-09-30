"""
The router's answer: the schema the model must fill, and everything the server
checks before an answer reaches the client (docs/AI_PHASE1_PLAN.md § 3).

The model picks a destination; it is never trusted with one. Every ID in a
target is checked against the database, a fantasy team must belong to the
caller, and a StatMuse link is built here from the model's question -- the
model never supplies a URL. A target that fails any check turns the answer
into `cannot` with `gap="invalid_target"`, which is logged as a bug signal.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Optional, get_args

from pydantic import Field, ValidationError, field_validator

from core.errors import ProviderError
from core.logging import get_logger
from db.base import db_operation
from db.models.nba.players import Player
from db.models.nba.teams import NBATeam
from db.models.teams import Team
from schemas.ai import (
    AiTarget,
    AnswerKind,
    GapKind,
    PageTarget,
    RoutablePage,
    TerminalMode,
    TerminalTarget,
)
from schemas.common import ApiModel
from services.scoring.category_rank import RANKABLE_KEYS

STATMUSE_ASK = "https://www.statmuse.com/nba/ask/"
MAX_TEXT = 300
MAX_QUERY = 200
MAX_SUGGESTIONS = 2
MAX_COMPARE = 4
INVALID_TARGET_TEXT = "I couldn't find the right place for that. Try asking another way."

# Gaps the model may name; `invalid_target` is the server's alone
_MODEL_GAPS = [gap for gap in get_args(GapKind) if gap != "invalid_target"]


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    # Every field required and nullable where optional: the model always says
    # every field, which keeps the output shape one thing to validate.
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


# What the model must produce. Hand-written rather than generated so it is
# byte-stable across releases and every construct in it is one structured
# outputs supports (no length or range constraints -- Pydantic enforces those).
ANSWER_SCHEMA: dict[str, Any] = {
    **_object({
        "kind": {"type": "string", "enum": list(get_args(AnswerKind))},
        "text": {"type": "string"},
        "target": {"anyOf": [{"$ref": "#/$defs/terminal"}, {"$ref": "#/$defs/page"}, {"type": "null"}]},
        "statmuse_query": _nullable({"type": "string"}),
        "suggestions": {"type": "array", "items": {"type": "string"}},
        "gap": _nullable({"type": "string", "enum": _MODEL_GAPS}),
        "missing": _nullable({"type": "string"}),
    }),
    "$defs": {
        "terminal": _object({
            "type": {"type": "string", "const": "terminal"},
            "mode": {"type": "string", "enum": list(get_args(TerminalMode))},
            "player_id": _nullable({"type": "integer"}),
            "compare_ids": {"type": "array", "items": {"type": "integer"}},
            "team_id": _nullable({"type": "integer"}),
            "nba_team": _nullable({"type": "string"}),
            "window": _nullable({"type": "string"}),
        }),
        "page": _object({
            "type": {"type": "string", "const": "page"},
            "page": {"type": "string", "enum": list(get_args(RoutablePage))},
            "team_id": _nullable({"type": "integer"}),
            "rankings": {"anyOf": [{"$ref": "#/$defs/rankings"}, {"type": "null"}]},
        }),
        "rankings": _object({
            "scope": _nullable({"type": "string", "enum": ["global", "league"]}),
            "format": _nullable({"type": "string", "enum": ["points", "categories"]}),
            "window": _nullable({"type": "integer", "enum": [7, 14, 30]}),
            "cats": {"type": "array", "items": {"type": "string", "enum": list(RANKABLE_KEYS)}},
            "min_games": _nullable({"type": "integer"}),
        }),
    },
}

ANSWER_FORMAT: dict[str, Any] = {"type": "json_schema", "schema": ANSWER_SCHEMA}


class RouterAnswer(ApiModel):
    """The model's answer, parsed. Loose where trimming is harmless, strict where
    it is not: the target's shape is validated here, its IDs in `validate`."""

    kind: AnswerKind
    text: str
    target: Optional[AiTarget] = None
    statmuse_query: Optional[str] = None
    suggestions: list[str] = Field(default_factory=list)
    gap: Optional[GapKind] = None
    missing: Optional[str] = None

    @field_validator("text", "missing")
    @classmethod
    def _clip(cls, value: Optional[str]) -> Optional[str]:
        return value.strip()[:MAX_TEXT] if value is not None else None

    @field_validator("suggestions")
    @classmethod
    def _clip_suggestions(cls, value: list[str]) -> list[str]:
        return [s.strip()[:MAX_TEXT] for s in value if s.strip()][:MAX_SUGGESTIONS]


def parse_answer(text: str) -> RouterAnswer:
    """The final message's JSON as a RouterAnswer. Structured outputs guarantee the
    shape unless the turn was cut short, so a failure here is the model's, not ours."""
    try:
        return RouterAnswer.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as exc:
        get_logger().warning("ai_route_unparseable", error=type(exc).__name__)
        raise ProviderError("anthropic", "The assistant returned an answer we couldn't read; try again",
                            error_code="AI_INCOMPLETE") from exc


def statmuse_url(query: str) -> Optional[str]:
    """A StatMuse ask URL for a question: accents folded, lower case, anything
    but letters and digits collapsed to single hyphens. Whatever the model wrote,
    the result can only ever be a search on StatMuse."""
    folded = unicodedata.normalize("NFKD", query).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")[:MAX_QUERY].strip("-")
    return f"{STATMUSE_ASK}{slug}" if slug else None


_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def ungrounded_numbers(text: str, question: str, target: Optional[AiTarget]) -> int:
    """Numbers in the answer line that appear neither in the question nor in the
    target (a window, an ID). The router states no statistics, so anything else
    is a number it made up. Logged, not blocked; the eval is where it fails."""
    allowed = set(_NUMBER.findall(question))
    if target is not None:
        allowed |= set(_NUMBER.findall(target.model_dump_json()))
    return sum(1 for number in _NUMBER.findall(text) if number not in allowed)


@dataclass(frozen=True)
class _Found:
    players: frozenset[int]
    owned_teams: frozenset[int]
    nba_teams: frozenset[str]


@db_operation("ai.route_ids")
def _lookup(player_ids: list[int], team_ids: list[int], nba_teams: list[str], user_id: int) -> _Found:
    players = {row.id for row in Player.select(Player.id).where(Player.id.in_(player_ids))} if player_ids else set()
    owned = (
        {row.team_id for row in Team.select(Team.team_id).where(Team.team_id.in_(team_ids), Team.user_id == user_id)}
        if team_ids else set()
    )
    nba = {row.id for row in NBATeam.select(NBATeam.id).where(NBATeam.id.in_(nba_teams))} if nba_teams else set()
    return _Found(frozenset(players), frozenset(owned), frozenset(nba))


def _invalid(reason: str) -> RouterAnswer:
    get_logger().warning("ai_route_invalid_target", reason=reason)
    return RouterAnswer(kind="cannot", text=INVALID_TARGET_TEXT, gap="invalid_target")


def _check(answer: RouterAnswer, found: _Found) -> RouterAnswer:
    """Normalize a parsed answer against what exists, or turn it into `cannot`."""
    if answer.kind == "statmuse":
        query = (answer.statmuse_query or "").strip()
        if not query or len(query) > MAX_QUERY or statmuse_url(query) is None:
            return _invalid("statmuse_query")
        return answer.model_copy(update={"statmuse_query": query, "target": None})

    if answer.kind == "cannot":
        return answer.model_copy(update={"target": None, "statmuse_query": None})

    target = answer.target
    if target is None:
        return _invalid("show_without_target")

    if isinstance(target, TerminalTarget):
        update: dict[str, Any] = {"player_id": None, "compare_ids": [], "team_id": None, "nba_team": None}
        if target.mode == "player":
            if target.player_id not in found.players:
                return _invalid("player_id")
            compare = list(dict.fromkeys(i for i in target.compare_ids if i != target.player_id))
            if any(i not in found.players for i in compare) or len(compare) > MAX_COMPARE:
                return _invalid("compare_ids")
            update.update(player_id=target.player_id, compare_ids=compare)
        elif target.mode == "team":
            if target.team_id not in found.owned_teams:
                return _invalid("team_id")
            update["team_id"] = target.team_id
        elif target.mode == "nba_team":
            abbrev = (target.nba_team or "").upper()
            if abbrev not in found.nba_teams:
                return _invalid("nba_team")
            update["nba_team"] = abbrev
        clean = target.model_copy(update=update)
    else:
        assert isinstance(target, PageTarget)
        if target.team_id is not None and target.team_id not in found.owned_teams:
            return _invalid("team_id")
        clean = target.model_copy(update={"rankings": target.rankings if target.page == "rankings" else None})

    return answer.model_copy(update={"target": clean, "statmuse_query": None})


async def validate(answer: RouterAnswer, *, user_id: int) -> RouterAnswer:
    """Check every ID the answer names in one round trip, then normalize it."""
    target = answer.target if answer.kind == "show" else None
    player_ids: list[int] = []
    team_ids: list[int] = []
    nba_teams: list[str] = []
    if isinstance(target, TerminalTarget):
        player_ids = [i for i in [target.player_id, *target.compare_ids] if i is not None]
        team_ids = [target.team_id] if target.team_id is not None else []
        nba_teams = [target.nba_team.upper()] if target.nba_team else []
    elif isinstance(target, PageTarget) and target.team_id is not None:
        team_ids = [target.team_id]
    found = await _lookup(player_ids, team_ids, nba_teams, user_id)
    return _check(answer, found)
