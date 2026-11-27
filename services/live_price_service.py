"""
The live prices a Week desk decision reads (the Live rankings plan, step 8).

    prices(scoring, nba_ids, espn_ids=...)  ->  LivePrices, or None

One read of the morning's live projection (`services.live_projection.load`) and
the season calendar, kept a few minutes per process, then each player asked for
priced under the league's scoring (`services.live_prices.price`).

None means "decide as before": a league scored by categories (a stream there is
worth what the matchup's open categories make it, which no single number knows),
a league that scores double-doubles (the projection carries no such line), no
snapshot fresh enough, or no calendar. A player the snapshot does not carry is
simply absent, and his row keeps the value it had. So nothing here can fail a
board: every caller falls back to the stored averages it read before.

Sync: call through `run_db`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING, Iterable, Mapping, Optional

from core.logging import get_logger
from core.settings import settings
from core.sync_cache import SyncTTLCache
from services import live_projection, schedule_service
from services.live_prices import LivePrice, price

if TYPE_CHECKING:
    from services.scoring.resolver import ResolvedScoring

log = get_logger("live_prices")

# The projection is written once a morning (and once more when the analyst's
# notes land), so a few minutes is as stale as a board can read it
CACHE_TTL_SECONDS = 300.0
_CACHE = SyncTTLCache("live_price_inputs", ttl_seconds=CACHE_TTL_SECONDS, max_entries=4)

# How far ahead a player is priced day by day: past the longest matchup a league
# plays (a two-week playoff round, the merged All-Star week)
REACH_DAYS = 21


@dataclass(frozen=True)
class _Inputs:
    """What every league's prices are built from, for one fantasy day."""

    snapshot: Optional[live_projection.LiveSnapshot]
    game_days: Mapping[str, tuple[date, ...]]      # each NBA team's game days from the day on
    nba_of: Mapping[int, int] = field(default_factory=dict)   # ESPN id -> NBA id, the snapshot's players


@dataclass(frozen=True)
class LivePrices:
    """The players asked for, priced from `today` on, by NBA id."""

    as_of: date                                    # the morning the projection was built
    today: date
    by_id: Mapping[int, LivePrice] = field(default_factory=dict)
    nba_of: Mapping[int, int] = field(default_factory=dict)   # the ESPN ids asked for -> NBA id

    def get(self, nba_id: Optional[int]) -> Optional[LivePrice]:
        return self.by_id.get(nba_id) if nba_id is not None else None

    def of_espn(self, espn_id: Optional[int]) -> Optional[LivePrice]:
        return self.get(self.nba_of.get(espn_id)) if espn_id is not None else None

    def season_by_espn_id(self) -> dict[int, float]:
        """Each ESPN id's rest-of-season price: expected points per game on his team's schedule."""
        out: dict[int, float] = {}
        for espn_id, nba_id in self.nba_of.items():
            found = self.by_id.get(nba_id)
            if found is not None and found.season.per_game is not None:
                out[espn_id] = found.season.per_game
        return out


def _game_days(today: date) -> dict[str, tuple[date, ...]]:
    out: dict[str, list[date]] = {}
    for week in schedule_service.iter_weeks():
        start, span = week["start_date"], int(week["game_span"])
        for team, played in (week["games"] or {}).items():
            for key, on in (played or {}).items():
                if not on or int(key) >= span:
                    continue
                day = start + timedelta(days=int(key))
                if day >= today:
                    out.setdefault(str(team), []).append(day)
    return {team: tuple(sorted(set(days))) for team, days in out.items()}


def _load(today: date) -> _Inputs:
    try:
        game_days = _game_days(today)
    except FileNotFoundError:
        return _Inputs(None, {})
    snapshot = live_projection.load(settings.nba_season, today)
    nba_of = {p.espn_id: pid for pid, p in snapshot.players.items() if p.espn_id is not None} if snapshot else {}
    return _Inputs(snapshot, game_days, nba_of)


def _inputs(today: date) -> _Inputs:
    return _CACHE.get_or_build((settings.nba_season, today), lambda: _load(today))


def available(today: Optional[date] = None) -> bool:
    """Whether there is a projection fresh enough to price on. A caller that has yet to
    resolve the league's scoring asks this first, and reads nothing more when there is none."""
    return _inputs(today or schedule_service.get_nba_today()).snapshot is not None


def supported(scoring: "ResolvedScoring") -> bool:
    """Whether a league's decisions can read the live prices (see the module docstring)."""
    return not scoring.is_categories and not scoring.points.uses_game_only_stats


def prices(scoring: "ResolvedScoring", nba_ids: Iterable[Optional[int]] = (), *,
           espn_ids: Iterable[Optional[int]] = (), today: Optional[date] = None) -> Optional[LivePrices]:
    """Each player asked for that the morning's projection carries, by NBA id or
    ESPN id, priced from `today` on (the fantasy day, by default) under
    `scoring`. None: decide as before."""
    if not supported(scoring):
        return None
    today = today or schedule_service.get_nba_today()
    inputs = _inputs(today)
    snapshot = inputs.snapshot
    if snapshot is None:
        return None
    nba_of = {e: inputs.nba_of[e] for e in espn_ids if e is not None and e in inputs.nba_of}
    reach = today + timedelta(days=REACH_DAYS)
    score = scoring.points.score
    out: dict[int, LivePrice] = {}
    for nba_id in {i for i in nba_ids if i is not None} | set(nba_of.values()):
        player = snapshot.players.get(nba_id)
        if player is None or not player.team:
            continue
        found = price(player, score, inputs.game_days.get(player.team, ()), reach)
        if found is not None:
            out[nba_id] = found
    return LivePrices(as_of=snapshot.as_of, today=today, by_id=out, nba_of=nba_of)


def prices_soft(scoring: "ResolvedScoring", nba_ids: Iterable[Optional[int]] = (), *,
                espn_ids: Iterable[Optional[int]] = (), today: Optional[date] = None) -> Optional[LivePrices]:
    """`prices`, or None when the read fails: a board is never failed by its prices."""
    try:
        return prices(scoring, nba_ids, espn_ids=espn_ids, today=today)
    except Exception as exc:
        log.warning("live_prices_unavailable", error=f"{type(exc).__name__}: {exc}")
        return None


def clear_cache() -> None:
    _CACHE.clear()
