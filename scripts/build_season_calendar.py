#!/usr/bin/env python3
"""
Season calendar generator.

Turns the raw NBA schedule feed (``scheduleLeagueV2_1.json``) into the static
files the app reads each season:

* ``schedule{yy}-{yy}.json``       fantasy week map, read by backend, data-platform
                                   and features/lineup-generation/v2 (Go)
* ``matchupsPerDay{yy}-{yy}.json``  home/away pairs per calendar date, read by
                                   backend and data-platform
* ``schedule{yy}-{yy}.meta.json``   how the files were built (backend copy only)

Usage:
    python scripts/build_season_calendar.py --season 2026-27
    python scripts/build_season_calendar.py --season 2026-27 --fetch       # refresh raw feed from cdn.nba.com
    python scripts/build_season_calendar.py --season 2026-27 --check       # run validate_calendar on the outputs
    python scripts/build_season_calendar.py --season 2026-27 --dry-run     # print the summary, write nothing
    python scripts/build_season_calendar.py --season 2026-27 --all-star-break 2027-02-19:2027-02-24
    python scripts/build_season_calendar.py --season 2026-27 --all-star-break none
    python scripts/build_season_calendar.py --season 2026-27 --out-dir static --out-dir /elsewhere

Rules (enforced by scripts/validate_calendar.py):
- Regular season = gameId prefix "002"; a game's date is gameDateEst[:10].
- A game is dropped unless BOTH tricodes are one of the 30 NBA teams. This
  removes Cup-knockout placeholders (empty tricodes) and international
  preseason opponents.
- Weeks = the feed's ``weeks`` deduped by weekNumber (first occurrence wins),
  sorted, restricted to those overlapping the regular season.
- All-Star break = the longest run of >= 4 consecutive dates between Jan 15 and
  Mar 15 of the season's second year with ZERO games of any kind in the feed.
  Placeholder and Cup games count as games on purpose: the December Cup
  knockout gap has no named games but does have placeholders, and must not be
  mistaken for the break.
- The week containing the break's first day is merged with the week containing
  its last day (or the following week when both fall in the same week) into one
  14-day week, and weeks are renumbered 1..N. This mirrors how ESPN builds its
  scoring periods.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from utils.espn_helpers import PRO_TEAM_MAP, TEAM_ABBREV_CORRECTIONS  # noqa: E402

NBA_CDN_SCHEDULE_URL = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_1.json"

# The 30 NBA tricodes as the NBA feed spells them. PRO_TEAM_MAP carries ESPN's
# spellings (PHL/PHO), so pass them through the same correction table the ESPN
# service uses.
NBA_TRICODES: frozenset[str] = frozenset(
    TEAM_ABBREV_CORRECTIONS.get(code, code) for code in PRO_TEAM_MAP.values() if code != "FA"
)
assert len(NBA_TRICODES) == 30, NBA_TRICODES

PRESEASON_PREFIX = "001"
REGULAR_SEASON_PREFIX = "002"
CUP_FINAL_PREFIX = "006"
# What goes into matchupsPerDay: preseason, regular season and the Cup final
# (previous seasons' file listed the Cup final too). Play-in/playoffs (003+)
# are excluded so the file ends on the last regular-season date.
PER_DAY_PREFIXES = frozenset({PRESEASON_PREFIX, REGULAR_SEASON_PREFIX, CUP_FINAL_PREFIX})

ALL_STAR_MIN_IDLE_DAYS = 4
ALL_STAR_WINDOW = ((1, 15), (3, 15))  # (month, day) bounds in the season's second year

DEFAULT_OUT_DIRS = (
    BACKEND_ROOT / "static",
    REPO_ROOT / "data-platform" / "static",
    REPO_ROOT / "features" / "lineup-generation" / "v2" / "static",
)

ONE_DAY = timedelta(days=1)


# --------------------------------------------------------------------------- #
# Season / date helpers
# --------------------------------------------------------------------------- #

def season_years(season: str) -> tuple[int, int]:
    """'2026-27' -> (2026, 2027)."""
    m = re.fullmatch(r"(\d{4})-(\d{2})", season)
    if not m:
        raise ValueError(f"season must look like 2026-27, got {season!r}")
    first = int(m.group(1))
    if (first + 1) % 100 != int(m.group(2)):
        raise ValueError(f"season years are not consecutive: {season!r}")
    return first, first + 1


def season_short(season: str) -> str:
    """'2026-27' -> '26-27' (the suffix used in static file names)."""
    first, second = season_years(season)
    return f"{first % 100:02d}-{second % 100:02d}"


def default_raw_path(season: str) -> Path:
    first, second = season_years(season)
    return BACKEND_ROOT / "static" / f"schedule_raw{first}-{second}.json"


def schedule_filename(season: str) -> str:
    return f"schedule{season_short(season)}.json"


def matchups_filename(season: str) -> str:
    return f"matchupsPerDay{season_short(season)}.json"


def meta_filename(season: str) -> str:
    return f"schedule{season_short(season)}.meta.json"


def parse_feed_date(value: str) -> date:
    """'2026-10-20T00:00:00Z' -> date(2026, 10, 20)."""
    return date.fromisoformat(value[:10])


def fmt_mdy(d: date) -> str:
    return d.strftime("%m/%d/%Y")


def parse_mdy(value: str) -> date:
    return datetime.strptime(value, "%m/%d/%Y").date()


def daterange(start: date, end: date) -> Iterator[date]:
    """Inclusive range of dates."""
    d = start
    while d <= end:
        yield d
        d += ONE_DAY


# --------------------------------------------------------------------------- #
# Raw feed model
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Game:
    game_id: str
    game_date: date
    home: str
    away: str
    label: str = ""

    @property
    def prefix(self) -> str:
        return self.game_id[:3]

    @property
    def is_nba_vs_nba(self) -> bool:
        return self.home in NBA_TRICODES and self.away in NBA_TRICODES

    @property
    def is_placeholder(self) -> bool:
        return not self.home or not self.away


@dataclass(frozen=True)
class Week:
    number: int
    start: date
    end: date

    @property
    def game_span(self) -> int:
        return (self.end - self.start).days + 1

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end


def load_raw(path: Path) -> dict:
    with open(path, "r") as f:
        return json.load(f)


def league_schedule(feed: dict) -> dict:
    try:
        return feed["leagueSchedule"]
    except KeyError as exc:
        raise ValueError("raw feed has no 'leagueSchedule' key") from exc


def feed_season(feed: dict) -> Optional[str]:
    return league_schedule(feed).get("seasonYear")


def iter_games(feed: dict) -> Iterator[Game]:
    """Every game in the feed, in feed order, placeholders included."""
    for game_date in league_schedule(feed).get("gameDates", []):
        for g in game_date.get("games", []):
            yield Game(
                game_id=g["gameId"],
                game_date=parse_feed_date(g["gameDateEst"]),
                home=(g.get("homeTeam") or {}).get("teamTricode") or "",
                away=(g.get("awayTeam") or {}).get("teamTricode") or "",
                label=g.get("gameLabel") or "",
            )


def regular_season_games(feed: dict) -> list[Game]:
    """NBA-vs-NBA games with gameId prefix 002."""
    return [g for g in iter_games(feed) if g.prefix == REGULAR_SEASON_PREFIX and g.is_nba_vs_nba]


def preseason_games(feed: dict) -> list[Game]:
    """NBA-vs-NBA games with gameId prefix 001."""
    return [g for g in iter_games(feed) if g.prefix == PRESEASON_PREFIX and g.is_nba_vs_nba]


def dedupe_weeks(weeks: list[dict]) -> list[dict]:
    """Drop repeated weekNumber entries (first occurrence wins); return sorted by weekNumber."""
    seen: dict[int, dict] = {}
    for w in weeks:
        seen.setdefault(int(w["weekNumber"]), w)
    return [seen[n] for n in sorted(seen)]


def feed_weeks(feed: dict) -> list[Week]:
    return [
        Week(int(w["weekNumber"]), parse_feed_date(w["startDate"]), parse_feed_date(w["endDate"]))
        for w in dedupe_weeks(league_schedule(feed).get("weeks", []))
    ]


def game_counts_by_date(feed: dict) -> Counter:
    """Number of games of ANY kind per date (placeholders and Cup games count)."""
    return Counter(g.game_date for g in iter_games(feed))


# --------------------------------------------------------------------------- #
# All-Star break
# --------------------------------------------------------------------------- #

def detect_all_star_break(
    feed: dict,
    second_year: int,
    *,
    window_start: Optional[date] = None,
    window_end: Optional[date] = None,
    min_idle_days: int = ALL_STAR_MIN_IDLE_DAYS,
) -> Optional[tuple[date, date]]:
    """Longest run of >= ``min_idle_days`` consecutive dates with zero games of any kind.

    Searched between Jan 15 and Mar 15 of ``second_year`` by default. Ties go
    to the earliest run. Returns ``(first_idle_day, last_idle_day)`` or None.
    """
    window_start = window_start or date(second_year, *ALL_STAR_WINDOW[0])
    window_end = window_end or date(second_year, *ALL_STAR_WINDOW[1])
    counts = game_counts_by_date(feed)

    best: Optional[tuple[date, date]] = None
    run_start: Optional[date] = None
    for d in daterange(window_start, window_end + ONE_DAY):  # +1 day sentinel closes a trailing run
        idle = d <= window_end and counts.get(d, 0) == 0
        if idle:
            run_start = run_start or d
            continue
        if run_start is not None:
            run = (run_start, d - ONE_DAY)
            length = (run[1] - run[0]).days + 1
            if length >= min_idle_days and (best is None or length > (best[1] - best[0]).days + 1):
                best = run
            run_start = None
    return best


def parse_all_star_arg(value: str) -> Optional[tuple[date, date]]:
    """'YYYY-MM-DD:YYYY-MM-DD' -> (start, end); 'none' -> None."""
    if value.lower() == "none":
        return None
    try:
        start_s, end_s = value.split(":")
        start, end = date.fromisoformat(start_s), date.fromisoformat(end_s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"expected YYYY-MM-DD:YYYY-MM-DD or 'none', got {value!r}"
        ) from exc
    if end < start:
        raise argparse.ArgumentTypeError(f"All-Star break ends before it starts: {value!r}")
    return start, end


# --------------------------------------------------------------------------- #
# Weeks
# --------------------------------------------------------------------------- #

def season_weeks(feed: dict, opening_night: date, regular_season_end: date) -> list[Week]:
    """Feed weeks (deduped) that overlap the regular season, clamped to it."""
    weeks = [w for w in feed_weeks(feed) if w.end >= opening_night and w.start <= regular_season_end]
    if not weeks:
        raise ValueError("no feed weeks overlap the regular season")
    first, last = weeks[0], weeks[-1]
    weeks[0] = Week(first.number, max(first.start, opening_night), first.end)
    weeks[-1] = Week(last.number, weeks[-1].start, min(last.end, regular_season_end))
    return weeks


@dataclass(frozen=True)
class MergedWeek:
    number: int            # position of the merged week in the final 1..N numbering
    start: date
    end: date
    source_weeks: tuple[int, ...]  # feed weekNumbers that were merged

    @property
    def game_span(self) -> int:
        return (self.end - self.start).days + 1


def merge_all_star_week(
    weeks: list[Week], all_star_break: Optional[tuple[date, date]]
) -> tuple[list[Week], Optional[MergedWeek]]:
    """Merge the All-Star week with its neighbour and renumber weeks 1..N.

    The week containing the break's first day is merged with the week containing
    its last day; when both fall in the same week, that week is merged with the
    FOLLOWING one. With no break, weeks are just renumbered.
    """
    if all_star_break is None:
        return [Week(i, w.start, w.end) for i, w in enumerate(weeks, 1)], None

    break_start, break_end = all_star_break
    first = next((i for i, w in enumerate(weeks) if w.contains(break_start)), None)
    if first is None:
        raise ValueError(f"All-Star break start {break_start} is not inside any season week")
    last = next((i for i, w in enumerate(weeks) if w.contains(break_end)), None)
    if last is None or last == first:
        last = first + 1
    if last >= len(weeks):
        raise ValueError("All-Star week is the final week; nothing to merge it with")

    merged = Week(weeks[first].number, weeks[first].start, weeks[last].end)
    combined = weeks[:first] + [merged] + weeks[last + 1:]
    info = MergedWeek(
        number=first + 1,
        start=merged.start,
        end=merged.end,
        source_weeks=tuple(w.number for w in weeks[first:last + 1]),
    )
    return [Week(i, w.start, w.end) for i, w in enumerate(combined, 1)], info


# --------------------------------------------------------------------------- #
# Output builders
# --------------------------------------------------------------------------- #

def build_week_map(weeks: list[Week], games: list[Game]) -> dict:
    """``{"schedule": {"1": {"startDate", "endDate", "gameSpan", "games": {TRI: {"0": true}}}}}``.

    Every week lists all 30 tricodes (an empty object when a team is idle);
    day indices are strings counted from the week's startDate.
    """
    by_date: dict[date, list[Game]] = defaultdict(list)
    for g in games:
        by_date[g.game_date].append(g)

    schedule: dict[str, dict] = {}
    for w in weeks:
        team_days: dict[str, dict[str, bool]] = {tri: {} for tri in sorted(NBA_TRICODES)}
        for d in daterange(w.start, w.end):
            day_index = str((d - w.start).days)
            for g in by_date.get(d, ()):
                team_days[g.home][day_index] = True
                team_days[g.away][day_index] = True
        schedule[str(w.number)] = {
            "startDate": fmt_mdy(w.start),
            "endDate": fmt_mdy(w.end),
            "gameSpan": w.game_span,
            "games": team_days,
        }
    return {"schedule": schedule}


def build_matchups_per_day(feed: dict) -> dict[str, list[dict[str, str]]]:
    """``{"MM/DD/YYYY": [{"homeTeam", "awayTeam"}, ...]}`` for every date with an NBA-vs-NBA game.

    Preseason, regular season and the Cup final are included; placeholder and
    non-NBA games are dropped. Games keep feed order within a date.
    """
    by_date: dict[date, list[dict[str, str]]] = defaultdict(list)
    for g in iter_games(feed):
        if g.prefix in PER_DAY_PREFIXES and g.is_nba_vs_nba:
            by_date[g.game_date].append({"homeTeam": g.home, "awayTeam": g.away})
    return {fmt_mdy(d): by_date[d] for d in sorted(by_date)}


def games_per_team(games: list[Game]) -> Counter:
    counts: Counter = Counter()
    for g in games:
        counts[g.home] += 1
        counts[g.away] += 1
    return counts


# --------------------------------------------------------------------------- #
# Fetch / IO
# --------------------------------------------------------------------------- #

def fetch_raw_feed(season: str, dest: Path) -> dict:
    """Download the CDN feed, verify it is for ``season`` and save it to ``dest``."""
    import requests

    from utils.nba_cdn_headers import NBA_CDN_HEADERS

    resp = requests.get(NBA_CDN_SCHEDULE_URL, headers=NBA_CDN_HEADERS("cdn.nba.com"), timeout=60)
    resp.raise_for_status()
    try:
        feed = resp.json()
    except ValueError as exc:
        encoding = resp.headers.get("Content-Encoding")
        hint = " (brotli response; `pip install brotli` lets requests decode it)" if encoding == "br" else ""
        raise SystemExit(f"could not decode feed as JSON; Content-Encoding={encoding!r}{hint}") from exc

    got = feed_season(feed)
    if got != season:
        raise SystemExit(f"fetched feed is for season {got!r}, expected {season!r}; not saved")

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)
    print(f"Fetched {NBA_CDN_SCHEDULE_URL} -> {dest} ({len(resp.content):,} bytes)")
    return feed


def write_json(path: Path, data: dict, indent: int = 4) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=indent)
        f.write("\n")


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--season", required=True, help="season label as in the feed, e.g. 2026-27")
    p.add_argument("--raw", type=Path, help="raw feed path (default: static/schedule_raw{YYYY}-{YYYY+1}.json)")
    p.add_argument("--fetch", action="store_true", help="download the feed from cdn.nba.com into --raw first")
    p.add_argument(
        "--all-star-break",
        type=parse_all_star_arg,
        default=argparse.SUPPRESS,
        metavar="YYYY-MM-DD:YYYY-MM-DD|none",
        help="override All-Star break detection, or 'none' to skip the week merge",
    )
    p.add_argument(
        "--out-dir",
        action="append",
        type=Path,
        help="output directory (repeatable; default: backend, data-platform and features static dirs)",
    )
    p.add_argument("--check", action="store_true", help="run validate_calendar on the written outputs")
    p.add_argument("--dry-run", action="store_true", help="print the summary without writing anything")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    season = args.season
    _, second_year = season_years(season)
    raw_path = args.raw or default_raw_path(season)

    # ---- load feed
    if args.fetch:
        feed = fetch_raw_feed(season, raw_path)
    else:
        if not raw_path.exists():
            raise SystemExit(f"raw feed not found: {raw_path} (pass --fetch or --raw)")
        feed = load_raw(raw_path)
        got = feed_season(feed)
        if got != season:
            raise SystemExit(f"{raw_path} is for season {got!r}, expected {season!r}")

    # ---- games
    all_games = list(iter_games(feed))
    regular = [g for g in all_games if g.prefix == REGULAR_SEASON_PREFIX and g.is_nba_vs_nba]
    if not regular:
        raise SystemExit("feed has no NBA-vs-NBA regular-season (002) games")
    regular_placeholders = sum(1 for g in all_games if g.prefix == REGULAR_SEASON_PREFIX and g.is_placeholder)
    regular_non_nba = sum(
        1 for g in all_games
        if g.prefix == REGULAR_SEASON_PREFIX and not g.is_placeholder and not g.is_nba_vs_nba
    )
    preseason = [g for g in all_games if g.prefix == PRESEASON_PREFIX and g.is_nba_vs_nba]
    preseason_dropped = sum(1 for g in all_games if g.prefix == PRESEASON_PREFIX and not g.is_nba_vs_nba)

    opening_night = min(g.game_date for g in regular)
    regular_season_end = max(g.game_date for g in regular)
    preseason_start = min((g.game_date for g in preseason), default=None)
    preseason_end = max((g.game_date for g in preseason), default=None)

    # ---- weeks
    raw_weeks = league_schedule(feed).get("weeks", [])
    week_numbers = Counter(int(w["weekNumber"]) for w in raw_weeks)
    duplicate_weeks = sorted(n for n, c in week_numbers.items() if c > 1)
    weeks = season_weeks(feed, opening_night, regular_season_end)

    if "all_star_break" in args:
        all_star_break = args.all_star_break
        break_source = "disabled (--all-star-break none)" if all_star_break is None else "given (--all-star-break)"
    else:
        all_star_break = detect_all_star_break(feed, second_year)
        break_source = "detected" if all_star_break else "not detected"
    weeks, merged = merge_all_star_week(weeks, all_star_break)

    # ---- outputs
    week_map = build_week_map(weeks, regular)
    per_day = build_matchups_per_day(feed)
    per_team = games_per_team(regular)
    unplaced = [g for g in regular if not (weeks[0].start <= g.game_date <= weeks[-1].end)]

    meta = {
        "season": season,
        "opening_night": opening_night.isoformat(),
        "regular_season_end": regular_season_end.isoformat(),
        "preseason_start": preseason_start.isoformat() if preseason_start else None,
        "week_count": len(weeks),
        "all_star_break": (
            {"start": all_star_break[0].isoformat(), "end": all_star_break[1].isoformat(), "source": break_source}
            if all_star_break else None
        ),
        "merged_week": (
            {
                "number": merged.number,
                "startDate": fmt_mdy(merged.start),
                "endDate": fmt_mdy(merged.end),
                "gameSpan": merged.game_span,
                "source_weeks": list(merged.source_weeks),
            }
            if merged else None
        ),
        "generated_from": _display(raw_path.resolve()),
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }

    # ---- summary
    per_team_values = sorted(set(per_team[t] for t in NBA_TRICODES))
    print(f"Season {season}  (raw: {_display(raw_path.resolve())})")
    print(
        f"  Regular season : {fmt_mdy(opening_night)} - {fmt_mdy(regular_season_end)}  "
        f"({len(regular)} NBA-vs-NBA games; dropped {regular_placeholders} placeholder, {regular_non_nba} non-NBA)"
    )
    if preseason_start:
        print(
            f"  Preseason      : {fmt_mdy(preseason_start)} - {fmt_mdy(preseason_end)}  "
            f"({len(preseason)} NBA-vs-NBA games; dropped {preseason_dropped} non-NBA/placeholder)"
        )
    else:
        print("  Preseason      : none in feed")
    print(
        f"  Feed weeks     : {len(raw_weeks)} entries -> {len(dedupe_weeks(raw_weeks))} after dedupe"
        + (f" (duplicate weekNumber: {', '.join(map(str, duplicate_weeks))})" if duplicate_weeks else "")
    )
    if all_star_break:
        idle_days = (all_star_break[1] - all_star_break[0]).days + 1
        print(f"  All-Star break : {all_star_break[0]} -> {all_star_break[1]}  ({idle_days} idle days, {break_source})")
    else:
        print(f"  All-Star break : {break_source}; no week merge")
    if merged:
        print(
            f"  Merged week    : feed weeks {' + '.join(map(str, merged.source_weeks))} -> "
            f"week {merged.number} {fmt_mdy(merged.start)} - {fmt_mdy(merged.end)}, gameSpan {merged.game_span}"
        )
    first_w, last_w = weeks[0], weeks[-1]
    print(
        f"  Weeks          : {len(weeks)}  "
        f"(week 1 {fmt_mdy(first_w.start)} - {fmt_mdy(first_w.end)} gameSpan {first_w.game_span}; "
        f"week {last_w.number} {fmt_mdy(last_w.start)} - {fmt_mdy(last_w.end)} gameSpan {last_w.game_span})"
    )
    print(
        f"  Games per team : {per_team_values[0] if len(per_team_values) == 1 else per_team_values}"
        + ("  (all 30 teams)" if len(per_team_values) == 1 else "  (UNEVEN across teams)")
    )
    if unplaced:
        print(f"  WARNING        : {len(unplaced)} regular-season games fall outside the week range")
    dates = list(per_day)
    print(
        f"  matchupsPerDay : {len(dates)} dates, {dates[0]} -> {dates[-1]}, "
        f"{sum(len(v) for v in per_day.values())} games"
    )

    if args.dry_run:
        print("Dry run: nothing written.")
        return 0

    # ---- write
    out_dirs = [d.resolve() for d in (args.out_dir or DEFAULT_OUT_DIRS)]
    backend_static = (BACKEND_ROOT / "static").resolve()
    meta_dir = backend_static if backend_static in out_dirs else out_dirs[0]
    print("Wrote:")
    written: list[Path] = []
    for out_dir in out_dirs:
        for name, data in ((schedule_filename(season), week_map), (matchups_filename(season), per_day)):
            path = out_dir / name
            write_json(path, data)
            written.append(path)
        if out_dir == meta_dir:
            path = out_dir / meta_filename(season)
            write_json(path, meta)
            written.append(path)
    for path in written:
        print(f"  {_display(path)}")

    if args.check:
        from scripts.validate_calendar import print_results, run_checks

        print()
        results = run_checks(feed, week_map, per_day, meta)
        return 0 if print_results(results) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
