"""
The cutover from Dime's top-six rule to the user's protections (James, 2026-11-15): which draft
picks a team's seed protects (`roster_protection.draft_core` and `seed`), when a check-in's start
runs it, and Dime's read of the league's add limit (`get_add_limits`). The database side (the
seed stored once, what the user is already moving out) is in
tests/integration/test_roster_protections_integration.py.
"""

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from core.settings import settings
from schemas.common import FantasyProvider, LeagueInfo
from schemas.lineup_editor import AcquisitionState, LineupPlayer, LineupState
from services import roster_protection
from services.ai import adds, checkins

pytestmark = pytest.mark.unit

ESPN = LeagueInfo(provider=FantasyProvider.ESPN, league_id=552315826, team_name="T", year=2027)
YAHOO = LeagueInfo(provider=FantasyProvider.YAHOO, league_id=1, team_name="T", year=2027)
TEAM = SimpleNamespace(team_id=21, user_id=11, league_info_json="{}", league_id=None, league=None)


def run(coro):
    return asyncio.run(coro)


def pick(n, team, player, bid=None):
    out = {"overallPickNumber": n, "teamId": team, "playerId": player}
    return {**out, "bidAmount": bid} if bid is not None else out


def draft(*picks, kind="SNAKE"):
    return {"settings": {"draftSettings": {"type": kind}}, "draftDetail": {"picks": list(picks)}}


def player(pid, name, value=0.0):
    return LineupPlayer(player_id=pid, nba_player_id=pid + 1000, name=name, team="MIN", lineup_slot_id=12,
                        lineup_slot="BE", eligible_slot_ids=[12], eligible_slots=["BE"], avg_points=value)


def board(players, acq=None, espn_team_id=1):
    return LineupState(provider=FantasyProvider.ESPN, team_name="T", espn_team_id=espn_team_id,
                       nba_date="2026-11-16", scoring_period_id=28, roster_version="v", fetched_at="now",
                       can_write=True, players=players, acquisitions=acq)


# ---- which picks ----


def test_the_core_is_the_team_s_first_picks_in_pick_order():
    payload = draft(pick(38, 1, 30), pick(3, 1, 10), pick(4, 2, 99), pick(18, 1, 20), pick(53, 1, -1))
    assert roster_protection.draft_core(payload, 1, 6) == [10, 20, 30]
    assert roster_protection.draft_core(payload, 1, 2) == [10, 20]


def test_an_auction_s_core_is_by_price():
    payload = draft(pick(1, 1, 10, bid=12), pick(2, 1, 20, bid=55), pick(3, 1, 30, bid=40), kind="AUCTION")
    assert roster_protection.draft_core(payload, 1, 2) == [20, 30]


def test_no_draft_or_no_team_is_no_core():
    assert roster_protection.draft_core({"draftDetail": {"picks": [pick(1, 1, -1)]}}, 1, 6) == []
    assert roster_protection.draft_core({}, 1, 6) == []
    assert roster_protection.draft_core(draft(pick(1, 1, 10)), None, 6) == []


# ---- the seed, the database stubbed ----


@pytest.fixture
def espn(monkeypatch):
    """The board, ESPN's draft and the repository faked; `h.stored` is what the seed stored."""
    from services import providers
    from services.providers import http

    h = SimpleNamespace(board=board([]), payload=draft(), moving=frozenset(), stored=None)

    class Adapter:
        async def read_lineup(self, team_id, league_info, **kw):
            return h.board

    async def provider_get(provider, url, **kw):
        return h.payload

    async def run_db(label, fn, *args):
        if fn is roster_protection._moving_out:
            return h.moving
        assert fn is roster_protection._store_seed
        h.stored = [p.name for p in args[2]]
        return h.stored

    monkeypatch.setattr(providers, "get_provider_adapter", lambda provider: Adapter())
    monkeypatch.setattr(http, "provider_get", provider_get)
    monkeypatch.setattr(roster_protection, "run_db", run_db)
    return h


def test_the_seed_protects_the_first_picks_still_on_the_roster_less_what_the_user_moves_out(espn):
    espn.board = board([player(10, "Jokic"), player(30, "Edwards"), player(40, "Kawhi"), player(50, "Streamer")])
    espn.payload = draft(pick(3, 1, 10), pick(18, 1, 20), pick(23, 1, 40), pick(38, 1, 30), pick(83, 1, 50))
    espn.moving = frozenset({40})          # a pickup of the user's drops Kawhi
    assert run(roster_protection.seed(TEAM, ESPN, n=4)) == ["Jokic", "Edwards"]


def test_a_league_with_no_draft_on_espn_takes_the_roster_s_top_by_value(espn):
    espn.board = board([player(10, "Low", 5.0), player(20, "Best", 40.0), player(30, "Next", 30.0)])
    assert run(roster_protection.seed(TEAM, ESPN, n=2)) == ["Best", "Next"]


def test_a_team_off_espn_is_marked_with_none(espn):
    assert run(roster_protection.seed(TEAM, YAHOO)) == [] and espn.stored == []


