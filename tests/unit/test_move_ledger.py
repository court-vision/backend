"""The move ledger (services/move_ledger.py): moves read off the daily rosters, who made
them, and what each earned in the box scores."""

import asyncio
import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from services import move_ledger as ml
from services.ai import tools
from services.scoring.categories import CategoryScoring
from services.scoring.models import CategoryDef, StatLine
from services.scoring.resolver import ResolvedScoring, resolve_scoring
from services.scoring.vocab import DEFAULT_POINT_WEIGHTS

pytestmark = pytest.mark.unit

OPENING = date(2026, 10, 20)
POINTS = resolve_scoring(None)
CATS = ResolvedScoring(
    format="categories", league=None, settings_synced=True, point_weights=dict(DEFAULT_POINT_WEIGHTS),
    category_scoring=CategoryScoring([CategoryDef.for_key(k) for k in ("pts", "reb", "tov", "fg_pct")]),
)


def day(period: int, players: dict[int, int], team: int = 1, name: str = "Atlas", matchup: int = 1) -> ml.DayRoster:
    """A stored day: {ESPN id: slot id}; a player's name is P<id>."""
    return ml.DayRoster(
        provider_team_id=team, team_name=name, scoring_period_id=period,
        nba_date=OPENING + timedelta(days=period - 1), matchup_period_id=matchup,
        players={pid: (f"P{pid}", slot) for pid, slot in players.items()},
    )


def write(rid: int, period: int, adds=(), drops=(), source="you", team: int = 1, **extra) -> ml.CvWrite:
    return ml.CvWrite(roster_move_id=rid, team_id=21, provider_team_id=team, scoring_period_id=period,
                      source=source, adds=frozenset(adds), drops=frozenset(drops), **extra)


def line(pts=0.0, reb=0.0, ast=0.0, tov=0.0, fgm=0.0, fga=0.0) -> StatLine:
    return StatLine(pts=pts, reb=reb, ast=ast, tov=tov, fgm=fgm, fga=fga, gp=1.0)


# ---------------------------- detecting changes ---------------------------- #


def test_a_change_is_a_roster_difference_between_stored_days():
    changes = ml.detect_changes([
        day(3, {10: 0, 11: 12, 12: 5}),
        day(1, {10: 0, 11: 0, 13: 12}),          # out of order on purpose
        day(2, {10: 0, 11: 12, 13: 12}),          # only a slot change: no move
    ])
    assert [(c.day.scoring_period_id, c.adds, c.drops) for c in changes] == [
        (3, ((12, "P12"),), ((13, "P13"),)),
    ]


def test_a_team_s_first_stored_day_and_a_gap_compare_with_what_was_stored():
    changes = ml.detect_changes([
        day(1, {10: 0}), day(4, {10: 0, 20: 12}),             # days 2-3 missing: the add counts from day 4
        day(1, {30: 0}, team=2, name="Rival"),
    ])
    assert [(c.day.provider_team_id, c.day.scoring_period_id, c.adds) for c in changes] == [(1, 4, ((20, "P20"),))]


# ---------------------------- who made it ---------------------------- #


def change(period: int, adds=(), drops=(), team: int = 1) -> ml.Change:
    return ml.Change(day=day(period, {}, team=team),
                     adds=tuple((p, f"P{p}") for p in adds), drops=tuple((p, f"P{p}") for p in drops))


def test_a_court_vision_write_names_its_move_and_pairs_its_add_with_its_drop():
    moves = ml.attribute([change(5, adds=[12], drops=[13])], [write(7, 4, adds=[12], drops=[13], source="lane")])
    assert len(moves) == 1
    m = moves[0]
    assert (m.add, m.drop, m.source, m.paired, m.write.roster_move_id) == ((12, "P12"), (13, "P13"), "lane", True, 7)


def test_a_write_explains_a_change_on_its_day_or_up_to_two_days_later():
    assert ml.attribute([change(6, adds=[12])], [write(7, 4, adds=[12])])[0].source == "you"
    assert ml.attribute([change(7, adds=[12])], [write(7, 4, adds=[12])])[0].source == "outside"
    assert ml.attribute([change(3, adds=[12])], [write(7, 4, adds=[12])])[0].source == "outside"   # before the write


def test_the_newest_write_wins_and_explains_a_player_once():
    writes = [write(7, 3, adds=[12], source="scheduled"), write(8, 4, adds=[12], source="dime")]
    moves = ml.attribute([change(4, adds=[12]), change(5, adds=[12])], writes)
    assert [m.source for m in moves] == ["dime", "scheduled"]


