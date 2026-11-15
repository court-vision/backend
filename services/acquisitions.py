"""
ESPN's limits on adds (acquisitions), and what a team has used.

The rules are in mSettings `settings.acquisitionSettings`:

    matchupAcquisitionLimit       adds per matchup, or per game day of the matchup when
    matchupLimitPerScoringPeriod  is true; -1 = no limit
    acquisitionLimit              adds per season; -1 = no limit
    waiverHours                   how long a dropped player sits on waivers

A per-scoring-period limit is still a matchup total, not a daily one: ESPN gives that many adds
for each day of the matchup with at least one NBA game and lets the team use them on any day (a
week with a day without games has 6, the merged All-Star matchup more; read off James's league
2026-11-15, where one team logged three adds on one day). ESPN sends no total: it is the limit
times the matchup's game days, counted on ESPN's own pro schedule (`proTeamSchedules_wl`), with
our calendar standing in when that can't be read. Until the matchup's days are known the total
is None, which refuses nothing.

The counts are in mTeam `teams[].transactionCounter`: `acquisitions` (the season) and
`matchupAcquisitionTotals` ({matchupPeriodId: adds}). Pre-season adds do not count (seen
2026-10-08: six pre-season adds, the counter's `acquisitions` 0).

The code ESPN refuses an over-limit add with is unconfirmed, so `is_limit_refusal` recognises the
refusal by its words, and `budget_block` only refuses what is certain.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any, Iterable, Mapping, Optional

from core.logging import get_logger
from schemas.lineup_editor import AcquisitionState

log = get_logger("acquisitions")

PRO_SCHEDULE_ENDPOINT = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/{}"
PRO_SCHEDULE_TTL_S = 6 * 3600        # ESPN adds games as the NBA publishes them (the Cup's December week)
PRO_SCHEDULE_RETRY_S = 15 * 60       # after a failed read, the calendar stands in for this long
_pro_schedule: dict[int, tuple[float, Optional[frozenset[int]]]] = {}


def _limit(value: Any) -> Optional[int]:
    """ESPN's -1 (or nothing usable) is no limit."""
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _hours(value: Any) -> Optional[int]:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def parse_acquisitions(payload: Mapping[str, Any], team: Mapping[str, Any]) -> Optional[AcquisitionState]:
    """The league's add limits and `team`'s counts from one mSettings + mTeam read; None when
    the league sends no acquisition settings. A per-game-day limit has no matchup total yet:
    `with_game_days` adds it once the matchup's days are known."""
    rules = (payload.get("settings") or {}).get("acquisitionSettings")
    if not isinstance(rules, Mapping):
        return None
    counter = team.get("transactionCounter") or {}
    matchup = (payload.get("status") or {}).get("currentMatchupPeriod")
    totals = counter.get("matchupAcquisitionTotals") or {}
    limit = _limit(rules.get("matchupAcquisitionLimit", -1))
    per_game_day = bool(rules.get("matchupLimitPerScoringPeriod"))
    return AcquisitionState(
        per="game_day" if per_game_day else "matchup",
        limit=None if per_game_day else limit,
        per_game_day=limit if per_game_day else None,
        matchup_period_id=int(matchup) if matchup else None,
        matchup_used=int(totals.get(str(matchup), 0) or 0) if matchup else 0,
        season_limit=_limit(rules.get("acquisitionLimit", -1)),
        season_used=int(counter.get("acquisitions") or 0),
        waiver_hours=_hours(rules.get("waiverHours")),
    )


def with_game_days(acq: AcquisitionState, game_days: Optional[int]) -> AcquisitionState:
    """A per-game-day limit with its matchup total: the limit for each of the matchup's
    `game_days`. Unchanged for a per-matchup limit, or when the game days are unknown."""
    if acq.per != "game_day" or acq.per_game_day is None or game_days is None:
        return acq
    return acq.model_copy(update={"game_days": game_days, "limit": acq.per_game_day * game_days})


