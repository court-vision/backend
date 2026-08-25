"""Generated season-calendar files stay consistent with the raw NBA feed they came from.

Runs the validator's four checks as assertions over every static/schedule{yy}-{yy}.json
that has a matching raw feed on disk, plus unit tests for the All-Star detector
and the feed-week dedupe. No DB, no network.
"""

import re
from datetime import date
from pathlib import Path

import pytest

from scripts import build_season_calendar as bsc
from scripts import validate_calendar as vc

pytestmark = pytest.mark.unit

STATIC_DIR = Path(__file__).resolve().parents[2] / "static"


def _seasons_with_raw_feed() -> list[str]:
    seasons = []
    for path in sorted(STATIC_DIR.glob("schedule??-??.json")):
        m = re.fullmatch(r"schedule(\d{2})-(\d{2})\.json", path.name)
        if not m:
            continue
        season = f"20{m.group(1)}-{m.group(2)}"
        if bsc.default_raw_path(season).exists():
            seasons.append(season)
    return seasons


SEASONS = _seasons_with_raw_feed()


@pytest.fixture(scope="module", params=SEASONS or [pytest.param(None, marks=pytest.mark.skip("no calendar files"))])
def calendar(request):
    """(feed, week_map, per_day, meta) for one generated season."""
    season = request.param
    week_map, per_day, meta = vc.load_static(STATIC_DIR, season)
    if meta is None:
        pytest.skip(f"{season}: legacy hand-built calendar (no .meta.json); predates build_season_calendar.py")
    feed = bsc.load_raw(bsc.default_raw_path(season))
    return feed, week_map, per_day, meta


def test_week_structure(calendar):
    feed, week_map, _, meta = calendar
    result = vc.check_week_structure(week_map, feed, meta)
    assert result.passed, "\n".join(result.details)


def test_game_coverage(calendar):
    feed, week_map, _, _ = calendar
    result = vc.check_game_coverage(week_map, feed)
    assert result.passed, "\n".join(result.details)


def test_matchups_per_day_bounds(calendar):
    feed, _, per_day, _ = calendar
    result = vc.check_matchups_per_day(per_day, feed)
    assert result.passed, "\n".join(result.details)


def test_espn_scoring_period_span(calendar):
    feed, week_map, _, _ = calendar
    result = vc.check_espn_scoring_span(week_map, feed)
    assert result.passed, "\n".join(result.details)


# --------------------------------------------------------------------------- #
# Synthetic-feed unit tests
# --------------------------------------------------------------------------- #

def _game(game_id: str, day: date, home: str, away: str) -> dict:
    return {
        "gameId": game_id,
        "gameDateEst": f"{day.isoformat()}T00:00:00Z",
        "homeTeam": {"teamTricode": home},
        "awayTeam": {"teamTricode": away},
        "weekNumber": 0,
        "gameLabel": "",
    }


def _feed(games_by_date: dict[date, list[dict]], weeks: list[dict] | None = None) -> dict:
    return {
        "leagueSchedule": {
            "seasonYear": "2026-27",
            "gameDates": [
                {"gameDate": d.strftime("%m/%d/%Y 00:00:00"), "games": games}
                for d, games in sorted(games_by_date.items())
            ],
            "weeks": weeks or [],
        }
    }


def _synthetic_feed_with_gaps() -> dict:
    """One named game per day Nov 25 -> Mar 10 except:
    - Dec 4-10: no named games, but Cup placeholders (empty tricodes) on Dec 4, 5, 8
      -> 7 dates without a *named* game, only 2-day runs with zero games of any kind
    - Jan 20-22: 3 idle days (too short to be the break)
    - Feb 19-24: 6 idle days (the All-Star break)
    """
    games: dict[date, list[dict]] = {}
    d = date(2026, 11, 25)
    seq = 0
    while d <= date(2027, 3, 10):
        seq += 1
        if date(2026, 12, 4) <= d <= date(2026, 12, 10):
            if d in (date(2026, 12, 4), date(2026, 12, 5), date(2026, 12, 8)):
                games[d] = [_game(f"00226{seq:05d}", d, "", "")]
        elif date(2027, 1, 20) <= d <= date(2027, 1, 22):
            pass
        elif date(2027, 2, 19) <= d <= date(2027, 2, 24):
            pass
        else:
            games[d] = [_game(f"00226{seq:05d}", d, "BOS", "LAL")]
        d += bsc.ONE_DAY
    return _feed(games)


