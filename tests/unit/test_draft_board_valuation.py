"""
The board on the league valuation: games in the category rank, the fantasy-
playoff column and weight, and roto resolved as its own category objective.

The fetch layer is replaced by hand-built BoardInputs, as in
`test_draft_board_service.py`; the calendar is a fabricated six-week season
whose default playoff window is weeks 2-5, so every playoff count below can be
checked on paper.
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from services.draft_board_service import BoardInputs, BoardSession, DraftBoardService
from services.draft_congestion import SampleWeek
from services.scoring.category_rank import PoolRow
from services.scoring.models import StatLine
from services.scoring.resolver import resolve_scoring, resolve_scoring_for_room

pytestmark = pytest.mark.unit

SEASON = "2026-27"
NINE_CAT = [
    {"key": k, "label": k.upper(), "higher_is_better": k != "tov", "is_rate": k.endswith("_pct")}
    for k in ("fg_pct", "ft_pct", "fg3m", "pts", "reb", "ast", "stl", "blk", "tov")
]
LINE = dict(pts=18.0, reb=6.0, ast=4.0, stl=1.0, blk=0.6, tov=2.0,
            fgm=6.5, fga=14.0, fg3m=2.0, fg3a=5.5, ftm=3.0, fta=3.8, min=30.0)


def _league(**overrides):
    base = dict(
        id=3, provider="espn", provider_league_id="1", season=2027, name="L",
        scoring_type="points", category_win_mode=None, categories=[],
        point_weights={"pts": 1.0, "reb": 1.0, "ast": 1.0},
        matchup_periods={},
        roster_slots={"PG": 1, "SG": 1, "SF": 1, "PF": 1, "C": 1, "UT": 3, "BE": 2, "IR": 1},
        position_limits={}, draft_settings={"pick_order": [1, 2]},
        raw_settings={}, settings_synced_at=datetime(2026, 9, 1),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _row(id, team, **stats):
    line = dict(LINE)
    line.update(stats)
    return PoolRow(id=id, name=f"P{id}", team=team, gp=60, line=StatLine.from_dict(line),
                   fpts_avg=30.0, fpts_total=1800.0, name_normalized=f"p{id}")


def _week(number, days):
    return SampleWeek(number=number, days=tuple(frozenset(d) for d in days))


def _calendar():
    """Six weeks. Default playoffs = weeks 2-5. HVY plays 4 a week in them, LGT 2;
    both play 3 in the regular week 1 and 3 in the unplayed week 6."""
    three = [{"HVY", "LGT"}, set(), {"HVY", "LGT"}, set(), {"HVY", "LGT"}, set(), set()]
    playoff = [{"HVY", "LGT"}, {"HVY"}, set(), {"HVY", "LGT"}, {"HVY"}, set(), set()]
    return (
        _week(1, three), _week(2, playoff), _week(3, playoff),
        _week(4, playoff), _week(5, playoff), _week(6, three),
    )


def _inputs(pool, projected_gp=None, current_team=None, calendar=None):
    return BoardInputs(
        season=SEASON, pool=pool,
        source={r.id: "projection" for r in pool},
        projected_gp=projected_gp or {},
        current_team=current_team or {},
        calendar=_calendar() if calendar is None else calendar,
    )


@pytest.fixture(autouse=True)
def direct_db_boundary(monkeypatch):
    from db import base as db_base

    async def direct_run_db(operation_name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(db_base, "run_db", direct_run_db)


def _board(scoring, inputs, session=None, monkeypatch=None):
    monkeypatch.setattr(DraftBoardService, "_fetch_inputs",
                        staticmethod(lambda my_ids, session_id=None: inputs))
    return asyncio.run(DraftBoardService.get_board(scoring, session=session or BoardSession()))


def _by_id(resp):
    return {r.player_id: r for r in resp.data}


def test_rows_carry_playoff_games_and_meta_names_the_window(monkeypatch):
    pool = [_row(1, "HVY"), _row(2, "LGT")]
    resp = _board(resolve_scoring(_league()), _inputs(pool), monkeypatch=monkeypatch)
    rows = _by_id(resp)
    assert rows[1].playoff_games == 16 and rows[1].playoff_games_by_week == [4, 4, 4, 4]
    assert rows[2].playoff_games == 8 and rows[2].playoff_games_by_week == [2, 2, 2, 2]
    # Every night in this calendar has at most one game: all light.
    assert rows[1].playoff_light_games == 16
    meta = resp.meta.playoffs
    assert meta.weeks == [2, 3, 4, 5]
    assert meta.rounds == [[2, 3], [4, 5]]
    assert meta.source == "espn_default"
    assert meta.weight == 2.0
    assert (meta.games_min, meta.games_max, meta.games_mean) == (8, 16, 12.0)


def test_current_team_decides_the_schedule_not_last_seasons(monkeypatch):
    pool = [_row(1, "LGT")]
    resp = _board(resolve_scoring(_league()), _inputs(pool, current_team={1: "HVY"}), monkeypatch=monkeypatch)
    assert _by_id(resp)[1].team == "HVY"
    assert _by_id(resp)[1].playoff_games == 16


def test_playoff_weight_moves_value_toward_the_heavy_schedule(monkeypatch):
    pool = [_row(1, "HVY"), _row(2, "LGT")]
    ratios = []
    for weight in (1.0, 2.0, 4.0):
        resp = _board(resolve_scoring(_league()), _inputs(pool, projected_gp={1: 20, 2: 20}),
                      session=BoardSession(playoff_weight=weight), monkeypatch=monkeypatch)
        rows = _by_id(resp)
        ratios.append(rows[1].season_games / rows[2].season_games)
        assert resp.meta.playoffs.weight == weight
    assert ratios[0] < ratios[1] < ratios[2]


def test_a_league_synced_window_is_used(monkeypatch):
    mp = {"periods": {"1": [1], "2": [2], "3": [3], "4": [4], "5": [5]},
          "period_count": 4, "playoff_period_length": 1, "playoff_team_count": 2}
    pool = [_row(1, "HVY")]
    resp = _board(resolve_scoring(_league(matchup_periods=mp)), _inputs(pool), monkeypatch=monkeypatch)
    assert resp.meta.playoffs.weeks == [5]
    assert resp.meta.playoffs.source == "league"
    assert _by_id(resp)[1].playoff_games == 4


def test_no_calendar_means_no_playoff_column(monkeypatch):
    pool = [_row(1, "HVY")]
    resp = _board(resolve_scoring(_league()), _inputs(pool, calendar=()), monkeypatch=monkeypatch)
    assert resp.meta.playoffs is None
    assert _by_id(resp)[1].playoff_games is None
    assert _by_id(resp)[1].season_games == 65.0


def test_category_rank_counts_projected_games(monkeypatch):
    fillers = [_row(10 + i, "HVY", pts=10.0 + i, reb=4.0 + (i % 3), ast=2.0 + (i % 4),
                    stl=0.6 + 0.1 * (i % 3), blk=0.3 + 0.1 * (i % 4), fg3m=1.0 + 0.2 * (i % 5))
               for i in range(20)]
    pool = fillers + [_row(1, "HVY"), _row(2, "HVY")]
    league = _league(scoring_type="categories", category_win_mode="each_category", categories=NINE_CAT)
    # The fabricated season is 22 games a team; projections share it out.
    resp = _board(resolve_scoring(league), _inputs(pool, projected_gp={1: 20, 2: 11}), monkeypatch=monkeypatch)
    rows = _by_id(resp)
    assert rows[1].cv_rank < rows[2].cv_rank
    assert rows[1].score > rows[2].score
    assert rows[1].season_games > rows[2].season_games


def test_a_projection_past_the_schedule_plays_every_game(monkeypatch):
    pool = [_row(1, "HVY"), _row(2, "HVY")]
    resp = _board(resolve_scoring(_league()), _inputs(pool, projected_gp={1: 82, 2: 22}),
                  session=BoardSession(playoff_weight=1.0), monkeypatch=monkeypatch)
    rows = _by_id(resp)
    assert rows[1].season_games == rows[2].season_games == 19.0     # week 6 is never scored


def test_roto_scores_categories_without_playoffs(monkeypatch):
    pool = [_row(1, "HVY", ast=9.0), _row(2, "LGT", reb=12.0)]
    league = _league(scoring_type="roto", category_win_mode=None, categories=NINE_CAT)
    scoring = resolve_scoring_for_room(league)
    assert scoring.is_categories and scoring.categories.win_mode == "roto"
    resp = _board(scoring, _inputs(pool), monkeypatch=monkeypatch)
    assert resp.meta.playoffs is None
    assert all(r.playoff_games is None for r in resp.data)
    assert all(r.category_z for r in resp.data)
    assert resp.meta.market_rank_type == "roto"


def test_a_roto_league_is_a_category_league_in_the_draft_room_and_nowhere_else():
    """The draft room can show a roto league as one. Rankings, matchups,
    streamers and the lineup tools still present it as points — and so does
    the frontend — so everywhere but the room it keeps resolving to points,
    rather than sending category numbers to views labelled for points."""
    league = _league(scoring_type="roto", category_win_mode=None, categories=NINE_CAT,
                     point_weights={"pts": 1.0, "reb": 1.5})

    elsewhere = resolve_scoring(league)
    assert elsewhere.format == "points" and elsewhere.categories is None
    assert elsewhere.point_weights == {"pts": 1.0, "reb": 1.5}
    assert elsewhere.fingerprint[0] == "points"

    room = resolve_scoring_for_room(league)
    assert room.format == "categories" and room.categories.win_mode == "roto"
    assert room.categories.keys == [c["key"] for c in NINE_CAT]
    assert room.fingerprint == ("categories", tuple(c["key"] for c in NINE_CAT), "roto")
    assert resolve_scoring(league, roto=True).fingerprint == room.fingerprint

    # An ordinary category league is one everywhere, with its own win mode.
    h2h = _league(scoring_type="categories", category_win_mode="most_categories", categories=NINE_CAT)
    assert resolve_scoring(h2h).categories.win_mode == "most_categories"
    assert resolve_scoring_for_room(h2h).categories.win_mode == "most_categories"
    # A league-less room still takes the format it was created with.
    assert resolve_scoring_for_room(None, "categories").is_categories
    assert not resolve_scoring_for_room(None, None).is_categories
