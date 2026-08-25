#!/usr/bin/env python3
"""
Season calendar validator.

Checks the generated static calendar files against the raw NBA feed they were
built from. Each check is a pure function over loaded dicts (no I/O, no
network) so tests can import them; the CLI just loads files and prints.

Usage:
    python scripts/validate_calendar.py --season 2026-27
    python scripts/validate_calendar.py --season 2026-27 --raw static/schedule_raw2026-2027.json --static-dir static

Checks (PASS/FAIL each; exit code 1 on any FAIL):
 1. week structure     weeks are contiguous, week 1 starts on opening night and
                       the last week ends on the last regular-season date,
                       gameSpan equals the inclusive day count, every week lists
                       all 30 tricodes, exactly one 14-day (merged All-Star)
                       week unless the meta file says no merge was done
 2. game coverage      every NBA-vs-NBA "002" game in the raw feed appears exactly
                       once in the week map (as two team-day entries); per-team
                       totals are identical across all 30 teams and in {80, 82}
 3. matchupsPerDay     first date <= preseason start, last date == regular-season
                       end, dates ascending, all tricodes NBA
 4. ESPN period span   (week 23 end - opening night).days + 1 == 167, ESPN's
                       finalScoringPeriod: ESPN merges the All-Star weeks and
                       stops before the final NBA week
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from scripts.build_season_calendar import (  # noqa: E402
    NBA_TRICODES,
    Game,
    default_raw_path,
    load_raw,
    matchups_filename,
    meta_filename,
    parse_mdy,
    preseason_games,
    regular_season_games,
    schedule_filename,
)

ESPN_FINAL_SCORING_PERIOD = 167
ESPN_FINAL_WEEK = 23
MERGED_WEEK_SPAN = 14
VALID_GAMES_PER_TEAM = {80, 82}
ONE_DAY = timedelta(days=1)


@dataclass
class CheckResult:
    name: str
    passed: bool
    details: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        head = f"[{'PASS' if self.passed else 'FAIL'}] {self.name}"
        return "\n".join([head] + [f"       {line}" for line in self.details])


@dataclass(frozen=True)
class SeasonBounds:
    opening_night: date
    regular_season_end: date
    preseason_start: Optional[date]


def season_bounds(feed: dict) -> SeasonBounds:
    regular = regular_season_games(feed)
    if not regular:
        raise ValueError("feed has no NBA-vs-NBA regular-season (002) games")
    preseason = preseason_games(feed)
    return SeasonBounds(
        opening_night=min(g.game_date for g in regular),
        regular_season_end=max(g.game_date for g in regular),
        preseason_start=min((g.game_date for g in preseason), default=None),
    )


def _weeks_in_order(schedule: dict) -> list[tuple[int, dict]]:
    return sorted(((int(k), v) for k, v in schedule.items()), key=lambda kv: kv[0])


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #

def check_week_structure(week_map: dict, feed: dict, meta: Optional[dict] = None) -> CheckResult:
    """Check 1: contiguity, season bounds, gameSpan, tricode coverage, merged-week count."""
    name = "week structure"
    problems: list[str] = []
    schedule = week_map.get("schedule")
    if not isinstance(schedule, dict) or not schedule:
        return CheckResult(name, False, ["top-level 'schedule' object missing or empty"])

    try:
        weeks = _weeks_in_order(schedule)
    except ValueError:
        return CheckResult(name, False, [f"week keys are not all integers: {sorted(schedule)}"])

    numbers = [n for n, _ in weeks]
    if numbers != list(range(1, len(numbers) + 1)):
        problems.append(f"week numbers are not 1..{len(numbers)}: {numbers}")

    bounds = season_bounds(feed)
    prev_end: Optional[date] = None
    span14 = []
    for number, week in weeks:
        try:
            start, end = parse_mdy(week["startDate"]), parse_mdy(week["endDate"])
        except (KeyError, ValueError) as exc:
            problems.append(f"week {number}: bad startDate/endDate ({exc})")
            continue
        span = (end - start).days + 1
        if week.get("gameSpan") != span:
            problems.append(f"week {number}: gameSpan {week.get('gameSpan')} != inclusive days {span}")
        if span == MERGED_WEEK_SPAN:
            span14.append(number)
        if prev_end is not None and start != prev_end + ONE_DAY:
            problems.append(f"week {number}: starts {start}, expected {prev_end + ONE_DAY} (day after week {number - 1})")
        prev_end = end

        games = week.get("games")
        if not isinstance(games, dict):
            problems.append(f"week {number}: 'games' missing")
            continue
        missing = sorted(NBA_TRICODES - set(games))
        extra = sorted(set(games) - NBA_TRICODES)
        if missing or extra:
            problems.append(f"week {number}: tricodes missing={missing} extra={extra}")
        for tri, days in games.items():
            bad = [d for d in days if not d.isdigit() or not (0 <= int(d) < span)]
            if bad:
                problems.append(f"week {number} {tri}: day indices out of range {bad}")

    first_start = parse_mdy(weeks[0][1]["startDate"])
    last_end = parse_mdy(weeks[-1][1]["endDate"])
    if first_start != bounds.opening_night:
        problems.append(f"week 1 starts {first_start}, opening night is {bounds.opening_night}")
    if last_end != bounds.regular_season_end:
        problems.append(f"last week ends {last_end}, regular season ends {bounds.regular_season_end}")

    expect_merge = meta is None or meta.get("merged_week") is not None
    if expect_merge and len(span14) != 1:
        problems.append(f"expected exactly one {MERGED_WEEK_SPAN}-day week, found {span14 or 'none'}")
    if not expect_merge and span14:
        problems.append(f"meta says no All-Star merge but {MERGED_WEEK_SPAN}-day weeks exist: {span14}")

    details = problems or [
        f"{len(weeks)} contiguous weeks {first_start} -> {last_end}"
        + (f"; 14-day week: {span14[0]}" if span14 else "; no merged week"),
    ]
    return CheckResult(name, not problems, details)


def _team_days(week_map: dict) -> Counter:
    """(tricode, date) -> occurrences in the week map."""
    found: Counter = Counter()
    for _, week in _weeks_in_order(week_map.get("schedule", {})):
        start = parse_mdy(week["startDate"])
        for tri, days in week.get("games", {}).items():
            for day in days:
                found[(tri, start + timedelta(days=int(day)))] += 1
    return found


def check_game_coverage(week_map: dict, feed: dict) -> CheckResult:
    """Check 2: every regular-season game appears exactly once; per-team totals equal and in {80, 82}."""
    name = "game coverage"
    problems: list[str] = []
    regular: list[Game] = regular_season_games(feed)
    expected: Counter = Counter()
    for g in regular:
        expected[(g.home, g.game_date)] += 1
        expected[(g.away, g.game_date)] += 1

    found = _team_days(week_map)
    missing = sorted(k for k in expected if found.get(k, 0) == 0)
    extra = sorted(k for k in found if expected.get(k, 0) == 0)
    doubled = sorted(k for k, n in expected.items() if n > 1)
    if missing:
        problems.append(f"{len(missing)} feed team-days missing from week map, e.g. {missing[:5]}")
    if extra:
        problems.append(f"{len(extra)} week-map team-days not in feed, e.g. {extra[:5]}")
    if doubled:
        problems.append(f"feed has a team playing twice on one date: {doubled[:5]}")
    if sum(found.values()) != 2 * len(regular):
        problems.append(f"week map has {sum(found.values())} team-days, feed implies {2 * len(regular)}")

    per_team: Counter = Counter()
    for g in regular:
        per_team[g.home] += 1
        per_team[g.away] += 1
    totals = {per_team.get(t, 0) for t in NBA_TRICODES}
    if len(totals) != 1:
        problems.append(
            "per-team game totals differ: "
            + ", ".join(f"{t}={per_team.get(t, 0)}" for t in sorted(NBA_TRICODES) if per_team.get(t, 0) != max(totals))
        )
    elif not totals <= VALID_GAMES_PER_TEAM:
        problems.append(f"games per team is {totals.pop()}, expected one of {sorted(VALID_GAMES_PER_TEAM)}")

    details = problems or [
        f"{len(regular)} regular-season games -> {sum(found.values())} team-days; "
        f"{next(iter(totals))} games per team for all 30 teams"
    ]
    return CheckResult(name, not problems, details)


def check_matchups_per_day(per_day: dict, feed: dict) -> CheckResult:
    """Check 3: first date <= preseason start, last date == regular-season end."""
    name = "matchupsPerDay bounds"
    problems: list[str] = []
    if not per_day:
        return CheckResult(name, False, ["file is empty"])
    try:
        dates = [parse_mdy(k) for k in per_day]
    except ValueError as exc:
        return CheckResult(name, False, [f"bad date key: {exc}"])
    if dates != sorted(dates):
        problems.append("dates are not in ascending order")

    bounds = season_bounds(feed)
    first, last = dates[0], dates[-1]
    if bounds.preseason_start is None:
        problems.append("feed has no NBA-vs-NBA preseason games; cannot check the first date")
    elif first > bounds.preseason_start:
        problems.append(f"first date {first} is after preseason start {bounds.preseason_start}")
    if last != bounds.regular_season_end:
        problems.append(f"last date {last} != regular-season end {bounds.regular_season_end}")

    bad_entries = [
        (key, entry) for key, entries in per_day.items() for entry in entries
        if entry.get("homeTeam") not in NBA_TRICODES or entry.get("awayTeam") not in NBA_TRICODES
    ]
    if bad_entries:
        problems.append(f"{len(bad_entries)} entries with non-NBA tricodes, e.g. {bad_entries[:3]}")

    details = problems or [
        f"{len(dates)} dates {first} -> {last}, {sum(len(v) for v in per_day.values())} games"
    ]
    return CheckResult(name, not problems, details)


def check_espn_scoring_span(week_map: dict, feed: dict) -> CheckResult:
    """Check 4: (week 23 end - opening night).days + 1 == 167 (ESPN's finalScoringPeriod)."""
    name = f"ESPN scoring-period span (week {ESPN_FINAL_WEEK} end - opening night + 1 == {ESPN_FINAL_SCORING_PERIOD})"
    week = week_map.get("schedule", {}).get(str(ESPN_FINAL_WEEK))
    if week is None:
        return CheckResult(name, False, [f"week {ESPN_FINAL_WEEK} not in week map"])
    bounds = season_bounds(feed)
    end = parse_mdy(week["endDate"])
    span = (end - bounds.opening_night).days + 1
    detail = f"opening night {bounds.opening_night}, week {ESPN_FINAL_WEEK} ends {end}: {span} days"
    return CheckResult(name, span == ESPN_FINAL_SCORING_PERIOD, [detail])


def run_checks(feed: dict, week_map: dict, per_day: dict, meta: Optional[dict] = None) -> list[CheckResult]:
    return [
        check_week_structure(week_map, feed, meta),
        check_game_coverage(week_map, feed),
        check_matchups_per_day(per_day, feed),
        check_espn_scoring_span(week_map, feed),
    ]


def print_results(results: list[CheckResult]) -> bool:
    for r in results:
        print(r)
    passed = all(r.passed for r in results)
    print(f"{'ALL CHECKS PASSED' if passed else 'CHECKS FAILED'} ({sum(r.passed for r in results)}/{len(results)})")
    return passed


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def load_static(static_dir: Path, season: str) -> tuple[dict, dict, Optional[dict]]:
    """Load (week map, matchupsPerDay, meta-or-None) for ``season`` from ``static_dir``."""
    week_map = load_raw(static_dir / schedule_filename(season))
    per_day = load_raw(static_dir / matchups_filename(season))
    meta_path = static_dir / meta_filename(season)
    meta = load_raw(meta_path) if meta_path.exists() else None
    return week_map, per_day, meta


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--season", required=True, help="season label, e.g. 2026-27")
    p.add_argument("--raw", type=Path, help="raw feed path (default: static/schedule_raw{YYYY}-{YYYY+1}.json)")
    p.add_argument("--static-dir", type=Path, default=BACKEND_ROOT / "static", help="directory holding the generated files")
    p.add_argument(
        "--espn-team-id",
        type=int,
        metavar="N",
        help="(stub) cross-check week boundaries against an ESPN league's scoring periods; not implemented yet",
    )
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.espn_team_id is not None:
        raise SystemExit("ESPN alignment check lands in milestone M2")

    raw_path = args.raw or default_raw_path(args.season)
    if not raw_path.exists():
        raise SystemExit(f"raw feed not found: {raw_path}")
    feed = load_raw(raw_path)
    try:
        week_map, per_day, meta = load_static(args.static_dir, args.season)
    except FileNotFoundError as exc:
        raise SystemExit(f"generated file missing: {exc.filename}") from exc

    print(f"Validating {args.season}: {args.static_dir} against {raw_path}")
    return 0 if print_results(run_checks(feed, week_map, per_day, meta)) else 1


if __name__ == "__main__":
    sys.exit(main())