def parse_pro_schedule(payload: Mapping[str, Any]) -> frozenset[int]:
    """ESPN's scoring periods (days) with at least one NBA game, from `proTeamSchedules_wl`."""
    days: set[int] = set()
    for team in (payload.get("settings") or {}).get("proTeams") or []:
        for period, games in (team.get("proGamesByScoringPeriod") or {}).items():
            if games:
                try:
                    days.add(int(period))
                except (TypeError, ValueError):
                    continue
    return frozenset(days)


async def espn_game_days(year: int) -> Optional[frozenset[int]]:
    """The season's ESPN days with games, from ESPN's public pro schedule, kept for a few hours;
    None when it can't be read (or came back empty)."""
    now = time.monotonic()
    cached = _pro_schedule.get(year)
    if cached is not None and now < cached[0]:
        return cached[1]
    from services.providers.http import provider_get   # the parse above stays import-light

    try:
        payload = await provider_get("espn", PRO_SCHEDULE_ENDPOINT.format(year),
                                     params={"view": ["proTeamSchedules_wl"]}, expect_key="settings")
        days: Optional[frozenset[int]] = parse_pro_schedule(payload) or None
    except Exception as exc:
        log.warning("acquisitions_pro_schedule_unavailable", year=year, error=str(exc))
        days = None
    _pro_schedule[year] = (now + (PRO_SCHEDULE_TTL_S if days else PRO_SCHEDULE_RETRY_S), days)
    return days


async def matchup_game_days(year: int, start: date, end: date) -> int:
    """How many days from `start` through `end` have at least one NBA game: ESPN's own count when
    its schedule can be read, else our calendar's (the two agreed day for day on 2026-11-15)."""
    from services import schedule_service

    espn = await espn_game_days(year)
    if espn is not None:
        opening = schedule_service.get_season_bounds().opening_night
        first, last = (start - opening).days + 1, (end - opening).days + 1
        return sum(1 for period in range(first, last + 1) if period in espn)
    return schedule_service.days_with_games(start, end)


def matchup_room(acq: AcquisitionState) -> Optional[int]:
    """Adds left this matchup; None when the matchup has no limit (or its total isn't known)."""
    if acq.limit is None:
        return None
    return max(0, acq.limit - acq.matchup_used)


def season_room(acq: AcquisitionState) -> Optional[int]:
    """Adds left this season; None when the season has no cap."""
    if acq.season_limit is None:
        return None
    return max(0, acq.season_limit - acq.season_used)


def in_current_matchup(acq: AcquisitionState, day: date) -> bool:
    """Whether `day` is one of the current matchup's days (False when they are unknown)."""
    return acq.matchup_start is not None and acq.matchup_end is not None and acq.matchup_start <= day <= acq.matchup_end


def budget_block(acq: Optional[AcquisitionState], day: date, *, pending: Iterable[date] = ()) -> Optional[str]:
    """Why an add for ESPN day `day` (a calendar date) certainly cannot count, as a sentence; None
    when it may. `pending` are the days of the team's pickups still to be made, which draw on the
    same adds (callers that pass none check the counter alone).

    Certain only: the season is used up; the matchup's adds are used up and `day` is in the
    current matchup (an add for a later matchup's day counts toward that one)."""
    if acq is None:
        return None
    days = list(pending)
    room = season_room(acq)
    if room is not None and room - len(days) <= 0:
        return f"Every add the league allows this season is used ({acq.season_used} of {acq.season_limit})" + (
            f", with {len(days)} more scheduled" if days and room > 0 else "")
    room = matchup_room(acq)
    if room is not None and in_current_matchup(acq, day):
        waiting = sum(1 for d in days if in_current_matchup(acq, d))
        if room - waiting <= 0:
            return f"Every add this matchup allows is used ({acq.matchup_used} of {acq.limit})" + (
                f", with {waiting} more scheduled" if waiting and room > 0 else "")
    return None


def is_limit_refusal(code: Optional[str], message: Optional[str]) -> bool:
    """ESPN refused an add over its acquisition limit. The code is unconfirmed, so the words decide;
    "LIMIT" alone is not enough (TRAN_ROSTER_POSITION_LIMIT_EXCEEDED is a position cap)."""
    return "ACQUISITION" in (code or "").upper() or "acquisition" in (message or "").lower()
