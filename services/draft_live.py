"""What the stored daily boards and the morning's projection add to a Draft lab
board in season (the Live rankings plan, step 6).

A board opened in season is already valued on the morning's live projection.
What it could not say is how a player got to his rank: which way he moved, why,
and how fresh the ESPN rank beside him is. Three reads answer that, none new:

- **Moves** come from nba.daily_rankings, the two standard boards stored every
  morning: places gained since the previous stored morning and since about a
  week ago, measured between two stored boards exactly as Scout's Rest of
  season board measures them (`services.live_rankings_service`). They are moves
  on the standard board of the room's format, points or nine categories. A room
  with scoring of its own ranks players a little differently, and a board for
  its scoring was never stored, so the standard board's move is the one shown.
- **ESPN's rank that morning** comes from the same table (`source='espn'`): the
  rank ESPN publishes today, and the stored day it became that. The preseason
  market snapshot stops on Oct 31; in season the board reads ESPN's rank from
  here, so the number beside Court Vision's is the one ESPN's own room shows
  today, with its age. ADP has no daily read and stays the snapshot's.
- **A reason** comes from what the live projection recorded about each player
  (`raw.parts` on his nba.player_projections row): an absence with its return
  date, a note in force, the minutes he has been playing lately.

Only `source='cv'` and `source='espn'` are read: the boards graded in shadow
are never shown here either.

`load` is the one read, cached for a few minutes per process like the public
routes over the same table: a room refetches its board on every pick, and the
table changes once a morning. Everything else is pure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from types import MappingProxyType
from typing import Iterable, Mapping, Optional, Sequence

from core.sync_cache import SyncTTLCache
from db.models.nba.daily_rankings import BOARD_CATEGORIES, BOARD_POINTS, SOURCE_CV, SOURCE_ESPN, DailyRanking
from services.live_rankings_service import BOARDS, DAY_REACH, compared_days, espn_age, move, stored_days

# The table is written once a morning by another service, so a few minutes is
# as stale as a room can see it: the public routes' own TTL
CACHE_TTL_SECONDS = 300.0
_CACHE = SyncTTLCache("draft_live_ranks", ttl_seconds=CACHE_TTL_SECONDS, max_entries=8)

# A minutes trend is offered as the reason for a move only when both are plain:
# he moved at least this many places, and has been playing at least this many
# minutes a game more or fewer lately than over the season
REASON_MOVE = 5
REASON_MINUTES = 3.0


@dataclass(frozen=True)
class EspnNow:
    """ESPN's rank for one player on the morning read, and how long it has stood."""

    rank: int
    value: Optional[float]              # its auction value that morning
    since: date                         # the stored day the rank became this
    since_first_stored: bool            # the run reaches the first stored day: at least this old


@dataclass(frozen=True)
class BoardRanks:
    """One standard board's stored facts, by player id."""

    # Places gained since the previous stored board, and since the one about a week back
    moves: Mapping[int, tuple[Optional[int], Optional[int]]] = field(default_factory=dict)
    espn_as_of: Optional[date] = None   # the morning of ESPN's ranks; None when none is near enough
    espn: Mapping[int, EspnNow] = field(default_factory=dict)


@dataclass(frozen=True)
class StoredRanks:
    """Both standard boards' stored facts for a board counting from one day."""

    as_of: Optional[date] = None        # the stored board the moves are measured to
    day: Optional[date] = None          # the two earlier boards they are measured from
    week: Optional[date] = None
    boards: Mapping[str, BoardRanks] = field(default_factory=dict)

    def board(self, categories: bool) -> BoardRanks:
        return self.boards.get(BOARD_CATEGORIES if categories else BOARD_POINTS) or BoardRanks()


def near(stored: Optional[date], day: date) -> Optional[date]:
    """`stored` when a board counting from `day` may read it: not after it, and
    not more than `DAY_REACH` days before. Older than that the morning job has
    been failing, and a move or an age read off it would describe last week."""
    if stored is None or stored > day or (day - stored).days > DAY_REACH:
        return None
    return stored


def moves_of(
    now: Mapping[int, int], day: Mapping[int, int], week: Mapping[int, int],
) -> dict[int, tuple[Optional[int], Optional[int]]]:
    """Every ranked player's move over a day and a week; None where he was not ranked then."""
    return {pid: (move(day.get(pid), rank), move(week.get(pid), rank)) for pid, rank in now.items()}


def espn_now(
    rows: Iterable[tuple[int, date, int, Optional[float]]], stored: Sequence[date], as_of: date,
) -> dict[int, EspnNow]:
    """ESPN's rank on `as_of` for every player it ranks that day, with its age.

    `rows` are one ESPN board's `(player, day, rank, value)` up to `as_of`, and
    `stored` the days that board was stored at all. A player it does not rank
    on `as_of` has no entry: a rank ESPN no longer gives him is not his rank.
    """
    history: dict[int, list[tuple[date, int]]] = {}
    value: dict[int, Optional[float]] = {}
    for player_id, day, rank, worth in rows:
        if day > as_of:
            continue
        history.setdefault(player_id, []).append((day, int(rank)))
        if day == as_of:
            value[player_id] = None if worth is None else float(worth)
    out: dict[int, EspnNow] = {}
    for player_id, held in history.items():
        if player_id not in value:
            continue
        held.sort()
        age = espn_age(held, stored)
        out[player_id] = EspnNow(rank=age.rank, value=value[player_id], since=age.unchanged_since,
                                 since_first_stored=age.since_first_stored)
    return out


