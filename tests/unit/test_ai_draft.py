"""
Dime in the Draft desk (the Dime autopilot plan, step 7): the numbers a pick note shows
beside Dime's words, the briefs each note starts from, which rooms Dime can hold the
user's seat in, the recap tool's read of a graded draft, the tool lists (the draft tools
only in a draft note's conversation, the recap everywhere, appended last), and the
switch. The rows, the claims and the seat run against the real schema in
tests/integration/test_dime_draft_integration.py.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from core.errors import ServiceUnavailableError
from core.settings import settings
from services.ai import agent, draft, tools

pytestmark = [pytest.mark.unit]


def run(coro):
    return asyncio.run(coro)


def _room(**kw):
    base = dict(session_id=7, user_id=42, team_id=23, league_id=1, league=None, kind="mock", status="active",
                draft_type="snake", pick_order=(11, 12, 13, 14), my_slot=3, rounds=13, punts=(),
                scoring_format=None, espn_league_id=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _detail(**kw):
    base = dict(name="Atlas mock", kind="mock", status="active", scoring_format="categories", league_size=4,
                rounds=13, my_slot=3, my_next_pick=11, picks_until_my_turn=0, pick_count=10, total_picks=52,
                draft_type="snake", picks=[])
    base.update(kw)
    return SimpleNamespace(**base)


def _board(**kw):
    rec = {"player_id": 1, "name": "Victor Wembanyama", "position": "C", "team": "SAS", "score": 48.2,
           "vorp": 40.1, "season_value": 61.0, "value": 0.9, "availability": "gone", "injury": None, "adp": 2.1,
           "cv_rank": 1, "market_rank": 2, "reason": "Best value left",
           "components": [{"key": "vorp", "label": "Value over replacement", "value": 40.1, "in_score": True,
                           "detail": None}]}
    second = {**rec, "player_id": 2, "name": "Tyrese Haliburton", "position": "PG", "team": "IND", "score": 41.0,
              "availability": "likely", "adp": 14.0, "reason": "Assists"}
    base = {
        "room_id": 7, "picks_made": 10, "my_next_pick": 11, "picks_until_my_turn": 0, "punts": ["ft_pct"],
        "recommendations": [rec, second, {**second, "player_id": 3, "name": "Third"},
                            {**second, "player_id": 4, "name": "Fourth"}],
        "best_available": [
            {"board_rank": 1, "player_id": 1, "name": "Victor Wembanyama", "positions": "C", "adp": 2.1,
             "availability": "gone"},
            {"board_rank": 2, "player_id": 2, "name": "Tyrese Haliburton", "positions": "PG", "adp": 14.0,
             "availability": "likely"},
            {"board_rank": 3, "player_id": 5, "name": "Mikal Bridges", "positions": "SF", "adp": 9.0,
             "availability": "gone"},
        ],
        "roster": [{"player_id": 9, "name": "Jalen Brunson", "team": "NYK", "position": "PG", "value": 0.7}],
        "category_need": [
            {"category": "blk", "label": "BLK", "need": 1.4, "punted": False, "my_rank": 4, "of": 4},
            {"category": "ft_pct", "label": "FT%", "need": 2.0, "punted": True, "my_rank": 4, "of": 4},
            {"category": "reb", "label": "REB", "need": 0.6, "punted": False, "my_rank": 3, "of": 4},
        ],
    }
    base.update(kw)
    return base


# ------------------------------- facts ------------------------------- #


class TestFacts:
    def test_on_the_clock_gone_is_about_the_turn_after_this_one(self):
        facts = draft.facts_of(_board(), following=22)
        assert facts["on_clock"] is True and facts["pick"] == 11 and facts["gone_before"] == 22
        assert [t["name"] for t in facts["top"]] == ["Victor Wembanyama", "Tyrese Haliburton", "Third"]
        assert facts["top"][0]["score"] == 48.2 and facts["top"][0]["availability"] == "gone"
        assert [g["name"] for g in facts["gone"]] == ["Victor Wembanyama", "Mikal Bridges"]
        assert facts["punts"] == ["ft_pct"] and facts["roster"] == 1

    def test_before_the_pick_gone_is_about_the_pick_itself(self):
        facts = draft.facts_of(_board(picks_until_my_turn=2), following=22)
        assert facts["on_clock"] is False and facts["gone_before"] == 11 and facts["picks_until"] == 2

    def test_needs_are_the_weakest_categories_left_out_of_the_punts(self):
        facts = draft.facts_of(_board(), following=None)
        assert [n["category"] for n in facts["needs"]] == ["blk", "reb"]

    def test_an_empty_board_has_nothing_to_show(self):
        facts = draft.facts_of({"recommendations": [], "best_available": []}, following=None)
        assert facts["top"] == [] and facts["gone"] == [] and facts["needs"] == [] and facts["roster"] == 0


class TestFollowingPick:
    def test_a_snake_s_next_turn_comes_back_from_the_turn(self):
        # Seat 3 of 4: picks 3, 6, 11, 14 ...
        assert draft.following_pick(_detail(my_next_pick=11)) == 14
        assert draft.following_pick(_detail(my_next_pick=6)) == 11

    def test_none_without_a_turn_or_past_the_last_one(self):
        assert draft.following_pick(_detail(my_next_pick=None)) is None
        assert draft.following_pick(_detail(my_next_pick=51, total_picks=52)) is None


# ------------------------------- briefs ------------------------------- #


class TestBriefs:
    def test_a_pick_note_carries_the_board_the_plan_and_says_who_picks(self):
        plan = SimpleNamespace(plan="Punt FT%, bigs early", punts=["ft_pct"])
        text = draft.brief(draft.PICK, _room(), _detail(), board=_board(), plan=plan, for_pick=11)
        assert text.startswith('[Pick note for room_id 7 ("Atlas mock"): a mock draft, categories scoring, 4 teams')
        assert "pick #11 (round 3) is now: the user is on the clock" in text
        assert "you don't make the pick" in text
        assert 'The room\'s plan: "Punt FT%, bigs early" (punts: ft_pct).' in text
        board = json.loads(text.split("cite these numbers):\n", 1)[1].split("\n\n", 1)[0])
        assert board["recommendations"][0]["name"] == "Victor Wembanyama"
        assert board["recommendations"][0]["components"] == [
            {"label": "Value over replacement", "value": 40.1, "in_score": True}]
        assert board["category_need"][0]["category"] == "blk"
        assert "draft_pick" not in text

    def test_a_pick_note_ahead_of_the_pick_says_how_far(self):
        text = draft.brief(draft.PICK, _room(), _detail(), board=_board(picks_until_my_turn=2), for_pick=11)
        assert "is in 2 picks" in text and "No plan is set for this room." in text

    def test_the_seat_s_brief_hands_it_the_pick(self):
        text = draft.brief(draft.PICK, _room(), _detail(), board=_board(), seat=True, for_pick=11)
        assert text.startswith("[You hold the user's seat in room_id 7")
        assert "draft_pick once" in text and "nothing here reaches ESPN" in text

    def test_a_plan_in_a_mock_stays_in_the_room_and_a_real_one_is_saved(self):
        mock = draft.brief(draft.PLAN, _room(), _detail())
        assert "set_draft_plan once" in mock and "stays in the room" in mock
        assert "recall your notes on team_id 23" in mock
        live = draft.brief(draft.PLAN, _room(kind="live"), _detail(kind="live"))
        assert "saved to the team's notes" in live
        bare = draft.brief(draft.PLAN, _room(team_id=None), _detail())
        assert "no team (a room with generic settings)" in bare and "earlier drafts" in bare

    def test_a_recap_in_a_real_draft_writes_the_season_s_start_and_habits(self):
        live = draft.brief(draft.RECAP, _room(kind="live"), _detail(kind="live"))
        assert "get_draft_recap for the room" in live and "opponent note" in live and "plan note" in live
        mock = draft.brief(draft.RECAP, _room(), _detail())
        assert "This is a mock" in mock and "opponent note" not in mock

    def test_titles_name_the_room_and_the_note(self):
        assert draft.title_of(draft.PLAN, _detail()) == "Dime · Atlas mock · plan"
        assert draft.title_of(draft.PICK, _detail(name=None), for_pick=11) == "Dime · Mock draft · pick 11"
        assert draft.title_of(draft.PICK, _detail(), for_pick=11, seat=True) == "Dime · Atlas mock · took pick 11"


# ------------------------------- the seat ------------------------------- #


class TestSeatable:
    def test_an_unlinked_active_snake_mock_with_a_seat_is_draftable(self):
        assert draft.seatable(_room()) is None

    @pytest.mark.parametrize("change, words", [
        ({"kind": "live"}, "only in a mock room"),
        ({"espn_league_id": 999}, "follows an ESPN lobby"),
        ({"status": "completed"}, "finished"),
        ({"draft_type": "auction"}, "snake rooms only"),
        ({"rounds": None}, "pick order and rounds"),
        ({"pick_order": ()}, "pick order and rounds"),
        ({"my_slot": None}, "Set your seat"),
    ])
    def test_every_other_room_says_why(self, change, words):
        assert words in draft.seatable(_room(**change))


# ------------------------------- the recap ------------------------------- #


def _pick(n, slot, adp, *, by_me=False, source="mock", vos=0.0):
    return SimpleNamespace(overall_pick=n, round=(n - 1) // 4 + 1, slot=slot, by_me=by_me, source=source,
                           player_id=100 + n, player_name=f"P{n}", adp=adp,
                           surplus_market=(adp - n) if adp is not None else None, value_over_slot=vos)


class TestRecap:
    def _recap(self, *, categories=True):
        picks = [_pick(1, 1, 1.0), _pick(2, 2, 15.0), _pick(3, 3, 3.0, by_me=True, source="dime", vos=1.5),
                 _pick(4, 4, 4.0), _pick(5, 4, 30.0), _pick(6, 3, 6.0, by_me=True, vos=-0.4),
                 _pick(7, 2, 2.0), _pick(8, 1, None)]
        seats = [SimpleNamespace(slot=s, espn_team_id=None, is_me=s == 3, picks=2, grade="ABCD"[s - 1],
                                 position=float(s), value_over_slot=2.0 - s) for s in (1, 2, 3, 4)]
        cats = [SimpleNamespace(key=k, label=k.upper(), rank=r) for k, r in (("pts", 1), ("reb", 3), ("blk", 2),
                                                                              ("ft_pct", 4))]
        standings = [SimpleNamespace(slot=s, categories=cats if categories else [], roto_rank=float(s),
                                     season_value=None if categories else 900.0 - s, value_rank=float(s))
                     for s in (1, 2, 3, 4)]
        meta = SimpleNamespace(format="categories" if categories else "points", complete=False, picks_made=8,
                               my_slot=3, graded_by="value_over_slot")
        return SimpleNamespace(data=picks, seats=seats, standings=standings, meta=meta, message="")

    def test_each_seat_says_how_far_it_reached_ahead_of_adp(self):
        out = draft.recap_of(self._recap(), league_team_ids=(11, 12, 13, 14))
        by = {s["slot"]: s for s in out["seats"]}
        # Seat 2 took ADP 15 at 2 (a steal of 13) and ADP 2 at 7 (a reach of 5): it waited 4 on average
        assert by[2]["reach"] == -4.0 and by[2]["steals"] == [{"pick": 2, "name": "P2", "adp": 15.0}]
        assert by[2]["reaches"] == [] and by[2]["league_team_id"] == 12
        # Seat 4 took ADP 30 at 5: a steal, and nothing ahead of ADP
        assert by[4]["steals"][0]["pick"] == 5 and by[4]["reach"] == -12.5
        # A pick with no ADP is left out of the average, not counted as on time
        assert by[1]["reach"] == 0.0
        assert by[3]["mine"] is True and by[3]["grade"] == "C" and by[3]["place"] == 3.0

    def test_category_leagues_name_each_seat_s_strongest_and_weakest(self):
        seat = draft.recap_of(self._recap())["seats"][0]
        assert seat["strongest"] == ["PTS", "BLK"] and seat["weakest"] == ["REB", "FT_PCT"]
        points = draft.recap_of(self._recap(categories=False))["seats"][0]
        assert points["season_value"] == 899.0 and "strongest" not in points

    def test_the_user_s_picks_say_which_dime_made(self):
        out = draft.recap_of(self._recap())
        assert [(p["pick"], p["by_dime"]) for p in out["my_picks"]] == [(3, True), (6, False)]
        assert out["format"] == "categories" and out["picks_made"] == 8 and out["complete"] is False

    def test_a_recap_with_no_seats_says_so(self):
        empty = SimpleNamespace(data=[], seats=[], standings=[], meta=None, message="No picks yet")
        assert draft.recap_of(empty)["note"] == "No picks yet"


# ------------------------------- the tools ------------------------------- #


class TestTools:
    def test_the_recap_is_appended_to_every_tool_list_and_the_draft_tools_to_a_draft_note_s_only(self):
        assert tools.AGENT_TOOLS[-1]["name"] == "get_draft_recap"
        assert [t["name"] for t in agent.DRAFT_TOOLS[-2:]] == ["set_draft_plan", "draft_pick"]
        # A draft note's list is the AI desk's with the two appended, so the cached prefix is shared
        assert agent.DRAFT_TOOLS[:len(agent.TOOLS)] == agent.TOOLS
        for allowed in (agent.ALLOWED, agent.CHECKIN_ALLOWED):
            assert "get_draft_recap" in allowed
            assert not ({"set_draft_plan", "draft_pick"} & allowed)
        assert {"get_draft_recap", "set_draft_plan", "draft_pick"} <= agent.DRAFT_ALLOWED

    @pytest.mark.parametrize("tool, model", [
        (draft.GET_DRAFT_RECAP, draft.DraftRecapInput),
        (draft.SET_DRAFT_PLAN, draft.SetDraftPlanInput),
        (draft.DRAFT_PICK, draft.DraftPickInput),
    ])
    def test_the_definitions_match_the_inputs(self, tool, model):
        assert set(tool["input_schema"]["properties"]) == set(model.model_fields)
        assert set(tool["input_schema"]["required"]) == {n for n, f in model.model_fields.items() if f.is_required()}
        assert tool["input_schema"]["additionalProperties"] is False
        assert tool["name"] in agent.STATUS

    def test_the_plan_is_one_line_and_never_empty(self):
        assert draft.SetDraftPlanInput(room_id=1, plan="  punt   FT%\n bigs early ").plan == "punt FT% bigs early"
        with pytest.raises(ValueError):
            draft.SetDraftPlanInput(room_id=1, plan="   ")

    def test_outside_a_draft_note_the_plan_and_the_pick_are_refused(self):
        ctx = tools.ToolContext(user_id=42, conversation_id=5)
        out = run(tools.run_tool("set_draft_plan", {"room_id": 7, "plan": "x"}, ctx=ctx, allowed=agent.DRAFT_ALLOWED))
        assert out.is_error and "only works in Dime's plan note" in out.content
        out = run(tools.run_tool("draft_pick", {"room_id": 7, "player_id": 1, "reason": "x"}, ctx=ctx,
                                 allowed=agent.DRAFT_ALLOWED))
        assert out.is_error and "only works in Dime's pick note" in out.content
        # And the AI desk's own list doesn't have them at all
        out = run(tools.run_tool("draft_pick", {"room_id": 7, "player_id": 1, "reason": "x"}, ctx=ctx,
                                 allowed=agent.ALLOWED))
        assert out.is_error and out.content == "Unknown tool: draft_pick"

    def test_limits_are_a_short_leash(self):
        limits = agent.Limits.draft()
        assert limits.model_calls == settings.dime_draft_max_model_calls
        assert limits.timeout_seconds == settings.dime_draft_timeout_seconds
        assert limits.model_calls < agent.Limits.chat().model_calls


# ------------------------------- the switch ------------------------------- #


class TestSwitch:
    def test_off_the_room_shows_no_dime_and_starts_are_refused(self, monkeypatch):
        monkeypatch.setattr(settings, "ai_enabled", True)
        monkeypatch.setattr(settings, "ai_agent_user_ids", [42])
        monkeypatch.setattr(settings, "dime_draft_enabled", False)
        view = run(draft.room_view(42, _room()))
        assert view["enabled"] is False and view["available"] is False and view["notes"] == []
        with pytest.raises(ServiceUnavailableError) as err:
            run(draft.start(42, _room(), draft.PLAN))
        assert err.value.error_code == "AI_DRAFT_DISABLED"
        with pytest.raises(ServiceUnavailableError):
            run(draft.set_seat(42, _room(), True))

    def test_on_a_user_off_the_ai_desk_s_list_sees_no_dime(self, monkeypatch):
        monkeypatch.setattr(settings, "ai_enabled", True)
        monkeypatch.setattr(settings, "ai_agent_user_ids", [1])
        monkeypatch.setattr(settings, "dime_draft_enabled", True)
        view = run(draft.room_view(42, _room()))
        assert view["enabled"] is True and view["available"] is False
