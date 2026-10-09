"""
ESPN's limits on adds (acquisitions), and what a team has used.

The rules are in mSettings `settings.acquisitionSettings`:

    matchupAcquisitionLimit       adds per matchup, or per scoring period (day) when
    matchupLimitPerScoringPeriod  is true; -1 = no limit
    acquisitionLimit              adds per season; -1 = no limit

The counts are in mTeam `teams[].transactionCounter`: `acquisitions` (the season)
and `matchupAcquisitionTotals` ({matchupPeriodId: adds}). A day's own adds, which
is what a per-day limit counts, are not in the counter; they are read from the
league's transactions (`mTransactions2`): free-agent and waiver adds, executed,
for the team, recorded for that scoring period, made once the season's first day
began. Pre-season adds do not count (seen 2026-10-08: six pre-season adds, the
counter's `acquisitions` 0), and ESPN records them under day 1.

Which day's allowance an add made after a day's first tip-off uses (that day's, or
the next day's it takes effect on) is unconfirmed, and so is the code ESPN refuses
an over-limit add with. `budget_block` therefore only refuses what is certain
under either reading, and `is_limit_refusal` recognises the refusal by its words.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any, Iterable, Mapping, Optional

import pytz

from core.logging import get_logger
from schemas.common import LeagueInfo
from schemas.lineup_editor import AcquisitionState

log = get_logger("acquisitions")

ACQUIRING_TYPES = frozenset({"FREEAGENT", "WAIVER"})
EASTERN = pytz.timezone("US/Eastern")
ROLLOVER_ET = time(2, 0)       # an ESPN fantasy day starts here


def _limit(value: Any) -> Optional[int]:
    """ESPN's -1 (or nothing usable) is no limit."""
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def parse_acquisitions(payload: Mapping[str, Any], team: Mapping[str, Any]) -> Optional[AcquisitionState]:
    """The league's add limits and `team`'s counts from one mSettings + mTeam read; None when
    the league sends no acquisition settings."""
    rules = (payload.get("settings") or {}).get("acquisitionSettings")
    if not isinstance(rules, Mapping):
        return None
    counter = team.get("transactionCounter") or {}
    matchup = (payload.get("status") or {}).get("currentMatchupPeriod")
    totals = counter.get("matchupAcquisitionTotals") or {}
    return AcquisitionState(
        per="day" if rules.get("matchupLimitPerScoringPeriod") else "matchup",
        limit=_limit(rules.get("matchupAcquisitionLimit", -1)),
        matchup_period_id=int(matchup) if matchup else None,
        matchup_used=int(totals.get(str(matchup), 0) or 0) if matchup else 0,
        season_limit=_limit(rules.get("acquisitionLimit", -1)),
        season_used=int(counter.get("acquisitions") or 0),
    )


def matchup_room(acq: AcquisitionState) -> Optional[int]:
    """Adds left this matchup under a per-matchup limit; None when no such limit applies."""
    if acq.per != "matchup" or acq.limit is None:
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


def budget_block(acq: Optional[AcquisitionState], day: date, *, day_adds: Optional[int] = None) -> Optional[str]:
    """Why an add for ESPN day `day` (a calendar date) certainly cannot count, as a sentence; None
    when it may. `day_adds` is the adds already recorded for that day (per-day limits only).

    Certain only: the season is used up; a per-matchup limit is used up and `day` is in the
    current matchup (an add for a later matchup's day may count toward that one); a per-day
    limit is used up on `day` itself (an add for it is recorded there whichever day's
    allowance it draws on)."""
    if acq is None:
        return None
    if season_room(acq) == 0:
        return f"Every add the league allows this season is used ({acq.season_used} of {acq.season_limit})"
    room = matchup_room(acq)
    if room == 0 and in_current_matchup(acq, day):
        return f"Every add this matchup allows is used ({acq.matchup_used} of {acq.limit})"
    if acq.per == "day" and acq.limit is not None and day_adds is not None and day_adds >= acq.limit:
        return f"That day's add{'s are' if acq.limit != 1 else ' is'} already used ({day_adds} of {acq.limit})"
    return None


def count_adds(transactions: Iterable[Mapping[str, Any]], espn_team_id: int, scoring_period_id: int, *,
               since_ms: Optional[int] = None) -> int:
    """Adds `espn_team_id` made that are recorded for `scoring_period_id`: executed free-agent and
    waiver transactions, one per ADD item to the team, made at or after `since_ms` (epoch ms, ESPN's
    `proposedDate`) when given. Each transaction counts once by its id."""
    seen: set[Any] = set()
    n = 0
    for t in transactions:
        key = t.get("id")
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        if (t.get("type") not in ACQUIRING_TYPES or t.get("status") != "EXECUTED"
                or t.get("teamId") != espn_team_id or t.get("scoringPeriodId") != scoring_period_id):
            continue
        if since_ms is not None and int(t.get("proposedDate") or 0) < since_ms:
            continue
        n += sum(1 for i in t.get("items") or [] if i.get("type") == "ADD" and i.get("toTeamId") == espn_team_id)
    return n


def is_limit_refusal(code: Optional[str], message: Optional[str]) -> bool:
    """ESPN refused an add over its acquisition limit. The code is unconfirmed, so the words decide;
    "LIMIT" alone is not enough (TRAN_ROSTER_POSITION_LIMIT_EXCEEDED is a position cap)."""
    return "ACQUISITION" in (code or "").upper() or "acquisition" in (message or "").lower()


def season_start_ms() -> Optional[int]:
    """When the season's first ESPN day began (its 02:00 ET rollover), as epoch ms; None when the
    calendar can't say."""
    from services import schedule_service

    try:
        opening = schedule_service.date_for_espn_scoring_period(1)
    except Exception as exc:  # no calendar: count every add, as ESPN would once the season runs
        log.warning("acquisitions_season_start_unknown", error=str(exc))
        return None
    start = EASTERN.localize(datetime.combine(opening, ROLLOVER_ET)).astimezone(timezone.utc)
    return int(start.timestamp() * 1000)


async def adds_for_day(league_info: LeagueInfo, espn_team_id: int, scoring_period_id: int) -> int:
    """The team's adds recorded for ESPN day `scoring_period_id`, pre-season ones left out. The
    transactions view is read for that day and the day before, since an add made the evening
    before may be listed under it."""
    from services.espn_service import EspnService   # the parse above stays import-light

    rows: list[Mapping[str, Any]] = []
    for period in sorted({max(1, scoring_period_id - 1), scoring_period_id}):
        payload = await EspnService.fetch_league(league_info, ["mTransactions2"], expect_key="transactions",
                                                 scoring_period_id=period)
        rows.extend(payload.get("transactions") or [])
    return count_adds(rows, espn_team_id, scoring_period_id, since_ms=season_start_ms())