def test_all_star_detector_picks_february_gap_not_december_placeholders():
    feed = _synthetic_feed_with_gaps()
    assert bsc.detect_all_star_break(feed, 2027) == (date(2027, 2, 19), date(2027, 2, 24))


def test_all_star_detector_counts_placeholders_even_with_a_wide_window():
    # Without counting placeholder games, Dec 4-10 (7 dates) would beat Feb 19-24 (6 dates).
    feed = _synthetic_feed_with_gaps()
    found = bsc.detect_all_star_break(
        feed, 2027, window_start=date(2026, 11, 25), window_end=date(2027, 3, 15)
    )
    assert found == (date(2027, 2, 19), date(2027, 2, 24))


def test_all_star_detector_ignores_short_gaps():
    games = {d: [_game("0022600001", d, "BOS", "LAL")] for d in bsc.daterange(date(2027, 1, 15), date(2027, 3, 15))}
    for d in (date(2027, 2, 19), date(2027, 2, 20), date(2027, 2, 21)):
        del games[d]
    assert bsc.detect_all_star_break(_feed(games), 2027) is None


def test_dedupe_weeks_keeps_first_week_8_entry():
    weeks = [
        {"weekNumber": 1, "startDate": "2026-10-20T00:00:00Z", "endDate": "2026-10-25T00:00:00Z"},
        {"weekNumber": 9, "startDate": "2026-12-14T00:00:00Z", "endDate": "2026-12-20T00:00:00Z"},
        {"weekNumber": 8, "startDate": "2026-12-07T00:00:00Z", "endDate": "2026-12-13T00:00:00Z"},
        {"weekNumber": 8, "startDate": "2026-12-08T00:00:00Z", "endDate": "2026-12-14T00:00:00Z"},
    ]
    deduped = bsc.dedupe_weeks(weeks)
    assert [w["weekNumber"] for w in deduped] == [1, 8, 9]
    assert deduped[1] is weeks[2], "first week-8 entry must win"


def test_merge_all_star_week_spanning_two_weeks_and_renumber():
    weeks = [
        bsc.Week(17, date(2027, 2, 8), date(2027, 2, 14)),
        bsc.Week(18, date(2027, 2, 15), date(2027, 2, 21)),
        bsc.Week(19, date(2027, 2, 22), date(2027, 2, 28)),
        bsc.Week(20, date(2027, 3, 1), date(2027, 3, 7)),
    ]
    merged, info = bsc.merge_all_star_week(weeks, (date(2027, 2, 19), date(2027, 2, 24)))
    assert [(w.number, w.start, w.end) for w in merged] == [
        (1, date(2027, 2, 8), date(2027, 2, 14)),
        (2, date(2027, 2, 15), date(2027, 2, 28)),
        (3, date(2027, 3, 1), date(2027, 3, 7)),
    ]
    assert merged[1].game_span == 14
    assert info.source_weeks == (18, 19) and info.number == 2


def test_merge_all_star_week_inside_one_week_merges_with_following():
    weeks = [
        bsc.Week(18, date(2027, 2, 15), date(2027, 2, 21)),
        bsc.Week(19, date(2027, 2, 22), date(2027, 2, 28)),
    ]
    merged, info = bsc.merge_all_star_week(weeks, (date(2027, 2, 16), date(2027, 2, 19)))
    assert len(merged) == 1 and merged[0].game_span == 14
    assert info.source_weeks == (18, 19)


def test_merge_disabled_only_renumbers():
    weeks = [bsc.Week(3, date(2026, 11, 2), date(2026, 11, 8)), bsc.Week(4, date(2026, 11, 9), date(2026, 11, 15))]
    merged, info = bsc.merge_all_star_week(weeks, None)
    assert info is None
    assert [w.number for w in merged] == [1, 2]