def test_the_default_count_is_the_setting(espn, monkeypatch):
    monkeypatch.setattr(settings, "autopilot_seed_protected", 1)
    espn.board = board([player(10, "Jokic"), player(20, "Edwards")])
    espn.payload = draft(pick(3, 1, 10), pick(18, 1, 20))
    assert run(roster_protection.seed(TEAM, ESPN)) == ["Jokic"]


# ---- when a check-in's start runs it ----


def pilot(**kw):
    base = dict(team_id=21, user_id=11, ai_rows=[], autopilot=False, restore=None, paused_at=None,
                paused_by=None, paused_reason=None, protections_seeded_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def seeds(monkeypatch):
    calls = []

    async def seed_for(team_id, user_id):
        calls.append((team_id, user_id))
        if calls and getattr(seed_for, "fail", False):
            raise RuntimeError("ESPN timed out")
        return []

    monkeypatch.setattr(roster_protection, "seed_for", seed_for)
    for k, v in dict(ai_enabled=True, autopilot_enabled=True, ai_agent_user_ids=[11]).items():
        monkeypatch.setattr(settings, k, v)
    return SimpleNamespace(calls=calls, seed_for=seed_for)


@pytest.mark.parametrize("rows", [["daily_actions"], ["lanes"], ["late_scratch"], ["lineup", "lanes"]])
def test_a_team_dime_drops_players_on_is_seeded_at_its_check_in_s_start(seeds, rows):
    run(checkins._seed_protections(21, 11, pilot(ai_rows=rows)))
    assert seeds.calls == [(21, 11)]


def test_no_seed_for_lineup_only_a_team_already_seeded_or_a_paused_one(seeds):
    run(checkins._seed_protections(21, 11, pilot(ai_rows=["lineup"])))
    run(checkins._seed_protections(21, 11, pilot(ai_rows=["daily_actions"], protections_seeded_at=date(2026, 11, 16))))
    run(checkins._seed_protections(21, 11, pilot(ai_rows=["daily_actions"], paused_at=date(2026, 11, 16))))
    run(checkins._seed_protections(21, 11, None))
    assert seeds.calls == []


def test_a_failed_seed_lets_the_check_in_go_on(seeds):
    seeds.seed_for.fail = True
    run(checkins._seed_protections(21, 11, pilot(ai_rows=["daily_actions"])))        # no raise
    assert seeds.calls == [(21, 11)]


# ---- get_add_limits ----


WEEK = dict(matchup_period_id=5, matchup_start=date(2026, 11, 16), matchup_end=date(2026, 11, 22))


@pytest.fixture
def limits(monkeypatch):
    from services import providers
    from services.ai import acting

    h = SimpleNamespace(acq=None, pending=[])

    class Adapter:
        async def read_lineup(self, team_id, league_info, **kw):
            return board([], acq=h.acq)

    async def pending(team_id):
        return h.pending

    monkeypatch.setattr(providers, "get_provider_adapter", lambda provider: Adapter())
    monkeypatch.setattr(acting, "_pending_days", pending)
    return h


def test_the_matchup_s_adds_made_anywhere_and_what_is_left(limits):
    limits.acq = AcquisitionState(per="game_day", per_game_day=1, game_days=7, limit=7, matchup_used=4, **WEEK)
    limits.pending = [date(2026, 11, 18), date(2026, 11, 23)]       # one this matchup, one the next
    out = run(adds.get_add_limits(TEAM, ESPN))
    assert out["matchup"] == {"period": 5, "start": "2026-11-16", "end": "2026-11-22"}
    assert (out["per"], out["adds_per_game_day"], out["days_with_games_this_matchup"]) == ("game_day", 1, 7)
    assert (out["this_matchup"], out["team_adds_this_matchup"], out["left_this_matchup"]) == (7, 4, 3)
    assert (out["scheduled_this_matchup"], out["left_after_scheduled"]) == (1, 2)


def test_a_league_without_a_matchup_limit_still_says_how_many_were_made(limits):
    limits.acq = AcquisitionState(per="matchup", limit=None, matchup_used=9, season_limit=60, season_used=40, **WEEK)
    out = run(adds.get_add_limits(TEAM, ESPN))
    assert (out["limit"], out["team_adds_this_matchup"], out["season_left"]) == (None, 9, 20)
    assert "left_after_scheduled" not in out


def test_unread_settings_say_so_and_a_yahoo_team_is_refused(limits):
    from core.errors import AppError
    from services.ai import acting
    assert run(adds.get_add_limits(TEAM, ESPN)) == {"team_id": 21, **acting.LIMITS_UNREAD}
    with pytest.raises(AppError):
        run(adds.get_add_limits(TEAM, YAHOO))


def test_the_tool_s_definition_matches_its_input():
    assert set(adds.GET_ADD_LIMITS["input_schema"]["properties"]) == set(adds.GetAddLimitsInput.model_fields)
    assert adds.GET_ADD_LIMITS["input_schema"]["additionalProperties"] is False
    assert not any(ch.isdigit() for ch in adds.GET_ADD_LIMITS["description"])