def test_what_no_write_explains_is_outside_and_a_lone_swap_is_paired():
    moves = ml.attribute([change(5, adds=[12, 14], drops=[13])], [write(7, 5, adds=[14], source="you")])
    assert [(m.source, m.add, m.drop) for m in moves] == [
        ("you", (14, "P14"), None),
        ("outside", (12, "P12"), (13, "P13")),
    ]


def test_an_unclear_day_stays_unpaired():
    moves = ml.attribute([change(5, adds=[12, 14], drops=[13, 15])], [])
    assert [(m.add, m.drop) for m in moves] == [
        ((12, "P12"), None), ((14, "P14"), None), (None, (13, "P13")), (None, (15, "P15")),
    ]
    assert not any(m.paired for m in moves)


def test_a_write_on_another_team_explains_nothing_here():
    moves = ml.attribute([change(5, adds=[12], team=2)], [write(7, 5, adds=[12], team=1)])
    assert moves[0].source == "outside"


# ---------------------------- scoring ---------------------------- #


def test_windows_start_on_the_day_the_move_counted():
    start = date(2026, 11, 5)
    assert ml.windows_of(start, date(2026, 11, 9)) == {
        "day": (start, start), "matchup": (start, date(2026, 11, 9)), "two_weeks": (start, date(2026, 11, 18)),
    }
    assert ml.windows_of(start, None)["matchup"] == (start, start)


def _move(add=(12, "Add"), drop=(13, "Drop")) -> ml.Move:
    return ml.Move(day=day(5, {}), add=add, drop=drop, source="you")


def test_a_points_window_scores_both_sides_and_the_add_s_counted_games():
    d = OPENING + timedelta(days=4)
    games = {
        112: [(d, line(pts=20)), (d + timedelta(days=1), line(pts=10)), (d + timedelta(days=30), line(pts=99))],
        113: [(d, line(pts=8))],
    }
    out = ml.score_window(_move(), (d, d + timedelta(days=13)), d + timedelta(days=1), add_nba=112, drop_nba=113,
                          games=games, active_days={d}, scoring=POINTS)
    weight = DEFAULT_POINT_WEIGHTS["pts"]
    assert out["add"] == {"games": 2, "points": round(30 * weight, 1), "counted_games": 1,
                          "counted_points": round(20 * weight, 1)}
    assert out["drop"] == {"games": 1, "points": round(8 * weight, 1)}
    assert out["net"] == round(22 * weight, 1)
    assert out["complete"] is False                     # ends after the last box score


def test_a_lone_add_nets_what_it_scored_and_a_lone_drop_what_it_gave_up():
    d = OPENING + timedelta(days=4)
    games = {112: [(d, line(pts=10))], 113: [(d, line(pts=6))]}
    add_only = ml.score_window(_move(drop=None), (d, d), d, add_nba=112, drop_nba=None, games=games,
                               active_days=set(), scoring=POINTS)
    drop_only = ml.score_window(_move(add=None), (d, d), d, add_nba=None, drop_nba=113, games=games,
                                active_days=set(), scoring=POINTS)
    weight = DEFAULT_POINT_WEIGHTS["pts"]
    assert (add_only["drop"], add_only["net"], add_only["complete"]) == (None, round(10 * weight, 1), True)
    assert (drop_only["add"], drop_only["net"]) == (None, round(-6 * weight, 1))


def test_a_player_with_no_nba_id_has_no_games():
    d = OPENING
    out = ml.score_window(_move(), (d, d), d, add_nba=None, drop_nba=None, games={}, active_days=set(), scoring=POINTS)
    assert out["add"]["games"] == 0 and out["drop"]["games"] == 0 and out["net"] == 0


def test_a_category_window_has_totals_and_the_categories_the_add_won():
    d = OPENING
    games = {112: [(d, line(pts=20, reb=2, tov=1, fgm=8, fga=16))], 113: [(d, line(pts=10, reb=9, tov=4, fgm=5, fga=10))]}
    out = ml.score_window(_move(), (d, d), d, add_nba=112, drop_nba=113, games=games, active_days={d}, scoring=CATS)
    assert out["add"]["totals"] == {"pts": 20.0, "reb": 2.0, "tov": 1.0, "fg_pct": 0.5}
    assert out["add"]["counted_games"] == 1
    assert "net" not in out
    assert out["edge"] == {"won": ["pts", "tov"], "lost": ["reb"], "tied": ["fg_pct"]}


