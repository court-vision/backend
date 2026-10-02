"""
The router's answer: the schema the model must fill, and everything the server
checks before an answer reaches the client (docs/AI_PHASE1_PLAN.md § 3).

The model picks a destination; it is never trusted with one. It names players
and the server finds them, so a player ID is never the model's to get wrong;
a fantasy team must belong to the caller; and a StatMuse link is built here
from the model's question -- the model never supplies a URL. A target that fails any check turns the answer
into `cannot` with `gap="invalid_target"`, which is logged as a bug signal:
the question log keeps the target that was refused and why.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Optional, get_args

from peewee import fn
from pydantic import Field, PrivateAttr, ValidationError, field_validator

from core.errors import ProviderError
from core.logging import get_logger
from core.season import previous_season
from core.settings import settings
from db.base import db_operation
from db.models.nba.player_season_stats import PlayerSeasonStats
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
MAX_NAME = 80
MAX_LISTED = 4      # players named in a "which one?" reply
_CANDIDATES = 80    # rows read per name; the most common surname has about twenty
INVALID_TARGET_TEXT = "I couldn't find the right place for that. Try asking another way."

# Gaps the model may name; the other two are the server's alone
_MODEL_GAPS = [gap for gap in get_args(GapKind) if gap not in ("invalid_target", "ambiguous")]


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
            # Names, not IDs: the server finds the players (`validate`)
            "player": _nullable({"type": "string"}),
            "compare": {"type": "array", "items": {"type": "string"}},
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
    # The player names the model gave for a terminal target: the focus, then the
    # comparison. `validate` turns them into the target's IDs. Private for the
    # same reason -- a name is the model's wording, not something to send on.
    _player: Optional[str] = PrivateAttr(default=None)
    _compare: list[str] = PrivateAttr(default_factory=list)

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
    """Slips the schema cannot rule out and the server can put right: "L15" is
    `l15`, a games minimum outside 1..82 is no minimum, and a category filter
    means the categories format -- /rankings ignores `cats` in any other, so
    "top shot blockers" would open plain points rankings."""
    window = target.get("window")
    if target.get("type") == "terminal" and isinstance(window, str):
        target["window"] = window.strip().lower() or None
    rankings = target.get("rankings")
    if isinstance(rankings, dict):
        games = rankings.get("min_games")
        if isinstance(games, int) and not 1 <= games <= SEASON_GAMES:
            rankings["min_games"] = None
        if rankings.get("cats"):
            rankings["format"] = "categories"


def _take_names(target: dict[str, Any]) -> tuple[Optional[str], list[str]]:
    """Lift the player names out of a terminal target, leaving the ID fields the
    response model has for `validate` to fill in."""
    if target.get("type") != "terminal":
        return None, []

    def clean(value: Any) -> Optional[str]:
        return value.strip()[:MAX_NAME] or None if isinstance(value, str) else None
    player = clean(target.pop("player", None))
    raw = target.pop("compare", None)
    compare = [name for name in map(clean, raw if isinstance(raw, list) else []) if name]
    target.update(player_id=None, compare_ids=[])
    return player, list(dict.fromkeys(compare))


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
    player: Optional[str] = None
    compare: list[str] = []
    if isinstance(target, dict):
        _tidy(target)
        player, compare = _take_names(target)
    try:
        answer = RouterAnswer.model_validate(raw)
    except ValidationError as exc:
        fields = [".".join(str(part) for part in error["loc"]) for error in exc.errors()]
        if isinstance(target, dict) and all(field.split(".")[0] == "target" for field in fields):
            return _invalid(", ".join(fields), {**target, "player": player, "compare": compare})
        raise _unreadable(exc, fields) from exc
    answer._player, answer._compare = player, compare
    return answer


def statmuse_url(query: str) -> Optional[str]:
    """A StatMuse ask URL for a question: accents folded, lower case, anything
    but letters and digits collapsed to single hyphens. Whatever the model wrote,
    the result can only ever be a search on StatMuse."""
    folded = unicodedata.normalize("NFKD", query).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")[:MAX_QUERY].strip("-")
    return f"{STATMUSE_ASK}{slug}" if slug else None


_NUMBER = re.compile(r"\d+(?:\.\d+)?")
# A season label ("2025-26"), but not the front of a date ("2026-10-20")
_SEASON = re.compile(r"(?<![\d-])\d{4}-\d{2}(?![\d-])")


def ungrounded_numbers(
    text: str,
    question: str,
    target: Optional[AiTarget],
    names: Iterable[str] = (),
    *,
    statmuse_query: Optional[str] = None,
    season_line: str = "",
) -> int:
    """Numbers in the answer line that appear neither in the question nor in the
    target (a window, an ID). The router states no statistics, so anything else
    is a number it made up. Names the lookups returned are removed first -- a
    team called "Lvl. 3 Goblins" is not a statistic -- but only where they stand
    alone: a league called "1" is not the 1 in "15".

    Two more things the line may repeat: a season the request itself named (the
    season line's, or the StatMuse question's), and any other number in the
    StatMuse question, which the user sees as the link anyway. A season is
    removed whole, so naming 2025-26 does not excuse a made-up 26 elsewhere.

    Logged, not blocked; the eval is where it fails."""
    text = text.replace("\u2013", "-")  # "2025\u201326" is the same season
    query = statmuse_query or ""
    seasons = set(_SEASON.findall(season_line)) | set(_SEASON.findall(query))
    for phrase in sorted({n for n in names if n} | seasons, key=len, reverse=True):
        text = re.sub(rf"(?<!\d){re.escape(phrase)}(?!\d)", " ", text, flags=re.IGNORECASE)
    allowed = set(_NUMBER.findall(question)) | set(_NUMBER.findall(_SEASON.sub(" ", query)))
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


async def describe_view(context: AiContext) -> tuple[dict[str, Any], dict[int, str]]:
    """The caller's view for the model, and the players in it by ID.

    The model sees players by name only: it names players back and the server
    finds them, so an ID in the view would be nothing but something to copy
    wrong. The second value is how `validate` recognises the view's own players
    without asking the database again.

    Names come from nba.players / nba.teams only -- never from league data --
    so they add no prompt-injection surface. A fantasy team stays an ID; the
    model can ask get_my_teams for its name.
    """
    ids = [i for i in [context.player_id, *context.compare_ids] if i is not None]
    names, nba_team_name = await _view_names(ids, context.nba_team) if (ids or context.nba_team) else ({}, None)
    view: dict[str, Any] = {"page": context.page, "mode": context.mode, "team_id": context.team_id,
                            "window": context.window, "player": names.get(context.player_id)}
    compared = [names[i] for i in context.compare_ids if i in names]
    if compared:
        view["compare"] = compared
    if context.nba_team:
        view["nba_team"] = {"abbrev": context.nba_team, "name": nba_team_name}
    return {key: value for key, value in view.items() if value is not None}, names


_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv"})


def _fold(name: str) -> tuple[str, ...]:
    """A name as comparable words: accents folded, lower case, apostrophes and
    periods dropped ("De'Aaron", "P.J."), anything else that isn't a letter or
    digit a space, and generational suffixes gone -- "Jimmy Butler" and "Jimmy
    Butler III" are one name."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    words = re.sub(r"[^a-z0-9]+", " ", re.sub(r"['.]", "", folded)).split()
    return tuple(word for word in words if word not in _SUFFIXES)


