"""
services/ai/steering.py: Dime's steering chips (the Dime autopilot plan, step 4), without
Postgres, ESPN or the model. The board is a prebuilt LineupState and the repository
functions are in-memory fakes.

Covers the inputs, the three chips (steer checks each hold against the roster and the days,
notes what it overlaps; set_lane plans a start as the Week desk's preview does and checks a
lane's state; cancel_pickup needs a pending pickup of the team's), their two shapes (the
model's view with NBA ids only, the frontend's chip with ESPN ids, which the API schema
takes), applying one (`for_apply` refuses anything but this very chip, still proposed, while
steering is on), and the re-check before applying.
"""

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core.errors import AppError, ConflictError, NotFoundError
from core.settings import settings
from schemas.ai import AiProposalChip
from services import steering as rules
from services.ai import decisions, proposals
from services.ai import steering as mod
from tests.unit.test_ai_proposals import (
    EDWARDS, JOKIC, KAWHI, LEAGUE, NBA_EDWARDS, NBA_JOKIC, NBA_KAWHI, NOW, TEAM, TODAY, WEEK, board, row, state,
)

TOMORROW = date(2026, 10, 22)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(settings, "dime_steering_enabled", True)


def live(**kw):
    base = dict(id=5, kind="keep", player_id=JOKIC, nba_player_id=NBA_JOKIC, player_name="Nikola Jokic",
                player_team="DEN", share=None, starts_on=TODAY, ends_on=TOMORROW, reason="Top-5 player",
                created_at=NOW, applied_by="user", proposal_id=None)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def chips(monkeypatch, on):
    h = SimpleNamespace(board=state(board()), live=[], pending=[], inserted=[])
    identities = {
        NBA_KAWHI: SimpleNamespace(nba_id=NBA_KAWHI, name="Kawhi Leonard", espn_id=KAWHI, team="LAC"),
        NBA_EDWARDS: SimpleNamespace(nba_id=NBA_EDWARDS, name="Anthony Edwards", espn_id=EDWARDS, team="MIN"),
        NBA_JOKIC: SimpleNamespace(nba_id=NBA_JOKIC, name="Nikola Jokic", espn_id=JOKIC, team="DEN"),
        1: SimpleNamespace(nba_id=1, name="No Espn", espn_id=None, team=None),
    }

    async def direct(name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def read_board(team, league_info):
        return h.board

    async def live_rows(team_id, today=None):
        return list(h.live)

    async def insert(**fields):
        h.inserted.append(fields)
        return row(**{**fields, "id": 41})

    monkeypatch.setattr(mod, "run_db", direct)
    monkeypatch.setattr(mod, "_identities", lambda ids: {i: identities[i] for i in ids if i in identities})
    monkeypatch.setattr(mod, "_pending_pickups", lambda team_id: list(h.pending))
    monkeypatch.setattr(proposals, "_board", read_board)
    monkeypatch.setattr(proposals, "_insert", insert)
    monkeypatch.setattr(rules, "live_rows", live_rows)
    monkeypatch.setattr(mod.schedule_service, "get_nba_today", lambda: TODAY)
    monkeypatch.setattr(mod.schedule_service, "get_current_matchup", lambda today: dict(WEEK))
    return h


def steer(**kw):
    kw.setdefault("reason", "Jokic is the team; Kawhi's minutes are capped")
    return run(mod.steer(11, 7, TEAM, LEAGUE, mod.SteerInput(team_id=21, **kw), NOW))


# ---- inputs ---------------------------------------------------------------------------------


@pytest.mark.unit
def test_the_inputs_name_what_they_change():
    with pytest.raises(ValueError, match="set a hold, clear one"):
        mod.SteerInput(team_id=21, reason="nothing to do")
    with pytest.raises(ValueError, match="needs share"):
        mod.HoldInput(kind="override", player_id=1)
    with pytest.raises(ValueError, match="changes nothing"):
        mod.HoldInput(kind="override", player_id=1, share=1)
    with pytest.raises(ValueError, match="override only"):
        mod.HoldInput(kind="keep", player_id=1, share=0.5)
    with pytest.raises(ValueError, match="before start"):
        mod.HoldInput(kind="keep", player_id=1, start=TOMORROW, until=TODAY)
    with pytest.raises(ValueError, match="one hold of a kind"):
        mod.SteerInput(team_id=21, reason="twice", holds=[{"kind": "keep", "player_id": 1}] * 2)
    with pytest.raises(ValueError, match="names the roster player"):
        mod.SetLaneInput(team_id=21, action="start", lane_id=3, reason="stream it")
    with pytest.raises(ValueError, match="names the lane"):
        mod.SetLaneInput(team_id=21, action="pause", player_id=3, reason="hold off")
    assert mod.HoldInput(kind="override", player_id=1, share=0).share == 0


@pytest.mark.unit
def test_the_tools_are_defined_as_their_inputs_take_them():
    for tool, model in ((mod.STEER_TOOL, mod.SteerInput), (mod.SET_LANE_TOOL, mod.SetLaneInput),
                        (mod.CANCEL_PICKUP_TOOL, mod.CancelPickupInput)):
        props = set(tool["input_schema"]["properties"])
        assert props == set(model.model_fields), tool["name"]
        assert set(tool["input_schema"]["required"]) <= props
    assert set(mod.STEER_TOOL["input_schema"]["properties"]["holds"]["items"]["properties"]) == \
        set(mod.HoldInput.model_fields)


# ---- steer ----------------------------------------------------------------------------------


@pytest.mark.unit
def test_a_steer_chip_carries_each_hold_with_espn_ids_and_what_it_does(chips):
    out = steer(holds=[{"kind": "keep", "player_id": NBA_JOKIC, "until": "2026-10-23"},
                       {"kind": "block", "player_id": NBA_KAWHI},
                       {"kind": "override", "player_id": NBA_EDWARDS, "share": 0}])
    stored = chips.inserted[0]
    assert stored["kind"] == "steer" and stored["state"] == "proposed" and stored["nba_date"] == TODAY
    holds = stored["action"]["holds"]
    assert [(h["kind"], h["player"]["espn_id"], h["starts_on"], h["ends_on"]) for h in holds] == [
        ("keep", JOKIC, "2026-10-21", "2026-10-23"), ("block", KAWHI, "2026-10-21", "2026-10-21"),
        ("override", EDWARDS, "2026-10-21", "2026-10-21")]
    assert holds[2]["effect"].startswith("Count Anthony Edwards out")
    assert stored["summary"] == ("Keep Nikola Jokic Wed 10/21 to Fri 10/23; Block Kawhi Leonard Wed 10/21; "
                                 "Anthony Edwards out Wed 10/21. Takes effect when you apply it.")
    # The chip expires when its first day ends on ESPN
    assert stored["expires_at"] == mod.rollover_at(TOMORROW)
    # The model reads NBA ids only; the frontend's chip carries the action, and the API schema takes it
    assert out["action"]["holds"][0]["player"] == {"player_id": NBA_JOKIC, "name": "Nikola Jokic", "team": "DEN"}
    chip = out[proposals.CHIP_KEY]
    assert chip["steer"]["holds"][1]["player"]["espn_id"] == KAWHI and chip["lane"] is None
    assert AiProposalChip.model_validate(chip).steer.holds[0].kind == "keep"


@pytest.mark.unit
def test_a_check_in_steers_in_shadow(chips):
    run(mod.steer(11, 7, TEAM, LEAGUE, mod.SteerInput(team_id=21, reason="looks right", holds=[
        {"kind": "pin", "player_id": NBA_EDWARDS}]), NOW, shadow=True))
    assert chips.inserted[0]["state"] == "shadow"
    assert chips.inserted[0]["summary"] == f"Pin Anthony Edwards Wed 10/21. {proposals.SHADOW_WORDS}"


@pytest.mark.unit
def test_a_hold_that_cant_be_set_is_refused_and_nothing_is_stored(chips):
    cases = [
        ([{"kind": "keep", "player_id": NBA_KAWHI}], "not on the roster"),
        ([{"kind": "pin", "player_id": NBA_KAWHI}], "not on the roster"),
        ([{"kind": "block", "player_id": NBA_JOKIC}], "is on the roster"),
        ([{"kind": "keep", "player_id": NBA_JOKIC, "start": "2026-10-20"}], "today"),
        ([{"kind": "keep", "player_id": NBA_JOKIC, "until": "2026-11-30"}], "at most 28 days"),
        ([{"kind": "block", "player_id": 1}], "no ESPN id"),
        ([{"kind": "block", "player_id": 999}], "No NBA player"),
    ]
    for holds, words in cases:
        with pytest.raises(AppError, match=words):
            steer(holds=holds)
    with pytest.raises(AppError, match="not one of this team's live holds"):
        steer(clear=[99])
    assert chips.inserted == []


@pytest.mark.unit
def test_a_steer_chip_clears_live_holds_and_notes_what_it_overlaps(chips):
    chips.live = [live(), live(id=6, kind="block", player_id=KAWHI, nba_player_id=NBA_KAWHI, player_name="Kawhi Leonard",
                           player_team="LAC")]
    chips.pending = [SimpleNamespace(id=70, nba_date=TOMORROW, add_player_id=KAWHI, drop_player_id=JOKIC)]
    out = steer(holds=[{"kind": "keep", "player_id": NBA_JOKIC, "until": "2026-10-22"}], clear=[6])
    cleared = chips.inserted[0]["action"]["clear"]
    assert [(c["hold_id"], c["kind"], c["reason"]) for c in cleared] == [(6, "block", "Top-5 player")]
    assert chips.inserted[0]["summary"].startswith("Keep Nikola Jokic Wed 10/21 to Thu 10/22; clear block on Kawhi")
    notes = " ".join(out["notes"])
    assert "already has a keep hold" in notes and "hold 5" in notes
    assert "pickup_id 70" in notes and "cancel_pickup" in notes


@pytest.mark.unit
def test_a_team_carries_a_bounded_number_of_holds(chips):
    chips.live = [live(id=i, kind="block", player_id=1000 + i) for i in range(rules.MAX_LIVE)]
    with pytest.raises(AppError, match="at most"):
        steer(holds=[{"kind": "keep", "player_id": NBA_JOKIC}])
    # Clearing one makes room
    steer(holds=[{"kind": "keep", "player_id": NBA_JOKIC}], clear=[0])
    assert len(chips.inserted) == 1


# ---- set_lane and cancel_pickup -------------------------------------------------------------


def _lane(**kw):
    holder = SimpleNamespace(player_id=EDWARDS, nba_player_id=NBA_EDWARDS, name="Anthony Edwards", team="MIN")
    base = dict(id=3, status="active", holder=holder, end_date=date(2026, 10, 25), next_period=3, end_period=7)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.unit
def test_a_lane_start_is_planned_as_the_preview_plans_it(chips, monkeypatch):
    from schemas.stream_lane import LanePlayer, LaneProjection, LaneProjectionDay, StreamLanePlan
    from services.stream_lane_service import StreamLaneService
    asked = {}
    holder = LanePlayer(player_id=EDWARDS, name="Anthony Edwards", team="MIN", nba_player_id=NBA_EDWARDS)
    fa = LanePlayer(player_id=KAWHI, name="Kawhi Leonard", team="LAC", nba_player_id=NBA_KAWHI)
    projection = LaneProjection(as_of=NOW, value=60.0, hold_value=40.5, gain=19.5, adds=1, adds_left=3, rehold_after=2,
                                days=[LaneProjectionDay(scoring_period_id=3, nba_date=TOMORROW, player=fa, value=30.0,
                                                        switch=True)])

    async def plan(team, league_info, req):
        asked["player_id"] = req.player_id
        return SimpleNamespace(data=StreamLanePlan(holder=holder, start_period=3, end_period=7,
                                                   end_date=date(2026, 10, 25), first_decision_at=NOW,
                                                   projection=projection))
    monkeypatch.setattr(StreamLaneService, "plan", staticmethod(plan))
    out = run(mod.set_lane(11, 7, TEAM, LEAGUE, mod.SetLaneInput(team_id=21, action="start", player_id=NBA_EDWARDS,
                                                                 reason="Edwards is out through Sunday"), NOW))
    assert asked["player_id"] == EDWARDS
    stored = chips.inserted[0]
    assert stored["kind"] == "lane" and stored["action"]["op"] == "start" and stored["action"]["gain"] == 19.5
    assert stored["action"]["holder"]["espn_id"] == EDWARDS and stored["nba_date"] == TOMORROW
    assert stored["projection"] == {"change": 19.5, "scoring": "lane"}
    assert stored["summary"].startswith("Stream Anthony Edwards's spot through Sun 10/25: +19.5 projected")
    chip = AiProposalChip.model_validate(out[proposals.CHIP_KEY])
    assert chip.lane.op == "start" and chip.lane.days[0].player.espn_id == KAWHI


@pytest.mark.unit
def test_a_lane_is_paused_resumed_or_stopped_only_from_a_state_it_can_leave(chips, monkeypatch):
    from services.stream_lane_service import StreamLaneService
    lanes = SimpleNamespace(open=[_lane(), _lane(id=4, status="paused")])

    async def list_for_team(team):
        return SimpleNamespace(data=lanes)
    monkeypatch.setattr(StreamLaneService, "list_for_team", staticmethod(list_for_team))

    def lane(action, lane_id):
        return run(mod.set_lane(11, 7, TEAM, LEAGUE, mod.SetLaneInput(team_id=21, action=action, lane_id=lane_id,
                                                                      reason="the week is won"), NOW))
    lane("pause", 3)
    lane("resume", 4)
    lane("stop", 4)
    assert [(i["action"]["op"], i["action"]["lane_id"]) for i in chips.inserted] == \
        [("pause", 3), ("resume", 4), ("stop", 4)]
    assert chips.inserted[0]["summary"] == "Pause the lane streaming Anthony Edwards's spot. Done when you apply it."
    with pytest.raises(AppError, match="can't be paused"):
        lane("pause", 4)
    with pytest.raises(AppError, match="can't be resumed"):
        lane("resume", 3)
    with pytest.raises(AppError, match="not one of this team's open lanes"):
        lane("stop", 9)


@pytest.mark.unit
def test_a_pickup_is_cancelled_only_while_pending_and_before_its_deadline(chips, monkeypatch):
    deadline = datetime(2026, 10, 22, 23, 0, tzinfo=timezone.utc)   # tomorrow's first tip
    pk = SimpleNamespace(id=70, status="pending", nba_date=TOMORROW, scoring_period_id=3, add_player_id=KAWHI,
                         add_name="Kawhi Leonard", add_team="LAC", drop_player_id=EDWARDS, drop_name="Anthony Edwards",
                         drop_team="MIN", lane_id=3, deadline_at=deadline)
    found = {"row": pk}
    monkeypatch.setattr(mod, "_team_pickup", lambda team_id, pickup_id: found["row"])
    import services.providers.identity as identity
    monkeypatch.setattr(identity, "nba_ids_by_espn_id", lambda ids: {KAWHI: NBA_KAWHI, EDWARDS: NBA_EDWARDS})

    def cancel(now=NOW):
        return run(mod.cancel_pickup(11, 7, TEAM, mod.CancelPickupInput(team_id=21, pickup_id=70,
                                                                       reason="Kawhi is questionable"), now))
    out = cancel()
    stored = chips.inserted[0]
    assert stored["action"]["pickup_id"] == 70 and stored["action"]["lane_id"] == 3
    assert stored["expires_at"] == deadline   # the chip lasts as long as the pickup may still be made
    assert "a stream lane's" in stored["summary"]
    assert AiProposalChip.model_validate(out[proposals.CHIP_KEY]).pickup.drop.espn_id == EDWARDS
    # Deferred past the rollover into its day (a waiver period), it can still be cancelled before the tip.
    cancel(mod.rollover_at(TOMORROW) + timedelta(hours=2))
    with pytest.raises(AppError, match="deadline has passed"):
        cancel(deadline)
    # With no deadline (no game that day), until the day passes.
    found["row"] = SimpleNamespace(**{**vars(pk), "deadline_at": None})
    cancel()
    assert chips.inserted[-1]["expires_at"] == mod.rollover_at(date(2026, 10, 23))
    found["row"] = SimpleNamespace(**{**vars(pk), "status": "executed"})
    with pytest.raises(AppError, match="not a pending pickup"):
        cancel()


# ---- applying -------------------------------------------------------------------------------


@pytest.fixture
def applying(monkeypatch, on):
    h = SimpleNamespace(row=row(id=41, kind="steer", add_player_id=None, drop_player_id=None, action={
        "holds": [{"kind": "keep", "player": {"espn_id": JOKIC, "nba_player_id": NBA_JOKIC, "name": "Nikola Jokic",
                                              "team": "DEN"}, "share": None, "starts_on": "2026-10-21",
                   "ends_on": "2026-10-22", "effect": "Keep"}],
        "clear": [{"hold_id": 6, "kind": "block"}]}, reason="Jokic is the team"))

    async def get(proposal_id, user_id):
        if proposal_id != h.row.id or user_id != h.row.user_id:
            raise NotFoundError("AI_PROPOSAL_NOT_FOUND", "Proposal not found")
        return h.row
    monkeypatch.setattr(proposals, "get", get)
    monkeypatch.setattr(mod, "_now", lambda: NOW)
    return h


@pytest.mark.unit
def test_a_chip_is_applied_only_as_itself(applying, monkeypatch):
    assert run(mod.for_apply(41, 11, 21, "steer")) is applying.row
    with pytest.raises(NotFoundError):
        run(mod.for_apply(41, 12, 21, "steer"))                 # not the caller's
    with pytest.raises(NotFoundError):
        run(mod.for_apply(41, 11, 22, "steer"))                 # not this team's
    with pytest.raises(AppError, match="not what this chip proposes") as e:
        run(mod.for_apply(41, 11, 21, "lane"))
    assert e.value.status_code == 422
    applying.row.kind, applying.row.action = "lane", {"op": "stop", "lane_id": 3, "holder": {"espn_id": EDWARDS}}
    assert run(mod.for_apply(41, 11, 21, "lane", op="stop", lane_id=3)) is applying.row
    assert run(mod.for_apply(41, 11, 21, "lane", op="stop", **{"holder.espn_id": EDWARDS})) is applying.row
    with pytest.raises(AppError, match="not what this chip proposes"):
        run(mod.for_apply(41, 11, 21, "lane", op="stop", lane_id=4))
    with pytest.raises(AppError, match="not what this chip proposes"):
        run(mod.for_apply(41, 11, 21, "lane", op="pause", lane_id=3))
    for gone in ("sent", "dismissed", "shadow"):
        applying.row.state = gone
        with pytest.raises(ConflictError) as e:
            run(mod.for_apply(41, 11, 21, "lane", op="stop", lane_id=3))
        assert e.value.error_code == "AI_PROPOSAL_NOT_PENDING"


@pytest.mark.unit
def test_switched_off_no_chip_is_applied(applying, monkeypatch):
    monkeypatch.setattr(settings, "dime_steering_enabled", False)
    with pytest.raises(mod.SteeringOff) as e:
        run(mod.for_apply(41, 11, 21, "steer"))
    assert e.value.error_code == "AI_STEERING_OFF" and e.value.status_code == 409


@pytest.mark.unit
def test_applying_holds_sets_them_with_the_chip_s_reason_and_clears_its_clears(applying, monkeypatch):
    seen = {}

    async def apply(row_id, team_id, user_id, holds, clears, today, now, by):
        seen.update(row_id=row_id, team_id=team_id, user_id=user_id, holds=holds, clears=clears, today=today, by=by)
        return len(holds)
    monkeypatch.setattr(mod, "_apply_holds", apply)
    monkeypatch.setattr(mod.schedule_service, "get_nba_today", lambda: TODAY)
    assert run(mod.apply_holds(TEAM, 41)) == 1
    assert (seen["row_id"], seen["team_id"], seen["user_id"], seen["clears"]) == (41, 21, 11, [6])
    assert seen["holds"][0]["reason"] == "Jokic is the team" and seen["today"] == TODAY
    assert seen["by"] == "user"
    assert run(mod.apply_holds(TEAM, 41, by="dime")) == 1 and seen["by"] == "dime"   # Dime applying its own

    async def gone(*args):
        return None
    monkeypatch.setattr(mod, "_apply_holds", gone)
    with pytest.raises(ConflictError, match="already applied"):
        run(mod.apply_holds(TEAM, 41))


@pytest.mark.unit
def test_the_re_check_says_why_a_chip_can_no_longer_be_applied(monkeypatch, on):
    async def live_rows(team_id, today=None):
        return [live(id=5)]
    monkeypatch.setattr(rules, "live_rows", live_rows)
    steer_row = row(kind="steer", action={"holds": [], "clear": [
        {"hold_id": 5, "kind": "keep", "player": {"name": "Nikola Jokic"}},
        {"hold_id": 6, "kind": "block", "player": {"name": "Kawhi Leonard"}}]})
    assert run(mod.problems(steer_row, TEAM)) == ["The block on Kawhi Leonard has already ended."]

    async def direct(name, fn, *args, **kwargs):
        return fn(*args, **kwargs)
    monkeypatch.setattr(mod, "run_db", direct)
    monkeypatch.setattr(mod, "_team_pickup", lambda team_id, pid: SimpleNamespace(status="cancelled"))
    assert run(mod.problems(row(kind="cancel_pickup", action={"pickup_id": 70}), TEAM)) == \
        ["That pickup is no longer pending."]
    monkeypatch.setattr(settings, "dime_steering_enabled", False)
    assert run(mod.problems(steer_row, TEAM)) == [mod.SteeringOff.default_message + "."]


# ---- what reads a steering chip -------------------------------------------------------------


@pytest.mark.unit
def test_the_header_and_a_decision_read_a_steering_chip():
    r = row(kind="lane", add_player_id=None, drop_player_id=None, summary="Pause the lane streaming X's spot. Done "
                                                                           "when you apply it.",
            action={"op": "pause", "lane_id": 3, "holder": {"espn_id": EDWARDS, "nba_player_id": NBA_EDWARDS,
                                                             "name": "Anthony Edwards", "team": "MIN"}})
    move = decisions._move_of(r, NOW)
    assert move["kind"] == "lane" and move["action"]["holder"] == {"player_id": NBA_EDWARDS, "name": "Anthony Edwards",
                                                                    "team": "MIN"}
    assert "add" not in move
    view = proposals.view_of(r, NOW)
    assert "espn_id" not in str(view)


# ---- the tools in the agent -----------------------------------------------------------------


@pytest.mark.unit
def test_switched_off_the_agent_is_offered_no_steering_tool_and_calls_none():
    from services.ai import agent, tools
    assert not {t["name"] for t in agent.TOOLS} & mod.STEERING_TOOL_NAMES
    assert not (agent.ALLOWED | agent.CHECKIN_ALLOWED) & mod.STEERING_TOOL_NAMES
    out = run(tools.run_tool("steer", {"team_id": 21, "reason": "keep him", "holds": [{"kind": "keep", "player_id": 1}]},
                             ctx=tools.ToolContext(user_id=11, conversation_id=7), allowed=agent.ALLOWED))
    assert out.is_error and out.content == "Unknown tool: steer"


@pytest.mark.unit
def test_a_steering_chip_is_made_only_inside_a_conversation():
    from services.ai import tools
    out = run(tools.run_tool("cancel_pickup", {"team_id": 21, "pickup_id": 70, "reason": "he's questionable"},
                             ctx=tools.ToolContext(user_id=11)))
    assert out.is_error and "inside a conversation" in out.content