def test_held_through_is_the_last_day_the_add_stayed():
    days = [day(5, {12: 0}), day(6, {12: 12}), day(7, {}), day(8, {12: 0})]
    move = ml.Move(day=days[0], add=(12, "P12"), drop=None, source="you")
    assert ml.held_through(move, days) == (OPENING + timedelta(days=5)).isoformat()
    assert ml.held_through(ml.Move(day=days[0], add=None, drop=(1, "x"), source="you"), days) is None


def test_build_rows_carries_who_made_it_the_forecast_and_the_two_week_net():
    days = [day(1, {10: 0, 13: 0}), day(2, {10: 0, 12: 0}), day(3, {10: 0, 12: 12}),
            day(1, {30: 0}, team=2, name="Rival"), day(2, {31: 0}, team=2, name="Rival")]
    league = ml.League(key=ml.LeagueKey("espn", "123", 2027), days=days, cv_teams={1: 21})
    proposal = write(9, 2, adds=[12], drops=[13], source="dime", proposal_id=4,
                     forecast={"scoring": "points", "change": 6.5}, reason="two extra games")
    moves = ml.attribute(ml.detect_changes(days), [proposal])
    d2 = OPENING + timedelta(days=1)
    games = {112: [(d2, line(pts=10)), (d2 + timedelta(days=1), line(pts=10))], 113: [(d2, line(pts=4))]}
    rows = ml.build_rows(league, moves, games=games, nba_ids={12: 112, 13: 113}, scoring=POINTS,
                         stats_through=d2 + timedelta(days=1), matchup_end=lambda _: d2 + timedelta(days=5))
    mine, rival = rows
    weight = DEFAULT_POINT_WEIGHTS["pts"]
    assert (mine["source"], mine["team_id"], mine["roster_move_id"], mine["proposal_id"]) == ("dime", 21, 9, 4)
    assert (mine["add_nba_id"], mine["drop_nba_id"], mine["paired"], mine["format"]) == (112, 113, True, "points")
    assert mine["forecast"] == {"scoring": "points", "change": 6.5} and mine["reason"] == "two extra games"
    assert mine["windows"]["day"]["add"]["counted_games"] == 1
    assert mine["windows"]["two_weeks"]["add"] == {"games": 2, "points": round(20 * weight, 1), "counted_games": 1,
                                                    "counted_points": round(10 * weight, 1)}
    assert mine["net_points"] == round(16 * weight, 1)
    assert mine["windows"]["held_through"] == (OPENING + timedelta(days=2)).isoformat()
    assert (rival["source"], rival["team_id"], rival["team_name"], rival["add_nba_id"]) == ("outside", None, "Rival", None)
    json.dumps(rows, default=str)                       # the windows go into jsonb


def test_category_rows_have_no_net_points():
    days = [day(1, {13: 0}), day(2, {12: 0})]
    league = ml.League(key=ml.LeagueKey("espn", "123", 2027), days=days, cv_teams={})
    rows = ml.build_rows(league, ml.attribute(ml.detect_changes(days), []), games={}, nba_ids={}, scoring=CATS,
                         stats_through=None, matchup_end=lambda _: None)
    assert rows[0]["format"] == "categories" and rows[0]["net_points"] is None
    assert rows[0]["windows"]["two_weeks"]["complete"] is False


# ---------------------------- reading the ledger ---------------------------- #


def outcome(**fields) -> SimpleNamespace:
    base = dict(id=1, nba_date=date(2026, 10, 25), team_name="Atlas", team_id_id=21, provider_team_id=1,
                source="you", add_nba_id=112, add_name="Add", drop_nba_id=113, drop_name="Drop", paired=True,
                format="points", forecast=None, reason=None,
                windows={"two_weeks": {"complete": True, "net": 5.0}})
    base.update(fields)
    return SimpleNamespace(**base)


Q = ml.OutcomeQuery(team_id=21, key=ml.LeagueKey("espn", "123", 2027), provider_team_id=1)


def test_a_move_reads_with_nba_ids_and_says_whose_it_was():
    view = ml.outcome_view(outcome(), Q)
    assert view["add"] == {"player_id": 112, "name": "Add"} and view["mine"] is True
    assert "forecast" not in view
    own = ml.outcome_view(outcome(source="dime", forecast={"change": 3}, reason="r"), Q)
    assert own["source"] == "dime" and own["forecast"] == {"change": 3} and own["reason"] == "r"
    assert ml.outcome_view(outcome(drop_nba_id=None, drop_name=None, paired=False), Q)["drop"] is None