def _load(season: str, day: date) -> StoredRanks:
    as_of = near(DailyRanking.latest_date(SOURCE_CV, season, before=day + timedelta(days=1)), day)
    before, week = compared_days(as_of, season) if as_of is not None else (None, None)
    espn_stored = stored_days(SOURCE_ESPN, season)
    boards: dict[str, BoardRanks] = {}
    for board in BOARDS:
        moves: dict[int, tuple[Optional[int], Optional[int]]] = {}
        if as_of is not None:
            moves = moves_of(
                DailyRanking.ranks_on(as_of, SOURCE_CV, board),
                DailyRanking.ranks_on(before, SOURCE_CV, board) if before else {},
                DailyRanking.ranks_on(week, SOURCE_CV, board) if week else {},
            )
        days = [d for d in espn_stored.get(board, []) if d <= day]
        espn_as_of = near(days[-1] if days else None, day)
        espn: dict[int, EspnNow] = {}
        if espn_as_of is not None:
            rows = (
                DailyRanking.select(DailyRanking.player, DailyRanking.as_of_date, DailyRanking.rank,
                                    DailyRanking.value)
                .where((DailyRanking.source == SOURCE_ESPN) & (DailyRanking.season == season)
                       & (DailyRanking.board == board) & (DailyRanking.as_of_date <= espn_as_of))
                .tuples()
            )
            espn = espn_now(rows, days, espn_as_of)
        boards[board] = BoardRanks(moves=MappingProxyType(moves), espn_as_of=espn_as_of,
                                   espn=MappingProxyType(espn))
    return StoredRanks(as_of=as_of, day=before, week=week, boards=MappingProxyType(boards))


def load(season: str, day: date) -> Optional[StoredRanks]:
    """The stored boards' facts for a board counting from `day`, or None when
    nothing near enough is stored. Shared between callers: read-only."""
    found = _CACHE.get_or_build((season, day), lambda: _load(season, day))
    if found.as_of is None and not any(b.espn_as_of for b in found.boards.values()):
        return None
    return found


def clear_cache() -> None:
    _CACHE.clear()


# ---- the reason beside a rank ----------------------------------------------------------------

def _day(value: object) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def short_day(day: date) -> str:
    """Dec 8"""
    return f"{day:%b} {day.day}"


def _number(value: object) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _note_reason(note: Mapping, names: Mapping[int, str], today: date) -> Optional[str]:
    """A role note in force, in a line: what it sets and how long it holds."""
    minutes, usage = _number(note.get("minutes")), _number(note.get("usage"))
    if minutes is not None:
        what = f"{minutes:g} minutes a game"
    elif usage is not None and usage != 1.0:
        what = "a bigger share of the offense" if usage > 1.0 else "a smaller share of the offense"
    else:
        return None
    teammate = names.get(note.get("depends_on")) if note.get("depends_on") is not None else None
    until = _day(note.get("until")) or _day(note.get("through"))
    if teammate:
        return f"Note: {what} while {teammate} is out"
    if until is not None and until >= today:
        return f"Note: {what} through {short_day(until)}"
    return f"Note: {what}"


def reason_of(
    parts: Optional[Mapping], today: date, moved: Optional[int] = None,
    names: Optional[Mapping[int, str]] = None,
) -> Optional[str]:
    """One line on why a player is ranked where he is this morning, or None.

    `parts` is what the live projection recorded about him, `moved` the places
    he gained over the week (over the day when no board is a week old), and
    `names` player names, for a note that holds while a teammate is out.

    What is known about an absence comes first, then a note in force, and only
    then the minutes he has been playing lately: offered when he moved plainly
    and the minutes moved the same way, so the line never contradicts the move
    beside it. Facts the projection read, never a guess at a cause.
    """
    if not isinstance(parts, Mapping):
        return None
    known = parts.get("known") if isinstance(parts.get("known"), Mapping) else {}
    noted = " (from a note)" if known.get("note") is not None else ""
    if known.get("out_for_season"):
        return f"Out for the season{noted}"
    back = _day(known.get("return_date"))
    if back is not None and back > today:
        return f"Out until about {short_day(back)}{noted}"
    note = parts.get("note")
    if isinstance(note, Mapping):
        line = _note_reason(note, names or {}, today)
        if line is not None:
            return line
    if known.get("out_next"):
        return "Out of his team's next game"
    lately, season = _number(parts.get("recent_minutes")), _number(parts.get("season_minutes"))
    if moved is None or lately is None or season is None or abs(moved) < REASON_MOVE:
        return None
    gap = lately - season
    if abs(gap) < REASON_MINUTES or (gap > 0) != (moved > 0):
        return None
    return f"Minutes {'up' if gap > 0 else 'down'}: {lately:.1f} lately, {season:.1f} on the season"
