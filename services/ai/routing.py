"""
The router's answer: the schema the model must fill, and everything the server
checks before an answer reaches the client (docs/AI_PHASE1_PLAN.md § 3).

The model picks a destination; it is never trusted with one. Every ID in a
target is checked against the database, a fantasy team must belong to the
caller, and a StatMuse link is built here from the model's question -- the
model never supplies a URL. A target that fails any check turns the answer
into `cannot` with `gap="invalid_target"`, which is logged as a bug signal:
the question log keeps the target that was refused and why.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Optional, get_args

from pydantic import Field, PrivateAttr, ValidationError, field_validator

from core.errors import ProviderError
from core.logging import get_logger
from db.base import db_operation
from db.models.nba.players import Player
from db.models.nba.teams import NBATeam
from db.models.teams import Team
from schemas.ai import (
    SEASON_GAMES,
    AiContext,
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
    # For the question log only: the target an invalid_target answer was refused
    # for. Private, so the model cannot write it and no response can carry it.
    _rejected_target: Optional[dict[str, Any]] = PrivateAttr(default=None)

    @property
    def rejected_target(self) -> Optional[dict[str, Any]]:
        return self._rejected_target

    @field_validator("text", "missing")
    @classmethod
    def _clip(cls, value: Optional[str]) -> Optional[str]:
        return value.strip()[:MAX_TEXT] if value is not None else None

    @field_validator("suggestions")
    @classmethod
    def _clip_suggestions(cls, value: list[str]) -> list[str]:
        return [s.strip()[:MAX_TEXT] for s in value if s.strip()][:MAX_SUGGESTIONS]


def _tidy(target: dict[str, Any]) -> None:
    """The two fields ANSWER_SCHEMA cannot bound, where the model's slip is harmless:
    "L15" is `l15`, and a games minimum outside 1..82 is no minimum."""
    window = target.get("window")
    if target.get("type") == "terminal" and isinstance(window, str):
        target["window"] = window.strip().lower() or None
    rankings = target.get("rankings")
    if isinstance(rankings, dict):
        games = rankings.get("min_games")
        if isinstance(games, int) and not 1 <= games <= SEASON_GAMES:
            rankings["min_games"] = None


def _unreadable(exc: Exception, fields: Iterable[str] = ()) -> ProviderError:
    get_logger().warning("ai_route_unparseable", error=type(exc).__name__, fields=list(fields))
    return ProviderError("anthropic", "The assistant returned an answer we couldn't read; try again",
                         error_code="AI_INCOMPLETE")


def parse_answer(text: str) -> RouterAnswer:
    """The final message's JSON as a RouterAnswer. Structured outputs guarantee the
    shape unless the turn was cut short, so an answer that can't be read is the
    model's failure, not ours. What they cannot guarantee is a value's range: a
    `show` whose target is out of range (window "l100") is an invalid target like
    any other, not an unreadable answer."""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _unreadable(exc) from exc
    target = raw.get("target") if isinstance(raw, dict) else None
    if isinstance(target, dict) and raw.get("kind") != "show":
        raw["target"] = target = None  # only a `show` has a destination; see _check
    if isinstance(target, dict):
        _tidy(target)
    try:
        return RouterAnswer.model_validate(raw)
    except ValidationError as exc:
        fields = [".".join(str(part) for part in error["loc"]) for error in exc.errors()]
        if isinstance(target, dict) and all(field.split(".")[0] == "target" for field in fields):
            return _invalid(", ".join(fields), target)
        raise _unreadable(exc, fields) from exc


def statmuse_url(query: str) -> Optional[str]:
    """A StatMuse ask URL for a question: accents folded, lower case, anything
    but letters and digits collapsed to single hyphens. Whatever the model wrote,
    the result can only ever be a search on StatMuse."""
    folded = unicodedata.normalize("NFKD", query).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")[:MAX_QUERY].strip("-")
    return f"{STATMUSE_ASK}{slug}" if slug else None


_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def ungrounded_numbers(
    text: str,
    question: str,
    target: Optional[AiTarget],
    names: Iterable[str] = (),
) -> int:
    """Numbers in the answer line that appear neither in the question nor in the
    target (a window, an ID). The router states no statistics, so anything else
    is a number it made up. Names the lookups returned are removed first -- a
    team called "Lvl. 3 Goblins" is not a statistic. Logged, not blocked; the
    eval is where it fails."""
    for name in sorted({n for n in names if n}, key=len, reverse=True):
        text = re.sub(re.escape(name), " ", text, flags=re.IGNORECASE)
    allowed = set(_NUMBER.findall(question))
    if target is not None:
        allowed |= set(_NUMBER.findall(target.model_dump_json()))
    return sum(1 for number in _NUMBER.findall(text) if number not in allowed)


@db_operation("ai.route_view_names")
def _view_names(player_ids: list[int], nba_team: Optional[str]) -> tuple[dict[int, str], Optional[str]]:
    players = (
        {row.id: row.name for row in Player.select(Player.id, Player.name).where(Player.id.in_(player_ids))}
        if player_ids else {}
    )
    team = NBATeam.get_or_none(NBATeam.id == nba_team) if nba_team else None
    return players, team.name if team else None


async def describe_view(context: AiContext) -> dict[str, Any]:
    """The caller's view for the model: its IDs, with player and NBA team names
    added from our own tables. Without a name the model has only an ID, and
    tries to find out whose it is by searching players at random.

    Names come from nba.players / nba.teams only -- never from league data --
    so they add no prompt-injection surface. A fantasy team stays an ID; the
    model can ask get_my_teams for its name.
    """
    ids = [i for i in [context.player_id, *context.compare_ids] if i is not None]
    names, nba_team_name = await _view_names(ids, context.nba_team) if (ids or context.nba_team) else ({}, None)
    view: dict[str, Any] = {"page": context.page, "mode": context.mode, "team_id": context.team_id,
                            "window": context.window}
    if context.player_id is not None:
        view["player"] = {"id": context.player_id, "name": names.get(context.player_id)}
    if context.compare_ids:
        view["compare"] = [{"id": i, "name": names.get(i)} for i in context.compare_ids]
    if context.nba_team:
        view["nba_team"] = {"abbrev": context.nba_team, "name": nba_team_name}
    return {key: value for key, value in view.items() if value is not None}


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


def _invalid(reason: str, target: AiTarget | dict[str, Any] | None = None) -> RouterAnswer:
    """The `cannot` a refused destination becomes. What was refused, and why, ride
    along for the question log -- the eval reads them there; the user never does."""
    get_logger().warning("ai_route_invalid_target", reason=reason)
    answer = RouterAnswer(kind="cannot", text=INVALID_TARGET_TEXT, gap="invalid_target",
                          missing=f"rejected: {reason}")
    answer._rejected_target = target.model_dump() if isinstance(target, ApiModel) else target
    return answer


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
                return _invalid("player_id", target)
            compare = list(dict.fromkeys(i for i in target.compare_ids if i != target.player_id))
            if any(i not in found.players for i in compare) or len(compare) > MAX_COMPARE:
                return _invalid("compare_ids", target)
            update.update(player_id=target.player_id, compare_ids=compare)
        elif target.mode == "team":
            if target.team_id not in found.owned_teams:
                return _invalid("team_id", target)
            update["team_id"] = target.team_id
        elif target.mode == "nba_team":
            abbrev = (target.nba_team or "").upper()
            if abbrev not in found.nba_teams:
                return _invalid("nba_team", target)
            update["nba_team"] = abbrev
        clean = target.model_copy(update=update)
    else:
        assert isinstance(target, PageTarget)
        if target.team_id is not None and target.team_id not in found.owned_teams:
            return _invalid("team_id", target)
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
