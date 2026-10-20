"""
services.ai.fantasy: the agent's what-if projection and stream planner tools over
stubbed reads. The projection core and the planners are exercised by their own
suites; what matters here is how the tools build their inputs (games per day off
the calendar, the first open day, the drop the rule picks, the value function) and
what they report: NBA ids only, the before figures the matchup read's own.
"""

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from core.errors import AppError
from schemas.common import ApiStatus, FantasyProvider, LeagueInfo
from schemas.lineup_editor import AcquisitionState, LineupPlayer, LineupState
from schemas.matchup import (
    CategoryComparison,
    CategoryScoreItem,
    CategoryTeamScore,
    MatchupData,
    MatchupPlayerResp,
    MatchupResp,
    MatchupTeamResp,
)
from services.ai import fantasy
from services.daily_actions_service import DropEvidence
from services.espn_service import PoolEntry
from services.player_value_service import PlayerValueService
from services.scoring.models import StatLine
from services.scoring.resolver import resolve_scoring
from services.stream_lane_service import LaneDay, PendingPickup, PoolPlayer, StreamLaneService, active_slots

ESPN = LeagueInfo(provider=FantasyProvider.ESPN, league_id=555, team_name="T", year=2027, espn_s2="s2", swid="{swid}")
POINTS = resolve_scoring(None)
CATEGORY = resolve_scoring(None, preview="categories")
NOW = datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc)


def team(slots=4):
    return SimpleNamespace(team_id=7, league=SimpleNamespace(roster_slots={"PG": 1, "UT": 1, "BE": slots - 3, "IR": 1}))


def run(coro):
    return asyncio.run(coro)


# ---- project_matchup ----------------------------------------------------------------------


WEEK = {
    "matchup_number": 1, "start_date": date(2026, 10, 20), "end_date": date(2026, 10, 26), "game_span": 7,
    "games": {"DEN": {"0": True, "2": True, "4": True, "6": True}, "LAL": {"1": True, "3": True, "5": True},
              "BOS": {"2": True, "3": True, "5": True, "6": True}},
    "current_day_index": 1,                                       # today is day 1 (Oct 21)
}


def roster_player(espn_id, nba_id, name, nba_team, value, slot, *, injured=False, line=None, source="provider"):
    p = MatchupPlayerResp(player_id=espn_id, nba_player_id=nba_id, name=name, team=nba_team, position="PG",
                          lineup_slot=slot, avg_points=value, projected_points=0.0, games_remaining=0, injured=injured)
    p._avg_line = line
    p._value_source = source
    return p


def matchup(roster, *, scoring_format="points", current=100.0, projected=250.0, opp_projected=240.0,
            categories=None, projected_categories=None):
    you = MatchupTeamResp(team_name="Me", team_id=1, current_score=current, projected_score=projected, roster=roster,
                          categories=categories)
    opp = MatchupTeamResp(team_name="Them", team_id=2, current_score=90.0, projected_score=opp_projected, roster=[])
    data = MatchupData(matchup_period=1, matchup_period_start="2026-10-20", matchup_period_end="2026-10-26",
                       your_team=you, opponent_team=opp, projected_winner="Me", projected_margin=projected - opp_projected,
                       scoring_format=scoring_format, projected_category_comparison=projected_categories)
    return MatchupResp(status=ApiStatus.SUCCESS, message="ok", data=data)


A = (101, 1001, "A", "DEN", 30.0, "PG")
B = (102, 1002, "B", "LAL", 20.0, "UT")
HURT = (103, 1003, "Hurt", "BOS", 40.0, "IR")
X = (104, 1004, "X", "BOS", 10.0, "BE")