def test_a_rival_s_move_never_says_how_court_vision_made_it():
    # A rival on Court Vision too: their lane, their own Dime and its reason are theirs
    rival = ml.outcome_view(outcome(team_id_id=99, provider_team_id=2, source="dime", forecast={"change": 3},
                                    reason="r"), Q)
    assert rival["mine"] is False and rival["source"] == "outside"
    assert "forecast" not in rival and "reason" not in rival
    rows = [outcome(), outcome(team_id_id=99, provider_team_id=2, source="lane")]
    assert [s["source"] for s in ml.summarize(rows, "points", Q)] == ["you", "outside"]


def test_the_summary_totals_each_source_over_its_settled_moves():
    rows = [
        outcome(source="you", windows={"two_weeks": {"complete": True, "net": 5.0}}),
        outcome(source="you", windows={"two_weeks": {"complete": True, "net": -2.5}}),
        outcome(source="you", windows={"two_weeks": {"complete": False, "net": 40.0}}),
        outcome(source="dime", windows={"two_weeks": {"complete": True, "net": 1.0}}),
    ]
    assert ml.summarize(rows, "points") == [
        {"source": "dime", "moves": 1, "settled": 1, "net_points": 1.0, "ahead": 1, "behind": 0},
        {"source": "you", "moves": 3, "settled": 2, "net_points": 2.5, "ahead": 1, "behind": 1},
    ]
    cats = [outcome(windows={"two_weeks": {"complete": True, "edge": {"won": ["pts", "reb"], "lost": ["tov"]}}})]
    assert ml.summarize(cats, "categories") == [
        {"source": "you", "moves": 1, "settled": 1, "categories_won": 2, "categories_lost": 1},
    ]


def test_read_outcomes_limits_the_moves_but_summarizes_them_all(monkeypatch):
    rows = [outcome(id=i) for i in range(5)]
    monkeypatch.setattr(ml, "select_outcomes", lambda q: rows)
    found = ml.read_outcomes(ml.OutcomeQuery(team_id=21, key=Q.key, provider_team_id=1, limit=2))
    assert [m["move_id"] for m in found["moves"]] == [0, 1]
    assert found["total"] == 5 and found["summary"][0]["moves"] == 5 and found["format"] == "points"


# ---------------------------- the tool ---------------------------- #


def _run(name, raw, ctx):
    return asyncio.run(tools.run_tool(name, raw, ctx=ctx, allowed=tools.AGENT_TOOL_NAMES))


def test_get_move_outcomes_reads_an_owned_team_s_ledger(monkeypatch):
    seen = {}

    async def owned(ctx, team_id):
        if team_id != 21:
            from core.errors import AppError
            raise AppError("TEAM_NOT_FOUND", f"Team {team_id} is not one of the user's teams")
        return SimpleNamespace(team_id=21), None

    async def query(team, **kw):
        seen.update(kw)
        return ml.OutcomeQuery(team_id=team.team_id, key=Q.key, provider_team_id=1, **kw)

    async def run_db(name, fn, *args):
        return fn(*args)

    monkeypatch.setattr(tools, "_owned", owned)
    monkeypatch.setattr(tools, "move_ledger_query", query)
    monkeypatch.setattr(tools, "run_db", run_db)
    monkeypatch.setattr(ml, "select_outcomes", lambda q: [outcome()])
    ctx = tools.ToolContext(user_id=42, conversation_id=1, build=None)

    ok = _run("get_move_outcomes", {"team_id": 21, "scope": "league", "source": ["dime"], "since": "2026-10-21"}, ctx)
    assert not ok.is_error
    body = json.loads(ok.content)
    assert body["team_id"] == 21 and body["scope"] == "league" and body["moves"][0]["add"]["player_id"] == 112
    assert seen == {"scope": "league", "sources": ("dime",), "nba_player_id": None, "since": date(2026, 10, 21),
                    "limit": 12}
    assert _run("get_move_outcomes", {"team_id": 99}, ctx).is_error
    assert _run("get_move_outcomes", {"team_id": 21, "source": ["auto"]}, ctx).is_error
    assert _run("get_move_outcomes", {"team_id": 21, "limit": 41}, ctx).is_error