def _by_relevance(hits: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Most fantasy points first, from this season's rows or, before it has any,
    last season's -- so a "which one?" reply leads with the players people mean."""
    ids = [player_id for player_id, _ in hits]
    points: dict[int, float] = {}
    for season in (settings.nba_season, previous_season(settings.nba_season)):
        rows = PlayerSeasonStats.latest_per_player(season).where(PlayerSeasonStats.player.in_(ids))
        points = {row.player_id: row.fpts or 0 for row in rows}
        if points:
            break
    return sorted(hits, key=lambda hit: (-points.get(hit[0], 0), hit[1], hit[0]))


def _pick(wanted: tuple[str, ...], candidates: list[tuple[int, str]]) -> tuple[list[tuple[int, str]], bool]:
    """The candidates a name means, and whether they matched it whole.

    The whole name beats part of one, so "Jalen Williams" is one player though
    "Williams" is many. Failing both, a word may be the start of one ("Steph
    Curry" is Stephen), as long as it is long enough to mean something.
    """
    folded = [(player, _fold(player[1])) for player in candidates]
    whole = [player for player, words in folded if words == wanted]
    if whole:
        return whole, True
    part = [player for player, words in folded if set(wanted) <= set(words)]
    if part:
        return part, False
    if all(len(word) >= 3 for word in wanted):
        return [player for player, words in folded
                if all(any(have.startswith(word) for have in words) for word in wanted)], False
    return [], False


@db_operation("ai.route_find_players")
def _find_players(names: list[str]) -> dict[str, list[tuple[int, str]]]:
    """Each name's players in nba.players as (id, name), best first. One hit is
    the player; several means the name alone doesn't say which."""
    # The stored name with its punctuation squeezed out, to be searched for one word of the name
    squeezed = fn.regexp_replace(fn.unaccent(Player.name_normalized), "[^a-z0-9 ]", "", "g")
    found: dict[str, list[tuple[int, str]]] = {}
    for name in names:
        wanted = _fold(name)
        if not wanted:
            found[name] = []
            continue
        # Any player the name could mean has its longest word, or something starting with it
        rows = Player.select(Player.id, Player.name).where(squeezed.contains(max(wanted, key=len))).limit(_CANDIDATES)
        hits, whole = _pick(wanted, [(row.id, row.name) for row in rows])
        if len(hits) > 1:
            # Two rows with one whole name are the same name twice: take the one who plays
            hits = _by_relevance(hits)[:1] if whole else _by_relevance(hits)
        found[name] = hits
    return found


@dataclass(frozen=True)
class _Found:
    players: frozenset[int]
    owned_teams: frozenset[int]
    nba_teams: frozenset[str]
    # The found NBA teams' names, for the number check: no tool returns them
    nba_team_names: frozenset[str] = frozenset()


@db_operation("ai.route_ids")
def _lookup(player_ids: list[int], team_ids: list[int], nba_teams: list[str], user_id: int) -> _Found:
    players = {row.id for row in Player.select(Player.id).where(Player.id.in_(player_ids))} if player_ids else set()
    owned = (
        {row.team_id for row in Team.select(Team.team_id).where(Team.team_id.in_(team_ids), Team.user_id == user_id)}
        if team_ids else set()
    )
    nba = (
        {row.id: row.name for row in NBATeam.select(NBATeam.id, NBATeam.name).where(NBATeam.id.in_(nba_teams))}
        if nba_teams else {}
    )
    return _Found(frozenset(players), frozenset(owned), frozenset(nba), frozenset(nba.values()))


def _invalid(reason: str, target: AiTarget | dict[str, Any] | None = None) -> RouterAnswer:
    """The `cannot` a refused destination becomes. What was refused, and why, ride
    along for the question log -- the eval reads them there; the user never does."""
    get_logger().warning("ai_route_invalid_target", reason=reason)
    answer = RouterAnswer(kind="cannot", text=INVALID_TARGET_TEXT, gap="invalid_target",
                          missing=f"rejected: {reason}")
    answer._rejected_target = target.model_dump() if isinstance(target, ApiModel) else target
    return answer


def _unknown_player(name: str, refused: dict[str, Any]) -> RouterAnswer:
    answer = RouterAnswer(kind="cannot", text=f"I couldn't find a player called {name}.", gap="no_data",
                          missing=f"player not found: {name}")
    answer._rejected_target = refused
    return answer


def _ambiguous_player(name: str, hits: list[tuple[int, str]], refused: dict[str, Any]) -> RouterAnswer:
    """Ask which player, in words the server wrote: the model never saw the candidates."""
    listed = [player_name for _, player_name in hits[:MAX_LISTED]]
    options = (", ".join(listed) + " or someone else" if len(hits) > MAX_LISTED
               else ", ".join(listed[:-1]) + " or " + listed[-1])
    answer = RouterAnswer(kind="cannot", text=f"Which {name} do you mean: {options}?", gap="ambiguous",
                          suggestions=[f"Show me {player_name}" for player_name in listed],
                          missing=f"ambiguous player: {name} ({len(hits)} matches)")
    answer._rejected_target = refused
    return answer


def _check(answer: RouterAnswer, found: _Found, players: dict[str, list[tuple[int, str]]]) -> RouterAnswer:
    """Normalize a parsed answer against what exists, or turn it into `cannot`.
    `players` is what each name the answer gave matched, best first."""
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
            # What was asked for, names included, for the question log if it is turned away
            refused = {**target.model_dump(), "player": answer._player, "compare": answer._compare}
            if not answer._player:
                return _invalid("player", refused)
            ids: list[int] = []
            for name in [answer._player, *answer._compare]:
                hits = players.get(name, [])
                if not hits:
                    return _unknown_player(name, refused)
                if len(hits) > 1:
                    return _ambiguous_player(name, hits, refused)
                ids.append(hits[0][0])
            compare = list(dict.fromkeys(i for i in ids[1:] if i != ids[0]))
            if len(compare) > MAX_COMPARE:
                return _invalid("compare", refused)
            update.update(player_id=ids[0], compare_ids=compare)
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


async def validate(
    answer: RouterAnswer,
    *,
    user_id: int,
    names: Optional[set[str]] = None,
    view_players: Optional[dict[int, str]] = None,
) -> RouterAnswer:
    """Find the players the answer names and check its other IDs, then normalize it.

    `view_players` are the players on the caller's screen (`describe_view`): a
    name that is one of theirs is that player, with no lookup. `names` collects
    the name of an NBA team the target points at. The model can pick one that no
    lookup named, and the 76 in "76ers" is not a statistic.
    """
    target = answer.target if answer.kind == "show" else None
    wanted: list[str] = []
    team_ids: list[int] = []
    nba_teams: list[str] = []
    if isinstance(target, TerminalTarget):
        if target.mode == "player":
            wanted = [name for name in [answer._player, *answer._compare] if name]
        team_ids = [target.team_id] if target.team_id is not None else []
        nba_teams = [target.nba_team.upper()] if target.nba_team else []
    elif isinstance(target, PageTarget) and target.team_id is not None:
        team_ids = [target.team_id]

    on_screen = {_fold(name): (player_id, name) for player_id, name in (view_players or {}).items()}
    players = {name: [on_screen[_fold(name)]] for name in wanted if _fold(name) in on_screen}
    unseen = [name for name in wanted if name not in players]
    if unseen:
        players.update(await _find_players(unseen))
    found = (await _lookup([], team_ids, nba_teams, user_id) if team_ids or nba_teams
             else _Found(frozenset(), frozenset(), frozenset()))
    if names is not None:
        names |= found.nba_team_names
    return _check(answer, found, players)