@pytest.fixture
def what_if(monkeypatch):
    """Stubbed reads: the matchup, the calendar, the first open day, the adds' identities and values."""
    st = SimpleNamespace(
        resp=matchup([roster_player(*A), roster_player(*B), roster_player(*HURT, injured=True)]),
        first_open=1, scoring=POINTS, evidence=None, entries={}, asked=None,
        players={2001: fantasy._Identity(2001, "FA", 201, "LAL"), 2002: fantasy._Identity(2002, "Nobody", None, None)},
        values={2001: (25.0, "rolling", StatLine(pts=15))},
    )

    async def direct(name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def get_matchup(user_id, team_id, window="season"):
        st.matchup_call = (user_id, team_id, window)
        return st.resp

    async def evidence_for(league_info, espn_ids):
        return st.evidence

    async def availability(league_info, espn_ids):
        st.asked = espn_ids
        return st.entries

    monkeypatch.setattr(fantasy, "run_db", direct)
    monkeypatch.setattr(fantasy.MatchupService, "get_matchup_by_team_id", staticmethod(get_matchup))
    monkeypatch.setattr(fantasy.schedule_service, "get_nba_today", lambda: date(2026, 10, 21))
    monkeypatch.setattr(fantasy.schedule_service, "get_current_matchup", lambda today=None: dict(WEEK))
    monkeypatch.setattr(fantasy, "_first_open_day", lambda today, idx, now: st.first_open)
    monkeypatch.setattr(PlayerValueService, "scoring_for", staticmethod(lambda li, team_id=None: st.scoring))
    monkeypatch.setattr(fantasy, "_identities", lambda ids: {i: st.players[i] for i in ids if i in st.players})
    monkeypatch.setattr(fantasy, "_add_values",
                        lambda scoring, adds: {a.nba_id: st.values.get(a.nba_id, (None, None, None)) for a in adds})
    monkeypatch.setattr(fantasy, "_evidence_for", evidence_for)
    monkeypatch.setattr(fantasy, "_availability", availability)
    return st


def project(moves, *, league=ESPN, team_=None, window="season", now=NOW):
    args = fantasy.ProjectMatchupInput(team_id=7, moves=moves, window=window)
    return run(fantasy.project_matchup(42, team_ or team(), league, args, now=now))


@pytest.mark.unit
def test_a_swap_on_a_day_moves_the_games_and_the_before_is_the_matchup_reads_projection(what_if):
    out = project([{"add": 2001, "drop": 1002, "day": 3}])
    assert what_if.matchup_call == (42, 7, "season")
    # Before: 100 + A 30 x 3 (days 2, 4, 6) + B 20 x 3 (days 1, 3, 5); Hurt is on IR. The read says 250 too.
    assert out["before"] == {"you": 250.0, "opponent": 240.0, "margin": 10.0}
    # After: B keeps day 1, FA (LAL) counts days 3 and 5 at 25.
    assert out["after"]["you"] == 100 + 90 + 20 + 50 and out["change"] == 10.0
    (move,) = out["moves"]
    assert (move["day"], move["date"], move["add"], move["add_player_id"], move["add_games"]) == (3, date(2026, 10, 23), "FA", 2001, 2)
    assert (move["drop"], move["drop_player_id"], move["drop_games"], move["drop_chosen"]) == ("B", 1002, 2, None)
    assert (out["today"], out["first_open_day"], out["last_day"], out["scoring"]) == (1, 1, 6, "points")
    assert "labels" in out and any("starts every game" in a for a in out["assumes"])


@pytest.mark.unit
def test_the_default_day_is_the_first_open_one_and_an_earlier_day_moves_to_it(what_if):
    what_if.first_open = 2                                       # after today's first tip-off
    out = project([{"add": 2001, "drop": 1002}])
    assert out["moves"][0]["day"] == 2 and out["moves"][0]["drop_games"] == 2
    out = project([{"add": 2001, "drop": 1002, "day": 0}])
    assert out["moves"][0]["day"] == 2 and any("counts from day 2" in a for a in out["assumes"])
    with pytest.raises(AppError, match="after the matchup's last day"):
        project([{"add": 2001, "drop": 1002, "day": 9}])


@pytest.mark.unit
def test_an_add_without_a_drop_on_a_full_roster_gets_the_rules_drop(what_if):
    what_if.evidence = DropEvidence(games={101: 7, 102: 7, 103: 7}, projected={})
    out = project([{"add": 2001, "day": 1}], team_=team(slots=3))
    (move,) = out["moves"]
    # From day 1: A 30 x 3 = 90, B 20 x 3 = 60; Hurt is on IR, so dropping him frees nothing: B goes.
    assert move["drop"] == "B" and move["drop_chosen"].startswith("the rule's drop: 3 game(s) left at 20.0")
    assert out["after"]["you"] == 100 + 90 + 25 * 3
    # With a seat open nobody is dropped.
    out = project([{"add": 2001, "day": 1}], team_=team(slots=4))
    assert out["moves"][0]["drop"] is None and any("open roster spot" in a for a in out["assumes"])


@pytest.mark.unit
def test_an_ir_player_is_never_the_rules_drop_and_dropping_him_frees_no_spot(what_if):
    cheap_ir = (103, 1003, "Hurt", "BOS", 1.0, "IR")
    what_if.resp = matchup([roster_player(*A), roster_player(*B), roster_player(*cheap_ir, injured=True)])
    what_if.evidence = DropEvidence(games={101: 7, 102: 7, 103: 7}, projected={})
    out = project([{"add": 2001, "day": 1}], team_=team(slots=3))
    assert out["moves"][0]["drop"] == "B"                                    # Hurt's 4 games at 1.0 would be cheapest
    with pytest.raises(AppError, match="frees no active spot"):
        project([{"add": 2001, "drop": 1003, "day": 1}], team_=team(slots=3))
    # Dropping him on his own opens nothing either: the add the next day still needs the rule's drop.
    out = project([{"drop": 1003, "day": 1}, {"add": 2001, "day": 2}], team_=team(slots=3))
    assert [(m["drop"], m["add"]) for m in out["moves"]] == [("Hurt", None), ("B", "FA")]
    # With an active spot open, the add takes it and the IR drop is just a drop.
    out = project([{"add": 2001, "drop": 1003, "day": 1}], team_=team(slots=4))
    assert out["moves"][0]["drop"] == "Hurt" and any("frees none" in a for a in out["assumes"])


@pytest.mark.unit
def test_a_chain_is_read_as_a_whole_before_the_rule_drops_anyone(what_if):
    """A drop listed later the same day opens the spot for an add listed first (with or without an IR
    drop of its own), and a player a later move drops is spoken for: the rule picks someone else."""
    what_if.evidence = DropEvidence(games={101: 7, 102: 7, 103: 7}, projected={})
    out = project([{"add": 2001, "day": 1}, {"drop": 1001, "day": 1}], team_=team(slots=3))
    assert [(m["add"], m["drop"]) for m in out["moves"]] == [("FA", None), (None, "A")]
    assert any("a drop later on day 1 opens" in a for a in out["assumes"])
    out = project([{"add": 2001, "drop": 1003, "day": 1}, {"drop": 1001, "day": 1}], team_=team(slots=3))
    assert [(m["add"], m["drop"]) for m in out["moves"]] == [("FA", "Hurt"), (None, "A")]
    out = project([{"add": 2001, "day": 1}, {"drop": 1002, "day": 3}], team_=team(slots=3))
    assert [(m["day"], m["add"], m["drop"]) for m in out["moves"]] == [(1, "FA", "A"), (3, None, "B")]
    # A spot opened by one drop is taken once: the second add that day still needs the rule.
    what_if.players[2003] = fantasy._Identity(2003, "Second", 203, "BOS")
    what_if.values[2003] = (10.0, "rolling", None)
    out = project([{"add": 2001, "day": 1}, {"add": 2003, "day": 1}, {"drop": 1001, "day": 1}], team_=team(slots=3))
    assert [(m["add"], m["drop"]) for m in out["moves"]] == [("FA", None), ("Second", "B"), (None, "A")]


@pytest.mark.unit
def test_an_empty_ir_slot_is_not_an_open_spot(what_if):
    """Three active players fill the three active slots; the empty IR slot is no room for a healthy add,
    so the rule drops someone. A player on IR takes no active seat either."""
    what_if.resp = matchup([roster_player(*A), roster_player(*B), roster_player(*X)])
    what_if.evidence = DropEvidence(games={101: 7, 102: 7, 104: 7}, projected={})
    out = project([{"add": 2001, "day": 1}], team_=team(slots=4))
    # From day 1: A 30 x 3 = 90, B 20 x 3 = 60, X 10 x 4 (BOS days 2, 3, 5, 6) = 40: X goes.
    assert out["moves"][0]["drop"] == "X"
    assert fantasy._roster_room(team(slots=4), what_if.resp.data.your_team.roster) == 0
    assert fantasy._roster_room(team(slots=4), [roster_player(*A), roster_player(*B), roster_player(*HURT)]) == 1
    assert fantasy._roster_room(SimpleNamespace(league=SimpleNamespace(roster_slots={})), []) == 0


@pytest.mark.unit
def test_an_adds_availability_is_reported_not_refused(what_if):
    what_if.players[2003] = fantasy._Identity(2003, "Claim", 203, "BOS")
    what_if.values[2003] = (10.0, "rolling", None)
    what_if.players[2004] = fantasy._Identity(2004, "Free", 204, "BOS")
    what_if.values[2004] = (10.0, "rolling", None)
    what_if.entries = {
        201: PoolEntry(201, "ONTEAM", 2, False, "FA", "LAL"),
        203: PoolEntry(203, "WAIVERS", 0, False, "Claim", "BOS", waivers_until=date(2026, 10, 22)),
        204: PoolEntry(204, "FREEAGENT", 0, False, "Free", "BOS"),
    }
    out = project([{"add": 2001, "drop": 1002, "day": 1}, {"add": 2003, "drop": 1001, "day": 2},
                   {"add": 2004, "day": 3}, {"add": 2002, "day": 4}], team_=team(slots=9))
    assert sorted(what_if.asked) == [201, 203, 204]                           # Nobody has no ESPN id
    assert [m["availability"] for m in out["moves"]] == [
        "on another team's roster", "on waivers until 2026-10-22", "free agent", None]
    assert any(a.startswith("FA is on another team's roster") for a in out["assumes"])
    assert any(a.startswith("Claim is on waivers until 2026-10-22") for a in out["assumes"])
    assert not any(a.startswith("Free is") for a in out["assumes"])
    assert fantasy.LABELS["availability"] == "Available"
    assert fantasy._availability_of(PoolEntry(201, "ONTEAM", 1, False, "FA", "LAL"), 1) == "on your roster"
    assert fantasy._availability_of(PoolEntry(201, None, 0, False, "FA", "LAL"), 1) is None


@pytest.mark.unit
def test_availability_is_espn_only_and_never_a_failure(monkeypatch):
    yahoo = LeagueInfo(provider=FantasyProvider.YAHOO, league_id=1, team_name="T", year=2027)
    assert run(fantasy._availability(yahoo, [201])) == {} and run(fantasy._availability(ESPN, [])) == {}

    async def boom(league_info, player_ids, *, scoring_period_id=None):
        raise RuntimeError("espn down")
    monkeypatch.setattr(fantasy.EspnService, "get_player_pool_entries", staticmethod(boom))
    assert run(fantasy._availability(ESPN, [201])) == {}


@pytest.mark.unit
def test_a_player_whose_value_is_unknown_is_never_the_rules_drop(what_if):
    what_if.resp = matchup([roster_player(*A), roster_player(*B, source=None), roster_player(*HURT, injured=True)])
    what_if.evidence = DropEvidence(games={101: 7, 103: 7}, projected={})
    out = project([{"add": 2001, "day": 1}], team_=team(slots=3))
    assert out["moves"][0]["drop"] == "A"
    what_if.resp = matchup([roster_player(*B, source=None)])
    with pytest.raises(AppError, match="nobody on it can be dropped"):
        project([{"add": 2001, "day": 1}], team_=team(slots=1))


@pytest.mark.unit
def test_moves_chain_and_bad_ones_are_refused(what_if):
    what_if.players[2003] = fantasy._Identity(2003, "Second", 203, "BOS")
    what_if.values[2003] = (10.0, "rolling", None)
    out = project([{"add": 2003, "drop": 2001, "day": 5}, {"add": 2001, "drop": 1002, "day": 1}])
    assert [(m["day"], m["add"], m["drop"]) for m in out["moves"]] == [(1, "FA", "B"), (5, "Second", "FA")]
    # 100 + A 90 + B 0 + FA days 1 and 3 (50) + Second days 5 and 6 (20)
    assert out["after"]["you"] == 260.0
    with pytest.raises(AppError, match="No NBA player with id 9999"):
        project([{"add": 9999}])
    with pytest.raises(AppError, match="not on the roster"):
        project([{"drop": 2001}])
    with pytest.raises(AppError, match="already on the roster"):
        project([{"add": 2001}, {"add": 2001}], team_=team(slots=9))


@pytest.mark.unit
def test_an_add_the_data_cannot_place_is_said_so(what_if):
    out = project([{"add": 2002, "drop": 1002, "day": 1}])
    assert out["moves"][0]["add_games"] == 0
    assert any("NBA team is unknown" in a for a in out["assumes"]) and any("no per-game value" in a for a in out["assumes"])


@pytest.mark.unit
def test_a_yahoo_bench_does_not_count_and_no_day_left_is_a_note(what_if):
    yahoo = LeagueInfo(provider=FantasyProvider.YAHOO, league_id=1, team_name="T", year=2027)
    what_if.resp = matchup([roster_player(*A), roster_player(102, 1002, "B", "LAL", 20.0, "BE")], projected=190.0)
    out = project([{"add": 2001, "drop": 1002, "day": 3}], league=yahoo)
    assert out["before"]["you"] == 190.0 and out["after"]["you"] == 100 + 90 + 50
    what_if.first_open = 7
    assert project([{"add": 2001}])["projection"] is None


def cats(you: dict, opp: dict):
    items = [CategoryScoreItem(key=c.key, label=c.label, you=you[c.key], opp=opp[c.key], winner="tie",
                               higher_is_better=c.higher_is_better, is_rate=c.is_rate) for c in CATEGORY.categories.categories]
    return CategoryComparison(items=items, wins=0, losses=0, ties=0)


@pytest.mark.unit
def test_a_categories_league_reports_each_categorys_totals_and_the_flips(what_if):
    what_if.scoring = CATEGORY
    totals = {"pts": 100.0, "reb": 50.0, "ast": 30.0, "stl": 10.0, "blk": 5.0, "tov": 10.0, "fg3m": 12.0,
              "fg_pct": 0.5, "ft_pct": 0.8}
    opp = {"pts": 195.0, "reb": 0.0, "ast": 0.0, "stl": 0.0, "blk": 0.0, "tov": 50.0, "fg3m": 0.0,
           "fg_pct": 0.4, "ft_pct": 0.7}
    raw = {"fgm": 50.0, "fga": 100.0, "ftm": 8.0, "fta": 10.0, "fg3m": 12.0, "fg3a": 30.0}
    what_if.resp = matchup(
        [roster_player(*A, line=StatLine(pts=20, reb=5)), roster_player(*B, line=StatLine(pts=10, tov=1))],
        scoring_format="categories", projected=8.0, opp_projected=1.0,
        categories=CategoryTeamScore(totals=totals, raw=raw), projected_categories=cats(totals, opp),
    )
    out = project([{"add": 2001, "drop": 1002, "day": 3}])
    pts = next(r for r in out["categories"] if r["category"] == "pts")
    # Before: 100 + A 20 x 3 + B 10 x 3 = 190, behind 195; after: B's day 1 only, FA 15 x 2: 200.
    assert (pts["projected_you"], pts["projected_you_after"], pts["projected_opponent"]) == (190.0, 200.0, 195.0)
    assert (pts["leader"], pts["leader_after"], pts["flips"]) == ("opp", "you", True)
    assert out["before"]["you"] == 8 and out["after"]["you"] == 9 and out["change"] == 1
    fg = next(r for r in out["categories"] if r["category"] == "fg_pct")
    assert fg["projected_you"] == 0.5 and fg["flips"] is False                   # rates from makes and attempts
    assert sum(r["flips"] for r in out["categories"]) == 1


# ---- plan_streams -------------------------------------------------------------------------


PG, C, UT, BE, IR = 0, 4, 11, 12, 13
D0 = date(2026, 11, 16)
TIP = datetime(2026, 11, 17, 0, 0, tzinfo=timezone.utc)
LANE_NOW = datetime(2026, 11, 16, 20, 0, tzinfo=timezone.utc)


def lane_day(i, *teams):
    return LaneDay(28 + i, date(2026, 11, 16 + i), frozenset(teams), TIP if teams else None)


def board_player(pid, name, slot, nba_team, *, eligible, avg, pos, injury=None, source="rolling"):
    return LineupPlayer(player_id=pid, name=name, team=nba_team, lineup_slot_id=slot, lineup_slot=str(slot),
                        eligible_slot_ids=list(eligible), eligible_slots=[str(s) for s in eligible], avg_points=avg,
                        injury_status=injury, default_position_id=pos, value_source=source)


HOLDER, GUARD, BIG, OUT = 1, 2, 3, 4
BOARD = LineupState(
    provider=FantasyProvider.ESPN, team_name="T", espn_team_id=1, nba_date="2026-11-16", scoring_period_id=28,
    slot_counts={"0": 1, "4": 1, "11": 1, "12": 3, "13": 1}, roster_version="v", fetched_at="now", can_write=True,
    players=[
        board_player(HOLDER, "Holder", UT, "MIA", eligible=(PG, UT, BE), avg=15, pos=1),
        board_player(GUARD, "Guard", PG, "DEN", eligible=(PG, UT, BE), avg=30, pos=1),
        board_player(BIG, "Big", C, "BOS", eligible=(C, UT, BE), avg=25, pos=5),
        board_player(OUT, "Out", IR, "BOS", eligible=(C, UT, BE, IR), avg=40, pos=5, injury="OUT"),
    ],
    acquisitions=AcquisitionState(per="matchup", limit=4, matchup_period_id=5, matchup_start=D0,
                                  matchup_end=date(2026, 11, 18), matchup_used=1, waiver_hours=24),
)
DAYS = {28: lane_day(0, "MIA", "DEN"), 29: lane_day(1, "LAL"), 30: lane_day(2, "MIA", "LAL")}


def fa(pid, nba_team, *, value, positions=("PG", "G"), pos=1):
    return PoolPlayer(pid, f"FA {pid}", nba_team, active_slots([*positions, "UT1"]), value, pos, None)


@pytest.fixture
def lanes(monkeypatch):
    st = SimpleNamespace(board=BOARD, pool=[fa(10, "LAL", value=22), fa(11, "LAL", value=35)], transactions=True,
                         scoring=POINTS, category_values={}, matchup=None, pending=[], named_pool=[], named_asked=None)

    async def direct(name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def read_lineup(team_id, league_info, *, fallback_slot_counts=None):
        return st.board

    async def pool(league_info, adapter, team_id):
        return st.pool

    async def evidence_for(league_info, espn_ids):
        return DropEvidence(games={i: 7 for i in espn_ids}, projected={})

    async def get_matchup(user_id, team_id, window="season"):
        return st.matchup

    async def named_pool(league_info, espn_ids):
        st.named_asked = espn_ids
        return st.named_pool

    monkeypatch.setattr(fantasy, "run_db", direct)
    monkeypatch.setattr(fantasy, "run_cpu", direct)
    monkeypatch.setattr(fantasy, "_named_pool", named_pool)
    monkeypatch.setattr(fantasy, "_identities", lambda ids: {i: fantasy._Identity(i, f"P{i}", i - 1000, "LAL") for i in ids})
    monkeypatch.setattr(fantasy, "get_provider_adapter", lambda provider: SimpleNamespace(
        capabilities=lambda li: SimpleNamespace(transactions=st.transactions), read_lineup=read_lineup))
    monkeypatch.setattr(fantasy, "_days", lambda start, end, base, base_date: [DAYS[p] for p in range(start, end + 1)])
    monkeypatch.setattr(fantasy, "_pending_pickups", lambda team_id: st.pending)
    monkeypatch.setattr(StreamLaneService, "_pool", staticmethod(pool))
    monkeypatch.setattr(fantasy, "nba_ids_by_espn_id", lambda ids: {e: 1000 + e for e in ids})
    monkeypatch.setattr(PlayerValueService, "scoring_for", staticmethod(lambda li, team_id=None: st.scoring))
    monkeypatch.setattr(fantasy, "_evidence_for", evidence_for)
    monkeypatch.setattr(fantasy, "_category_values",
                        lambda key, ids: fantasy._Valued({e: st.category_values.get(e, 0.0) for e in ids}, key, [], []))
    monkeypatch.setattr(fantasy.MatchupService, "get_matchup_by_team_id", staticmethod(get_matchup))
    return st


def plan(**kw):
    args = fantasy.PlanStreamsInput(team_id=7, **kw)
    return run(fantasy.plan_streams(42, team(), ESPN, args, now=LANE_NOW))


@pytest.mark.unit
def test_the_default_holder_is_the_rules_drop_and_the_plan_names_nba_ids(lanes):
    out = plan()
    # Holder 15 x 2 games (MIA days 0, 2), Guard 30 x 1, Big 25 x 0: the big's spot is streamed.
    (holder,) = out["holders"]
    assert (holder["player_id"], holder["name"], holder["games_left"]) == (1003, "Big", 0)
    assert holder["chosen_by"].startswith("the drop rule: 0 game(s) left at 25.0")
    assert out["value_kind"] == "league" and out["value_label"] == "value per game"
    assert (out["start"], out["end"], out["days_left"]) == (D0, date(2026, 11, 18), 3)
    # FA 11 (35, LAL days 1 and 2) takes the spot; the holder's day 0 is worth nothing to it anyway.
    assert [(r["day"], r["spot"], r["player_id"], r["value"], r["switch"]) for r in out["plan"]] == [
        (0, 1, 1003, 0.0, False), (1, 1, 1011, 35.0, True), (2, 1, 1011, 35.0, False)]
    (switch,) = out["switches"]
    assert (switch["day"], switch["drop_player_id"], switch["add_player_id"], switch["add_name"]) == (1, 1003, 1011, "FA 11")
    assert (out["projected_value"], out["hold_value"], out["gain"], out["adds"], out["optimal"]) == (70.0, 0.0, 70.0, 1, True)
    assert out["candidates_considered"] == 2 and out["weights"] is None and out["notes"] == []
    assert "espn" not in str(out) and 11 not in {r["player_id"] for r in out["plan"]}


@pytest.mark.unit
def test_named_holders_and_candidates(lanes):
    out = plan(holders=[1001, 1002], candidates=[1010, 1099])
    assert [(h["player_id"], h["chosen_by"]) for h in out["holders"]] == [(1001, "named"), (1002, "named")]
    assert len(out["plan"]) == 6 and any("1099 is not a free agent" in n for n in out["notes"])
    with pytest.raises(AppError, match="not on this roster"):
        plan(holders=[1099])
    with pytest.raises(AppError, match="on IR"):
        plan(holders=[1004])


@pytest.mark.unit
def test_a_candidate_named_outside_the_market_read_is_read_by_id(lanes):
    """The pool read is ESPN's most-owned free agents; a little-owned specialist the user names is
    read by his id and joins the pool. One the read does not return stays unavailable."""
    lanes.named_pool = [fa(50, "LAL", value=40)]
    out = plan(candidates=[1050, 1060])
    assert lanes.named_asked == [50, 60]
    assert (out["switches"][0]["add_player_id"], out["switches"][0]["add_name"]) == (1050, "FA 50")
    assert [n for n in out["notes"] if "not a free agent" in n] == [
        "Player 1060 is not a free agent the plan can take (rostered, out, or unknown)."]
    assert out["candidates_considered"] == 3 and out["projected_value"] == 80.0


@pytest.mark.unit
def test_a_holder_listed_out_keeps_his_games_in_the_rule(lanes):
    """Guard is out today with his one game left: the rule still weighs that game (30), so the big's
    empty spot goes first, as in the projection's drop rule; named, the plan counts him for nothing."""
    lanes.board = BOARD.model_copy(update={"players": [
        board_player(HOLDER, "Holder", UT, "MIA", eligible=(PG, UT, BE), avg=15, pos=1),
        board_player(GUARD, "Guard", PG, "DEN", eligible=(PG, UT, BE), avg=30, pos=1, injury="OUT"),
        board_player(BIG, "Big", C, "BOS", eligible=(C, UT, BE), avg=25, pos=5),
    ]})
    out = plan()
    assert out["holders"][0]["player_id"] == 1003 and out["notes"] == []
    out = plan(holders=[1002])
    assert out["holders"][0]["games_left"] == 1 and any("listed OUT today" in n for n in out["notes"])
    assert out["plan"][0]["value"] == 0.0                                    # his game today counts for nothing


@pytest.mark.unit
def test_a_player_a_scheduled_pickup_drops_is_not_a_holder(lanes):
    """A pickup already scheduled drops the big on day 1: his spot is that pickup's, so the rule skips him
    (Holder 15 x 2 and Guard 30 x 1 tie at 30; the fewer games left go first) and naming him is refused."""
    lanes.pending = [PendingPickup(1, 29, date(2026, 11, 17), 10, BIG)]
    out = plan()
    (holder,) = out["holders"]
    assert (holder["player_id"], holder["name"]) == (1002, "Guard")
    assert out["switches"][0]["add_player_id"] == 1011                        # FA 10 is the pickup's, not the plan's
    with pytest.raises(AppError, match="scheduled to be dropped on 2026-11-17"):
        plan(holders=[1003])


@pytest.mark.unit
def test_a_category_value_drives_both_the_holder_choice_and_the_plan(lanes):
    lanes.category_values = {HOLDER: 0.5, GUARD: 0.1, BIG: 2.0, 10: 3.0, 11: 0.0}
    out = plan(value="blk")
    # Under blocks: Holder 0.5 x 2 = 1, Guard 0.1 x 1, Big 2 x 0 = 0: the big's spot; FA 10 (3 blocks) takes it.
    assert out["holders"][0]["player_id"] == 1003 and out["holders"][0]["value_per_game"] == 2.0
    assert out["switches"][0]["add_player_id"] == 1010 and out["value_label"] == "blocks per game"
    assert out["projected_value"] == 6.0


@pytest.mark.unit
def test_the_matchup_value_needs_a_categories_league(lanes):
    out = plan(value="matchup")
    assert out["value_kind"] == "league" and any("points league" in n for n in out["notes"])


@pytest.mark.unit
def test_the_matchup_value_weighs_the_close_categories(lanes, monkeypatch):
    lanes.scoring = CATEGORY
    totals = {k: 10.0 for k in CATEGORY.categories.keys}
    opp = dict(totals, pts=10.5, blk=200.0)                          # points a dead heat, blocks long gone
    lanes.matchup = matchup([], scoring_format="categories", projected_categories=cats(totals, opp))
    seen = {}

    def matchup_values(scoring, margins, games):
        seen.update(margins=margins, games=games)
        return fantasy._Valued({HOLDER: 1.0, GUARD: 1.0, BIG: 1.0, 10: 9.0, 11: 2.0}, "matchup",
                               [{"category": "pts", "label": "PTS", "margin": -0.5, "weight": 1.0}], [])
    monkeypatch.setattr(fantasy, "_matchup_values", matchup_values)
    out = plan(value="matchup")
    assert seen["margins"]["pts"] == (10.0, 10.5, True) and seen["games"] == 5 / 30
    assert out["weights"][0]["category"] == "pts" and out["switches"][0]["add_player_id"] == 1010


@pytest.mark.unit
def test_a_provider_without_transactions_is_refused(lanes):
    lanes.transactions = False
    with pytest.raises(AppError, match="ESPN league"):
        plan()


@pytest.mark.unit
def test_the_pool_is_capped_to_the_best_under_the_value_plus_the_named(lanes, monkeypatch):
    lanes.pool = [fa(100 + i, "LAL", value=float(200 - i)) for i in range(100)]
    seen, original = {}, fantasy.plan_spots

    def plan_spots(board, holder_ids, pool, *rest):
        seen["pool"] = [f.player_id for f in pool]
        return original(board, holder_ids, pool[:3], *rest)
    monkeypatch.setattr(fantasy, "plan_spots", plan_spots)
    plan(candidates=[1199])
    assert len(seen["pool"]) == fantasy.POOL_CAP + 1 and seen["pool"][-1] == 199 and seen["pool"][0] == 100
    # A player a scheduled pickup brings in stays in the pool too: the plan's roster-to-be needs his games.
    lanes.pending = [PendingPickup(1, 29, date(2026, 11, 17), 198, None)]
    plan(candidates=[1199])
    assert len(seen["pool"]) == fantasy.POOL_CAP + 2 and seen["pool"][-2:] == [198, 199]
