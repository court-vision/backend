"""
services/ai/proposals.py: the agent's proposed moves (the CV AI plan, step 5), without
Postgres, ESPN or the model. The board is a prebuilt LineupState, the repository functions
are in-memory fakes, and the roster transaction's own checks are stubbed where a test
needs a refusal.

Covers the words and times (state, summary, expiry), the two shapes (the model's view with
NBA ids only, the frontend's chip with ESPN ids), the checks in the Week desk's order, a
proposal for today and for a later day, the re-check before staging, and the send contract
(for_send refuses a chip that isn't this move, mark_sent never raises).
"""

import asyncio
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest

from core.errors import AppError, ConflictError, NotFoundError
from schemas.common import FantasyProvider, LeagueInfo
from schemas.lineup_editor import LineupPlayer, LineupState
from services.ai import proposals as mod
from services.roster_transaction_service import RosterTransactionInvalid, RosterTransactionService

PG, UT, BE = 0, 11, 12
COUNTS = {"11": 1, "12": 1}
LEAGUE = LeagueInfo(provider=FantasyProvider.ESPN, league_id=552315826, team_name="GloatingSoap369", year=2027,
                    espn_s2="s2", swid="{SWID}", espn_team_id=1)
TEAM = SimpleNamespace(team_id=21, user_id=11, league_info_json="{}", league_id=None, league=None)
JOKIC, EDWARDS, KAWHI = 3112335, 4594268, 6450                 # ESPN ids
NBA_JOKIC, NBA_EDWARDS, NBA_KAWHI = 203999, 1630162, 202695    # NBA ids
TODAY = date(2026, 10, 21)                                      # Wednesday, matchup day 2 (ESPN day 2)
NOW = datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc)        # 4 PM EDT, before the first tip
WEEK = {"start_date": date(2026, 10, 19), "end_date": date(2026, 10, 25), "current_day_index": 2}


def player(pid, name, slot=UT, *, team="DEN", nba_id=None):
    return LineupPlayer(
        player_id=pid, name=name, team=team, lineup_slot_id=slot, lineup_slot=str(slot), nba_player_id=nba_id,
        eligible_slot_ids=[PG, UT, BE], eligible_slots=["PG", "UT", "BE"], lineup_locked=False,
        has_game_today=True, opponent="vs LAL", game_time_et="19:30", game_started=False, locked=False,
        playable=True, avg_points=40.0, value_kind="fpts", value_source="rolling",
    )


def state(players, *, can_write=True, reason=None, period=2, nba_date="2026-10-21", counts=COUNTS):
    return LineupState(
        provider=FantasyProvider.ESPN, team_name="GloatingSoap369", espn_team_id=1, nba_date=nba_date,
        scoring_period_id=period, scoring_period_source="provider", first_game_time_et="19:00",
        slot_counts=counts, slots=[], lock_type="INDIVIDUAL_GAME", players=players,
        can_write=can_write, write_blocked_reason=reason, roster_version="v1", fetched_at="now",
    )


def board():
    return [player(JOKIC, "Nikola Jokic", nba_id=NBA_JOKIC), player(EDWARDS, "Anthony Edwards", BE, team="MIN", nba_id=NBA_EDWARDS)]


def row(**kw):
    """A usr.ai_proposals row as peewee hands it back."""
    fields = dict(id=31, user_id=11, conversation_id=7, turn_id=None, team_id=21, kind="transaction", day=2,
                  scoring_period_id=2, nba_date=TODAY, add_player_id=KAWHI, add_nba_id=NBA_KAWHI,
                  add_name="Kawhi Leonard", add_team="LAC", drop_player_id=EDWARDS, drop_nba_id=NBA_EDWARDS,
                  drop_name="Anthony Edwards", drop_team="MIN", reason="4 games left", summary="Add Kawhi Leonard, drop Anthony Edwards, today. Goes to ESPN when you send it.",
                  projection={"scoring": "categories", "before": {}, "after": {}, "change": 1}, state="proposed",
                  moves=None, expires_at=datetime(2026, 10, 21, 23, 0, tzinfo=timezone.utc), created_at=NOW,
                  updated_at=NOW, sent_at=None, audit_id=None, pickup_id=None)
    fields.update(kw)
    return SimpleNamespace(**fields)


def run(coro):
    return asyncio.run(coro)


# ---- words and times ------------------------------------------------------------------------


@pytest.mark.unit
def test_a_proposed_row_past_its_expiry_reads_as_expired_and_nothing_else_moves():
    r = row()
    assert mod.state_of(r, NOW) == "proposed"
    assert mod.state_of(r, r.expires_at) == "expired"
    assert mod.state_of(row(expires_at=datetime(2026, 10, 21, 23, 0)), NOW + timedelta(days=1)) == "expired"  # naive = UTC
    assert mod.state_of(row(state="sent"), NOW + timedelta(days=9)) == "sent"
    assert mod.state_of(row(state="dismissed"), NOW + timedelta(days=9)) == "dismissed"


@pytest.mark.unit
def test_the_summary_says_the_move_the_day_and_what_sending_does():
    assert mod.summary_of("transaction", "Kawhi Leonard", "Anthony Edwards", TODAY) == \
        "Add Kawhi Leonard, drop Anthony Edwards, today. Goes to ESPN when you send it."
    assert mod.summary_of("transaction", "Kawhi Leonard", None, TODAY) == \
        "Add Kawhi Leonard into an open roster spot, today. Goes to ESPN when you send it."
    assert mod.summary_of("scheduled", None, "Anthony Edwards", date(2026, 10, 22)) == \
        "Drop Anthony Edwards, Thu 10/22. Scheduled: made for you ahead of Thursday's games."


@pytest.mark.unit
def test_a_chip_expires_at_the_day_s_first_tip_or_the_rollover_after_it():
    tip = mod.et_at(TODAY, time(19, 0))
    assert mod.expires_at_for("transaction", TODAY, time(19, 0), NOW) == tip
    # Past the tip (or a day with no games), the chip lasts until ESPN's next day begins
    assert mod.expires_at_for("transaction", TODAY, time(19, 0), tip) == mod.rollover_at(TODAY + timedelta(days=1))
    assert mod.expires_at_for("transaction", date(2026, 10, 23), None, NOW) == mod.rollover_at(date(2026, 10, 24))
    # A scheduled move lasts until its day begins on ESPN, when a pickup can no longer be scheduled for it
    assert mod.expires_at_for("scheduled", date(2026, 10, 23), time(19, 0), NOW) == mod.rollover_at(date(2026, 10, 23))


@pytest.mark.unit
def test_a_chip_made_between_2_and_3_am_et_is_born_live():
    # ESPN's day turns at 3 AM ET (ROLLOVER_ET, shared with schedule_service.get_nba_today). At 2:20 AM
    # ET on Friday, Thursday is still today and a Friday pickup still schedules, so neither chip may
    # expire before Friday begins. Under the pickup service's old 02:00 ET rollover, both were born
    # expired: the first chip asked for on production, a Friday pickup at 2:20 AM ET, arrived expired.
    late = datetime(2026, 10, 23, 6, 20, tzinfo=timezone.utc)          # 2:20 AM EDT, Friday Oct 23
    friday_begins = datetime(2026, 10, 23, 7, 0, tzinfo=timezone.utc)  # 3:00 AM EDT
    assert mod.rollover_at(date(2026, 10, 23)) == friday_begins
    assert mod.expires_at_for("scheduled", date(2026, 10, 23), time(19, 0), late) == friday_begins
    assert mod.expires_at_for("transaction", date(2026, 10, 22), time(19, 0), late) == friday_begins


@pytest.mark.unit
def test_the_input_needs_a_move_and_a_short_reason():
    ok = mod.ProposeMoveInput(team_id=21, add=NBA_KAWHI, reason="4 games left")
    assert (ok.drop, ok.day) == (None, None)
    with pytest.raises(ValueError, match="add, a drop, or both"):
        mod.ProposeMoveInput(team_id=21, reason="4 games left")
    with pytest.raises(ValueError, match="same player"):
        mod.ProposeMoveInput(team_id=21, add=1, drop=1, reason="4 games left")
    with pytest.raises(ValueError):
        mod.ProposeMoveInput(team_id=21, add=1, day=14, reason="4 games left")
    with pytest.raises(ValueError):
        mod.ProposeMoveInput(team_id=21, add=1, reason="x" * (mod.REASON_MAX + 1))
    with pytest.raises(ValueError):
        mod.ProposeMoveInput(team_id=21, add=1, reason="ok", extra=1)


# ---- shapes ---------------------------------------------------------------------------------


@pytest.mark.unit
def test_the_model_s_view_carries_nba_ids_only_and_the_chip_carries_espn_ids_too():
    r = row()
    view = mod.view_of(r, NOW)
    assert view["add"] == {"player_id": NBA_KAWHI, "name": "Kawhi Leonard", "team": "LAC"}
    assert view["drop"] == {"player_id": NBA_EDWARDS, "name": "Anthony Edwards", "team": "MIN"}
    assert (view["proposal_id"], view["state"], view["kind"], view["day"], view["date"]) == (31, "proposed", "transaction", 2, "2026-10-21")
    assert view["projection"] == r.projection and view["expires_at"] == "2026-10-21T23:00:00+00:00"
    assert "espn" not in str(view) and "team_id" not in view

    chip = mod.chip_of(r, NOW)
    assert chip["add"] == {"espn_id": KAWHI, "nba_player_id": NBA_KAWHI, "name": "Kawhi Leonard", "team": "LAC"}
    assert chip["drop"]["espn_id"] == EDWARDS
    assert (chip["team_id"], chip["scoring_period_id"], chip["dow"], chip["change"], chip["scoring"]) == (21, 2, "Wed", 1, "categories")
    assert chip["sent_at"] is None and chip["state"] == "proposed" and chip["summary"] == r.summary
    # No projection: the chip shows no change
    assert mod.chip_of(row(projection=None), NOW)["change"] is None
    # A drop-only row has no add
    assert mod.chip_of(row(add_player_id=None, add_nba_id=None, add_name=None, add_team=None), NOW)["add"] is None


@pytest.mark.unit
def test_a_stored_turn_s_chips_are_refreshed_from_the_rows_in_one_read(monkeypatch):
    reads = []

    async def of_conversation(conversation_id, user_id):
        reads.append((conversation_id, user_id))
        return [row(state="sent", sent_at=NOW), row(id=32, state="dismissed")]
    monkeypatch.setattr(mod, "_of_conversation", of_conversation)
    stale = {"type": "proposal", "id": "p1", **mod.chip_of(row(), NOW)}
    gone = {"type": "proposal", "id": "p2", **mod.chip_of(row(id=33), NOW)}       # a row that no longer exists
    history = [[{"kind": "text", "text": "Try this:"}, {"kind": "block", "block": stale}],
               [{"kind": "block", "block": gone}, {"kind": "block", "block": {"type": "table", "id": "t1"}}]]

    out = run(mod.refresh_history(history, 7, 11, NOW))

    assert reads == [(7, 11)]
    assert out[0][0] == {"kind": "text", "text": "Try this:"}
    assert out[0][1]["block"]["state"] == "sent" and out[0][1]["block"]["id"] == "p1" and out[0][1]["block"]["type"] == "proposal"
    assert out[0][1]["block"]["sent_at"] == NOW.isoformat()
    assert out[1] == history[1]
    # Turns without a chip never read the rows
    assert run(mod.refresh_history([[{"kind": "text", "text": "hi"}]], 7, 11, NOW)) == [[{"kind": "text", "text": "hi"}]]
    assert reads == [(7, 11)]


@pytest.mark.unit
def test_the_header_line_lists_what_became_of_the_conversation_s_proposals(monkeypatch):
    rows = []

    async def of_conversation(conversation_id, user_id):
        return rows
    monkeypatch.setattr(mod, "_of_conversation", of_conversation)

    assert run(mod.header_line(7, 11, NOW)) == ""
    rows[:] = [row(state="sent"), row(id=32, add_player_id=None, add_name=None, drop_name="Nikola Jokic", kind="scheduled",
                                         nba_date=date(2026, 10, 22), expires_at=NOW - timedelta(hours=1))]
    line = run(mod.header_line(7, 11, NOW))
    assert line == ("Your earlier proposals in this conversation: #31 add Kawhi Leonard / drop Anthony Edwards "
                    "(Wed 2026-10-21): sent; #32 drop Nikola Jokic (Thu 2026-10-22): expired.")
    rows[:] = [row(id=i) for i in range(1, mod.HEADER_MAX + 5)]
    assert run(mod.header_line(7, 11, NOW)).count("#") == mod.HEADER_MAX


# ---- the checks, in the Week desk's order ---------------------------------------------------


@pytest.fixture
def checks(monkeypatch):
    h = SimpleNamespace(validate_error=None, lane_holders=set(), scheduled_drop=None, pending_add=None, budget=None)

    async def validate(league_info, st, add, drop, for_future_day=False):
        h.validate_args = (add, drop, for_future_day)
        if h.validate_error:
            raise h.validate_error
        return None, None

    async def direct_run_db(name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(RosterTransactionService, "_validate", staticmethod(validate))
    monkeypatch.setattr(mod, "run_db", direct_run_db)
    monkeypatch.setattr(mod, "_lane_holders", lambda team_id: h.lane_holders)
    monkeypatch.setattr(mod, "_scheduled_drop", lambda team_id, pid: h.scheduled_drop)
    monkeypatch.setattr(mod, "_pending_pickup_of", lambda team_id, pid: h.pending_add)
    monkeypatch.setattr(mod, "budget_block", lambda acq, d: h.budget)
    return h


ADD = mod._Who(KAWHI, NBA_KAWHI, "Kawhi Leonard", "LAC")
DROP = mod._Who(EDWARDS, NBA_EDWARDS, "Anthony Edwards", "MIN")


@pytest.mark.unit
def test_a_clean_move_has_no_problems_and_a_scheduled_one_is_checked_for_a_future_day(checks):
    assert run(mod._problems(TEAM, LEAGUE, state(board()), "transaction", ADD, DROP, TODAY)) == []
    assert checks.validate_args == (KAWHI, EDWARDS, False)
    assert run(mod._problems(TEAM, LEAGUE, state(board()), "scheduled", ADD, None, date(2026, 10, 22))) == [
        "The roster is full: name a player to drop for Kawhi Leonard (project_matchup picks one by the rule when the move names none)."]
    assert checks.validate_args == (KAWHI, None, True)
    # An open seat makes an add alone fine
    assert run(mod._problems(TEAM, LEAGUE, state(board(), counts={"11": 1, "12": 2}), "scheduled", ADD, None, date(2026, 10, 22))) == []


@pytest.mark.unit
def test_a_scheduled_move_for_a_day_espn_has_reached_can_no_longer_be_scheduled(checks):
    begun = "Wed Oct 21 has begun on ESPN, so a pickup can no longer be scheduled for it; ask for the move again."
    open_seat = state(board(), counts={"11": 1, "12": 2})
    assert run(mod._problems(TEAM, LEAGUE, open_seat, "scheduled", ADD, None, TODAY)) == [begun]
    assert run(mod._problems(TEAM, LEAGUE, state(board(), counts={"11": 1, "12": 2}, nba_date="2026-10-22"), "scheduled", ADD, None, TODAY)) == [begun]
    # A move for today is a transaction, and today is its day
    assert run(mod._problems(TEAM, LEAGUE, open_seat, "transaction", ADD, None, TODAY)) == []


@pytest.mark.unit
def test_every_problem_is_listed_in_order(checks):
    checks.validate_error = RosterTransactionInvalid(message="Kawhi Leonard is on waivers until Thu", data={"reason": "on_waivers"})
    checks.lane_holders = {EDWARDS}
    checks.scheduled_drop = SimpleNamespace(add_name="Someone", nba_date=date(2026, 10, 23))
    checks.pending_add = SimpleNamespace(nba_date=date(2026, 10, 24))
    checks.budget = "Acquisition limit reached for the week (7 of 7)"

    problems = run(mod._problems(TEAM, LEAGUE, state(board(), can_write=False, reason="writes_disabled"), "transaction", ADD, DROP, TODAY))

    assert problems == [
        "Roster changes are blocked for this team (writes_disabled).",
        "Kawhi Leonard is on waivers until Thu",
        "Anthony Edwards holds the spot a stream lane is streaming; stop the lane to drop him.",
        "Anthony Edwards is already being dropped for Someone on Fri Oct 23.",
        "A pickup of Kawhi Leonard is already scheduled for Sat Oct 24.",
        "Acquisition limit reached for the week (7 of 7).",
    ]


# ---- propose_move ---------------------------------------------------------------------------


@pytest.fixture
def proposing(checks, monkeypatch):
    h = checks
    h.board = state(board())
    h.first_open = 2
    h.first_tip = time(19, 0)
    h.inserted = []
    h.projection = {"scoring": "categories", "before": {"you": 4}, "after": {"you": 5}, "change": 1}
    h.projection_error = None
    identities = {
        NBA_KAWHI: SimpleNamespace(nba_id=NBA_KAWHI, name="Kawhi Leonard", espn_id=KAWHI, team="LAC"),
        NBA_EDWARDS: SimpleNamespace(nba_id=NBA_EDWARDS, name="Anthony Edwards", espn_id=EDWARDS, team="MIN"),
        NBA_JOKIC: SimpleNamespace(nba_id=NBA_JOKIC, name="Nikola Jokic", espn_id=JOKIC, team="DEN"),
        1: SimpleNamespace(nba_id=1, name="No Espn", espn_id=None, team=None),
    }

    async def read_board(team, league_info):
        return h.board

    async def project(user_id, team, league_info, args, now=None):
        h.projected = (user_id, team.team_id, args.moves[0].add, args.moves[0].drop, args.moves[0].day)
        if h.projection_error:
            raise h.projection_error
        return h.projection

    async def insert(**fields):
        h.inserted.append(fields)
        return row(**{**fields, "id": 31})

    monkeypatch.setattr(mod, "_board", read_board)
    monkeypatch.setattr(mod.schedule_service, "get_nba_today", lambda: TODAY)
    monkeypatch.setattr(mod.schedule_service, "get_current_matchup", lambda today: dict(WEEK))
    monkeypatch.setattr(mod, "_first_open_day", lambda today, idx, now: h.first_open)
    monkeypatch.setattr(mod, "_identities", lambda ids: {i: identities[i] for i in ids if i in identities})
    monkeypatch.setattr(mod, "_first_tip", lambda d: h.first_tip)
    monkeypatch.setattr(mod, "project_matchup", project)
    monkeypatch.setattr(mod, "_insert", insert)
    return h


def propose(**kw):
    args = mod.ProposeMoveInput(team_id=21, reason="4 games left at 2.4 blocks", **kw)
    return run(mod.propose_move(11, 7, TEAM, LEAGUE, args, NOW))


@pytest.mark.unit
def test_a_move_for_today_is_a_transaction_stored_with_espn_ids_and_shown_two_ways(proposing):
    out = propose(add=NBA_KAWHI, drop=NBA_EDWARDS)

    f = proposing.inserted[0]
    assert (f["user_id"], f["conversation_id"], f["team_id"], f["kind"], f["day"], f["scoring_period_id"], f["nba_date"]) == \
        (11, 7, 21, "transaction", 2, 2, TODAY)
    assert (f["add_player_id"], f["add_nba_id"], f["add_name"], f["add_team"]) == (KAWHI, NBA_KAWHI, "Kawhi Leonard", "LAC")
    assert (f["drop_player_id"], f["drop_nba_id"], f["drop_name"], f["drop_team"]) == (EDWARDS, NBA_EDWARDS, "Anthony Edwards", "MIN")
    assert f["state"] == "proposed" and f["expires_at"] == mod.et_at(TODAY, time(19, 0)) and f["projection"] == proposing.projection
    assert f["summary"] == "Add Kawhi Leonard, drop Anthony Edwards, today. Goes to ESPN when you send it."
    assert proposing.projected == (11, 21, NBA_KAWHI, NBA_EDWARDS, 2)
    # The model's view, and the chip beside it for the turn loop to lift off
    assert out["proposal_id"] == 31 and out["add"] == {"player_id": NBA_KAWHI, "name": "Kawhi Leonard", "team": "LAC"}
    assert out["notes"] == [] and out["projection"] == proposing.projection
    assert out[mod.CHIP_KEY]["add"]["espn_id"] == KAWHI and out[mod.CHIP_KEY]["kind"] == "transaction"
    assert "espn" not in str({k: v for k, v in out.items() if k != mod.CHIP_KEY})


@pytest.mark.unit
def test_a_move_for_a_later_day_is_scheduled_for_that_espn_day(proposing):
    proposing.first_tip = None
    out = propose(add=NBA_KAWHI, drop=NBA_EDWARDS, day=4)

    f = proposing.inserted[0]
    assert (f["kind"], f["day"], f["scoring_period_id"], f["nba_date"]) == ("scheduled", 4, 4, date(2026, 10, 23))
    assert f["expires_at"] == mod.rollover_at(date(2026, 10, 23))      # when ESPN's Friday begins, scheduling for it closes
    assert f["summary"] == "Add Kawhi Leonard, drop Anthony Edwards, Fri 10/23. Scheduled: made for you ahead of Friday's games."
    assert out["kind"] == "scheduled" and out[mod.CHIP_KEY]["scoring_period_id"] == 4
    assert proposing.validate_args == (KAWHI, EDWARDS, True)


@pytest.mark.unit
def test_a_day_no_longer_open_moves_to_the_first_open_one_with_a_note(proposing):
    proposing.first_open = 3           # today's first tip has passed
    out = propose(add=NBA_KAWHI, drop=NBA_EDWARDS)
    assert out["day"] == 3 and out["kind"] == "scheduled" and out["notes"] == []
    out = propose(add=NBA_KAWHI, drop=NBA_EDWARDS, day=2)
    assert out["day"] == 3 and out["notes"] == ["Day 2 is no longer open; the move is proposed for day 3, the first a move made now counts for."]


@pytest.mark.unit
def test_refusals_come_back_as_readable_errors_and_store_nothing(proposing):
    with pytest.raises(AppError, match="after the matchup's last day"):
        propose(add=NBA_KAWHI, day=7)
    with pytest.raises(AppError, match="not on the roster"):
        propose(add=NBA_KAWHI, drop=NBA_KAWHI + 1) if False else propose(drop=NBA_KAWHI)
    with pytest.raises(AppError, match="No NBA player with id 999"):
        propose(add=999)
    with pytest.raises(AppError, match="no ESPN id"):
        propose(add=1, drop=NBA_EDWARDS)
    proposing.validate_error = RosterTransactionInvalid(message="Kawhi Leonard is on waivers", data={"reason": "on_waivers"})
    with pytest.raises(AppError) as err:
        propose(add=NBA_KAWHI, drop=NBA_EDWARDS)
    assert err.value.error_code == "AI_PROPOSAL_INVALID" and err.value.message == "Kawhi Leonard is on waivers"
    proposing.validate_error = None
    with pytest.raises(AppError, match="drop on its own can only be made today"):
        propose(drop=NBA_EDWARDS, day=4)
    proposing.first_open = 7
    with pytest.raises(AppError, match="no day left"):
        propose(add=NBA_KAWHI, drop=NBA_EDWARDS)
    assert proposing.inserted == []


@pytest.mark.unit
def test_a_failed_projection_is_a_note_not_a_refusal(proposing):
    proposing.projection_error = RuntimeError("no projection today")
    out = propose(add=NBA_KAWHI, drop=NBA_EDWARDS)
    assert out["projection"] is None and out[mod.CHIP_KEY]["change"] is None
    assert out["notes"] == ["The matchup projection for this move is unavailable; the chip shows no change."]
    assert proposing.inserted[0]["projection"] is None


# ---- the desk's reads and writes ------------------------------------------------------------


@pytest.fixture
def stored(monkeypatch):
    h = SimpleNamespace(rows={31: row()}, sent=[], dismissed=[])

    async def get(proposal_id, user_id):
        r = h.rows.get(proposal_id)
        return r if r is not None and r.user_id == user_id else None

    async def dismiss(proposal_id, user_id, now):
        r = await get(proposal_id, user_id)
        if r is not None and r.state == "proposed":
            r.state = "dismissed"
            h.dismissed.append(proposal_id)
        return r

    async def mark_sent(proposal_id, now, audit_id, pickup_id):
        r = h.rows.get(proposal_id)
        if r is None or r.state == "sent":
            return False
        r.state, r.sent_at, r.audit_id, r.pickup_id = "sent", now, audit_id, pickup_id
        h.sent.append(proposal_id)
        return True

    monkeypatch.setattr(mod, "_get", get)
    monkeypatch.setattr(mod, "_dismiss", dismiss)
    monkeypatch.setattr(mod, "_mark_sent", mark_sent)
    return h


@pytest.mark.unit
def test_for_send_hands_back_the_chip_s_own_move_and_refuses_anything_else(stored):
    r = run(mod.for_send(31, 11, 21, kind="transaction", add=KAWHI, drop=EDWARDS, now=NOW))
    assert r.id == 31
    with pytest.raises(NotFoundError):
        run(mod.for_send(31, 12, 21, kind="transaction", add=KAWHI, drop=EDWARDS, now=NOW))   # someone else's
    with pytest.raises(NotFoundError):
        run(mod.for_send(31, 11, 22, kind="transaction", add=KAWHI, drop=EDWARDS, now=NOW))   # another team's
    with pytest.raises(NotFoundError):
        run(mod.for_send(99, 11, 21, kind="transaction", add=KAWHI, drop=EDWARDS, now=NOW))
    for kw in ({"kind": "scheduled"}, {"add": JOKIC}, {"drop": None}, {"scoring_period_id": 3}):
        args = {"kind": "transaction", "add": KAWHI, "drop": EDWARDS, **kw}
        with pytest.raises(AppError) as err:
            run(mod.for_send(31, 11, 21, now=NOW, **args))
        assert (err.value.error_code, err.value.status_code) == ("AI_PROPOSAL_MISMATCH", 422)
    # The chip's own day is fine when the request names it
    assert run(mod.for_send(31, 11, 21, kind="transaction", add=KAWHI, drop=EDWARDS, scoring_period_id=2, now=NOW)).id == 31


@pytest.mark.unit
def test_for_send_refuses_a_chip_that_is_not_pending(stored):
    for state_, when in (("sent", NOW), ("dismissed", NOW), ("proposed", NOW + timedelta(days=1))):
        stored.rows[31] = row(state=state_)
        with pytest.raises(ConflictError) as err:
            run(mod.for_send(31, 11, 21, kind="transaction", add=KAWHI, drop=EDWARDS, now=when))
        assert err.value.error_code == "AI_PROPOSAL_NOT_PENDING"
        assert err.value.data == {"state": "expired" if state_ == "proposed" else state_}


@pytest.mark.unit
def test_mark_sent_records_the_write_and_never_raises(stored, monkeypatch):
    run(mod.mark_sent(31, audit_id=9, now=NOW))
    assert stored.sent == [31] and stored.rows[31].audit_id == 9 and stored.rows[31].sent_at == NOW
    run(mod.mark_sent(31, pickup_id=4, now=NOW))        # already sent: logged, nothing raised
    assert stored.rows[31].audit_id == 9 and stored.rows[31].pickup_id is None
    run(mod.mark_sent(99, pickup_id=4, now=NOW))
    # A dismiss that landed while the write was with ESPN is overtaken: the move was made
    stored.rows[31] = row(state="dismissed")
    run(mod.mark_sent(31, audit_id=10, now=NOW))
    assert stored.rows[31].state == "sent" and stored.rows[31].audit_id == 10

    async def broken(*args):
        raise RuntimeError("db")
    monkeypatch.setattr(mod, "_mark_sent", broken)
    run(mod.mark_sent(31, audit_id=9, now=NOW))


@pytest.mark.unit
def test_dismiss_lets_a_proposed_chip_go_and_refuses_a_sent_one(stored):
    chip = run(mod.dismiss(31, 11, NOW))
    assert chip["state"] == "dismissed" and stored.dismissed == [31]
    assert run(mod.dismiss(31, 11, NOW))["state"] == "dismissed"      # again: the same answer
    stored.rows[31] = row(state="sent", sent_at=NOW)
    with pytest.raises(ConflictError) as err:
        run(mod.dismiss(31, 11, NOW))
    assert err.value.error_code == "AI_PROPOSAL_SENT"
    with pytest.raises(NotFoundError):
        run(mod.dismiss(31, 12, NOW))


@pytest.mark.unit
def test_check_re_reads_the_board_for_a_proposed_chip_and_names_the_state_otherwise(stored, checks, monkeypatch):
    reads = []

    async def read_board(team, league_info):
        reads.append(team.team_id)
        return state(board())
    monkeypatch.setattr(mod, "_board", read_board)

    chip, problems = run(mod.check(31, 11, TEAM, LEAGUE, NOW))
    assert (chip["proposal_id"], problems, reads) == (31, [], [21])
    assert checks.validate_args == (KAWHI, EDWARDS, False)
    checks.lane_holders = {EDWARDS}
    assert run(mod.check(31, 11, TEAM, LEAGUE, NOW))[1] == ["Anthony Edwards holds the spot a stream lane is streaming; stop the lane to drop him."]

    stored.rows[31] = row(state="sent")
    chip, problems = run(mod.check(31, 11, TEAM, LEAGUE, NOW))
    assert (chip["state"], problems) == ("sent", ["This move was already sent."])
    stored.rows[31] = row()
    assert run(mod.check(31, 11, TEAM, LEAGUE, NOW + timedelta(days=1)))[1] == ["This chip expired: the day it was for has started."]
    assert reads == [21, 21]


# ---- lineup chips (get_lineup, propose_lineup) ----------------------------------------------


def lplayer(pid, name, slot, *, nba_id, team="DEN", game=True, locked=False, eligible=(PG, UT, BE), value=40.0,
            status=None, injured=False, value_kind="fpts"):
    return LineupPlayer(
        player_id=pid, name=name, team=team, lineup_slot_id=slot, lineup_slot=mod._slot_name(slot), nba_player_id=nba_id,
        eligible_slot_ids=list(eligible), eligible_slots=[mod._slot_name(s) for s in eligible], injured=injured,
        injury_status=status, lineup_locked=locked, has_game_today=game, opponent="vs LAL" if game else None,
        game_time_et="19:30" if game else None, game_started=False, locked=locked, playable=game and status != "OUT",
        avg_points=value, value_kind=value_kind, value_source="rolling",
    )


def lineup_board(*, jokic_plays=False, edwards_locked=False, value_kind="fpts"):
    """Jokic idle at UT, Edwards on the bench with a game: the one fill on the board is the swap."""
    return [lplayer(JOKIC, "Nikola Jokic", UT, nba_id=NBA_JOKIC, game=jokic_plays, value=55.0, value_kind=value_kind),
            lplayer(EDWARDS, "Anthony Edwards", BE, nba_id=NBA_EDWARDS, team="MIN", value=42.5, locked=edwards_locked,
                    value_kind=value_kind)]


SWAP_MOVES = [mod.Move(EDWARDS, BE, UT, "start", "vs LAL · 7:30 PM"), mod.Move(JOKIC, UT, BE, "bench", "no game today")]
SWAP_JSON = [
    {"espn_id": EDWARDS, "nba_player_id": NBA_EDWARDS, "name": "Anthony Edwards", "team": "MIN",
     "from_slot_id": BE, "from_slot": "BE", "to_slot_id": UT, "to_slot": "UT", "role": "start", "note": "vs LAL · 7:30 PM"},
    {"espn_id": JOKIC, "nba_player_id": NBA_JOKIC, "name": "Nikola Jokic", "team": "DEN",
     "from_slot_id": UT, "from_slot": "UT", "to_slot_id": BE, "to_slot": "BE", "role": "bench", "note": "no game today"},
]
SWAP_INPUT = [{"player_id": NBA_EDWARDS, "to_slot": "UT"}, {"player_id": NBA_JOKIC, "to_slot": "BE"}]


def lineup_row(**kw):
    """A usr.ai_proposals row of kind lineup: no add or drop, the swap as `moves`."""
    fields = dict(kind="lineup", add_player_id=None, add_nba_id=None, add_name=None, add_team=None, drop_player_id=None,
                  drop_nba_id=None, drop_name=None, drop_team=None, moves=SWAP_JSON,
                  reason="Edwards plays tonight; Jokic sits idle",
                  summary="Start Anthony Edwards, bench Nikola Jokic, today. Goes to ESPN when you send it.",
                  projection={"scoring": "points", "change": 42.5, "basis": "day"},
                  expires_at=mod.rollover_at(date(2026, 10, 22)))
    fields.update(kw)
    return row(**fields)


@pytest.mark.unit
def test_the_lineup_input_names_moves_or_fill_once_each_and_known_slots():
    ok = mod.ProposeLineupInput(team_id=21, moves=[{"player_id": NBA_EDWARDS, "to_slot": "ut"}], reason="plays tonight")
    assert (ok.moves[0].to_slot, ok.fill, ok.day) == ("UT", False, None)
    assert mod.ProposeLineupInput(team_id=21, fill=True, reason="set it").moves is None
    with pytest.raises(ValueError, match="name the moves, or set fill"):
        mod.ProposeLineupInput(team_id=21, reason="set it")
    with pytest.raises(ValueError, match="name the moves, or set fill"):
        mod.ProposeLineupInput(team_id=21, fill=True, moves=[{"player_id": 1, "to_slot": "BE"}], reason="set it")
    with pytest.raises(ValueError, match="at most one move"):
        mod.ProposeLineupInput(team_id=21, moves=[{"player_id": 1, "to_slot": "BE"}, {"player_id": 1, "to_slot": "UT"}], reason="set it")
    with pytest.raises(ValueError, match="unknown slot 'QB'"):
        mod.ProposeLineupInput(team_id=21, moves=[{"player_id": 1, "to_slot": "QB"}], reason="set it")
    with pytest.raises(ValueError):
        mod.ProposeLineupInput(team_id=21, moves=[{"player_id": i, "to_slot": "BE"} for i in range(1, mod.MOVES_MAX + 2)], reason="set it")
    with pytest.raises(ValueError):
        mod.GetLineupInput(team_id=21, day=14)
    assert mod.SLOT_IDS == {"PG": 0, "SG": 1, "SF": 2, "PF": 3, "C": 4, "G": 5, "F": 6, "SG/SF": 7, "G/F": 8,
                            "PF/C": 9, "F/C": 10, "UT": 11, "BE": 12, "IR": 13}


@pytest.mark.unit
def test_lineup_roles_words_summary_and_expiry():
    assert (mod.role_of(BE, UT), mod.role_of(UT, BE), mod.role_of(PG, UT), mod.role_of(UT, 13), mod.role_of(13, BE)) == \
        ("start", "bench", "shift", "shift", "bench")
    moves = [*SWAP_JSON, {"name": "Jalen Green", "role": "shift", "to_slot": "IR"}]
    assert mod.lineup_words(moves) == "start Anthony Edwards, bench Nikola Jokic, Jalen Green to IR"
    assert mod.lineup_summary(moves, TODAY, True) == \
        "Start Anthony Edwards, bench Nikola Jokic, Jalen Green to IR, today. Goes to ESPN when you send it."
    assert mod.lineup_summary(SWAP_JSON[:1], date(2026, 10, 23), False) == \
        "Start Anthony Edwards, Fri 10/23. Sets Friday's lineup on ESPN when you send it."
    # A lineup is edited until its day turns; the day's tip is no end for it
    assert mod.expires_at_for("lineup", TODAY, time(19, 0), NOW) == mod.rollover_at(date(2026, 10, 22))
    assert mod.expires_at_for("lineup", date(2026, 10, 23), None, NOW) == mod.rollover_at(date(2026, 10, 24))


@pytest.mark.unit
def test_the_day_s_change_counts_playable_players_entering_and_leaving_active_slots():
    # Jokic has no game: benching him costs nothing, and Edwards' night counts in full
    assert mod.lineup_change(state(lineup_board()), SWAP_MOVES) == {"scoring": "points", "change": 42.5, "basis": "day"}
    # A starter with a game who sits costs his points; a shift between active slots changes nothing
    both = state(lineup_board(jokic_plays=True))
    assert mod.lineup_change(both, SWAP_MOVES)["change"] == -12.5
    assert mod.lineup_change(both, [mod.Move(JOKIC, UT, PG, "shift")])["change"] == 0.0
    # A category board's value is a rank score, not a total: the chip shows no change
    assert mod.lineup_change(state(lineup_board(value_kind="cat_value")), SWAP_MOVES) is None
    assert mod.lineup_change(state(lineup_board()), []) is None


@pytest.mark.unit
def test_a_lineup_row_is_shown_two_ways_and_an_add_drop_row_carries_no_moves():
    r = lineup_row()
    view = mod.view_of(r, NOW)
    assert (view["kind"], view["add"], view["drop"], view["summary"]) == ("lineup", None, None, r.summary)
    assert view["moves"] == [
        {"player_id": NBA_EDWARDS, "name": "Anthony Edwards", "team": "MIN", "from_slot": "BE", "to_slot": "UT",
         "role": "start", "note": "vs LAL · 7:30 PM"},
        {"player_id": NBA_JOKIC, "name": "Nikola Jokic", "team": "DEN", "from_slot": "UT", "to_slot": "BE",
         "role": "bench", "note": "no game today"},
    ]
    assert "espn" not in str(view)
    chip = mod.chip_of(r, NOW)
    assert chip["moves"] == SWAP_JSON and chip["add"] is None and chip["drop"] is None
    assert (chip["kind"], chip["change"], chip["scoring"], chip["scoring_period_id"]) == ("lineup", 42.5, "points", 2)
    assert mod.chip_of(row(), NOW)["moves"] == [] and mod.view_of(row(), NOW)["moves"] == []
    assert mod.row_moves(r) == SWAP_MOVES and mod.row_moves(row()) == []


@pytest.mark.unit
def test_the_header_line_says_a_lineup_chip_s_moves(monkeypatch):
    async def of_conversation(conversation_id, user_id):
        return [lineup_row(id=40, state="sent")]
    monkeypatch.setattr(mod, "_of_conversation", of_conversation)
    assert run(mod.header_line(7, 11, NOW)) == ("Your earlier proposals in this conversation: #40 lineup: start Anthony "
                                                "Edwards, bench Nikola Jokic (Wed 2026-10-21): sent.")


@pytest.fixture
def boards(monkeypatch):
    """Today's board and any later day's, read by ESPN scoring period; the insert is recorded."""
    h = SimpleNamespace(today=state(lineup_board()), later={}, reads=[], inserted=[])
    blank = dict(add_player_id=None, add_nba_id=None, add_name=None, add_team=None,
                 drop_player_id=None, drop_nba_id=None, drop_name=None, drop_team=None)

    async def read_board(team, league_info, scoring_period_id=None):
        h.reads.append(scoring_period_id)
        return h.today if scoring_period_id is None else h.later[scoring_period_id]

    async def insert(**fields):
        h.inserted.append(fields)
        return row(**{**blank, **fields, "id": 40})

    monkeypatch.setattr(mod, "_board", read_board)
    monkeypatch.setattr(mod.schedule_service, "get_nba_today", lambda: TODAY)
    monkeypatch.setattr(mod.schedule_service, "get_current_matchup", lambda today: dict(WEEK))
    monkeypatch.setattr(mod, "_insert", insert)
    return h


@pytest.mark.unit
def test_get_lineup_lists_the_board_with_eligibility_game_lock_and_the_fill(boards):
    boards.today = state([*lineup_board(), lplayer(KAWHI, "Kawhi Leonard", BE, nba_id=NBA_KAWHI, team="LAC", status="OUT",
                                                   injured=True, eligible=(PG, UT, BE, 13), value=30.0)])
    out = run(mod.get_lineup(TEAM, LEAGUE, mod.GetLineupInput(team_id=21)))

    assert (out["team_id"], out["day"], out["date"], out["today"], out["scoring_period_id"], out["can_write"]) == \
        (21, 2, "2026-10-21", True, 2, True)
    assert out["slots"] == [{"slot": "UT", "count": 1}, {"slot": "BE", "count": 1}]
    jokic, edwards, kawhi = out["players"]
    assert jokic == {"player_id": NBA_JOKIC, "name": "Nikola Jokic", "team": "DEN", "slot": "UT",
                     "eligible_slots": ["PG", "UT", "BE"], "game": None, "locked": False, "injury": None,
                     "avg_value": 55.0, "value_kind": "fpts"}
    assert (edwards["slot"], edwards["game"], edwards["eligible_slots"]) == ("BE", "vs LAL · 7:30 PM", ["PG", "UT", "BE"])
    # IR is listed only for a player ESPN would let on it
    assert (kawhi["injury"], kawhi["eligible_slots"]) == ("OUT", ["PG", "UT", "BE", "IR"])
    assert out["fill"]["moves"] == [
        {"player_id": NBA_EDWARDS, "name": "Anthony Edwards", "from_slot": "BE", "to_slot": "UT", "role": "start", "note": "vs LAL · 7:30 PM"},
        {"player_id": NBA_JOKIC, "name": "Nikola Jokic", "from_slot": "UT", "to_slot": "BE", "role": "bench", "note": "no game today"},
    ]
    assert out["fill"]["summary"] == "2 move(s): start Anthony Edwards, bench Nikola Jokic (no game today)"
    assert out["notes"] == [] and "espn" not in str(out) and boards.reads == [None]


@pytest.mark.unit
def test_get_lineup_for_a_later_day_reads_that_day_s_board(boards):
    boards.later[4] = state(lineup_board(edwards_locked=True), period=4, nba_date="2026-10-23")
    out = run(mod.get_lineup(TEAM, LEAGUE, mod.GetLineupInput(team_id=21, day=4)))
    assert (out["day"], out["date"], out["today"], out["scoring_period_id"]) == (4, "2026-10-23", False, 4)
    assert out["players"][1]["locked"] is True and out["fill"]["moves"] == []
    assert boards.reads == [None, 4]
    with pytest.raises(AppError, match="after the matchup's last day"):
        run(mod.get_lineup(TEAM, LEAGUE, mod.GetLineupInput(team_id=21, day=7)))
    with pytest.raises(AppError, match="Day 1 has passed"):
        run(mod.get_lineup(TEAM, LEAGUE, mod.GetLineupInput(team_id=21, day=1)))


def propose_lineup(**kw):
    args = mod.ProposeLineupInput(team_id=21, reason="Edwards plays tonight; Jokic sits idle", **kw)
    return run(mod.propose_lineup(11, 7, TEAM, LEAGUE, args, NOW))


@pytest.mark.unit
def test_propose_lineup_resolves_the_moves_against_the_board_and_stores_one_chip(boards):
    out = propose_lineup(moves=SWAP_INPUT)

    f = boards.inserted[0]
    assert (f["user_id"], f["conversation_id"], f["team_id"], f["kind"], f["day"], f["scoring_period_id"], f["nba_date"]) == \
        (11, 7, 21, "lineup", 2, 2, TODAY)
    assert f["moves"] == SWAP_JSON and "add_player_id" not in f
    assert f["summary"] == "Start Anthony Edwards, bench Nikola Jokic, today. Goes to ESPN when you send it."
    assert f["projection"] == {"scoring": "points", "change": 42.5, "basis": "day"}
    assert f["expires_at"] == mod.rollover_at(date(2026, 10, 22)) and f["state"] == "proposed"
    assert f["reason"] == "Edwards plays tonight; Jokic sits idle"
    # The model's view (NBA ids, slot names), and the chip beside it for the turn loop to lift off
    assert out["proposal_id"] == 40 and out["kind"] == "lineup" and out["notes"] == []
    assert out["moves"][0] == {"player_id": NBA_EDWARDS, "name": "Anthony Edwards", "team": "MIN", "from_slot": "BE",
                               "to_slot": "UT", "role": "start", "note": "vs LAL · 7:30 PM"}
    assert out["projection"]["change"] == 42.5
    assert "espn" not in str({k: v for k, v in out.items() if k != mod.CHIP_KEY})
    chip = out[mod.CHIP_KEY]
    assert (chip["kind"], chip["moves"], chip["add"], chip["drop"], chip["change"]) == ("lineup", SWAP_JSON, None, None, 42.5)
    assert boards.reads == [None]


@pytest.mark.unit
def test_propose_lineup_with_fill_proposes_the_server_s_plan_and_refuses_a_set_lineup(boards):
    out = propose_lineup(fill=True)
    assert boards.inserted[0]["moves"] == SWAP_JSON and out[mod.CHIP_KEY]["moves"] == SWAP_JSON
    # Jokic plays too: Edwards can't displace an equal, so there is nothing to fill
    boards.today = state(lineup_board(jokic_plays=True))
    with pytest.raises(AppError, match="Nothing to fill: .* \\(Wed Oct 21\\)"):
        propose_lineup(fill=True)
    assert len(boards.inserted) == 1


@pytest.mark.unit
def test_propose_lineup_for_a_later_day_is_for_that_day_s_board(boards):
    boards.later[4] = state(lineup_board(), period=4, nba_date="2026-10-23")
    out = propose_lineup(day=4, fill=True)
    f = boards.inserted[0]
    assert (f["day"], f["scoring_period_id"], f["nba_date"]) == (4, 4, date(2026, 10, 23))
    assert f["summary"] == "Start Anthony Edwards, bench Nikola Jokic, Fri 10/23. Sets Friday's lineup on ESPN when you send it."
    assert f["expires_at"] == mod.rollover_at(date(2026, 10, 24))
    assert out[mod.CHIP_KEY]["scoring_period_id"] == 4 and out["date"] == "2026-10-23"
    assert boards.reads == [None, 4]


@pytest.mark.unit
def test_propose_lineup_refuses_what_the_week_desk_would_and_stores_nothing(boards):
    with pytest.raises(AppError, match="No player with id 999 is on this roster"):
        propose_lineup(moves=[{"player_id": 999, "to_slot": "UT"}])
    with pytest.raises(AppError, match="Anthony Edwards isn't eligible at C"):
        propose_lineup(moves=[{"player_id": NBA_EDWARDS, "to_slot": "C"}])
    with pytest.raises(AppError, match="Anthony Edwards is already in that slot"):
        propose_lineup(moves=[{"player_id": NBA_EDWARDS, "to_slot": "BE"}])
    with pytest.raises(AppError, match="can't go on IR"):
        propose_lineup(moves=[{"player_id": NBA_EDWARDS, "to_slot": "IR"}])
    # Starting him without benching Jokic overfills the one UT slot: the Week desk's capacity check
    with pytest.raises(AppError, match=r"Slot 11 would hold 2 players \(limit 1\)"):
        propose_lineup(moves=[{"player_id": NBA_EDWARDS, "to_slot": "UT"}])
    boards.today = state(lineup_board(edwards_locked=True))
    with pytest.raises(AppError, match="Anthony Edwards is locked"):
        propose_lineup(moves=SWAP_INPUT)
    boards.today = state(lineup_board(), can_write=False, reason="no_credentials")
    with pytest.raises(AppError, match=r"blocked for this team \(no_credentials\)"):
        propose_lineup(moves=SWAP_INPUT)
    with pytest.raises(AppError, match="Day 1 has passed"):
        propose_lineup(day=1, moves=SWAP_INPUT)
    for err in (
        pytest.raises(AppError, match="after the matchup's last day"),
    ):
        with err:
            propose_lineup(day=7, fill=True)
    assert boards.inserted == []


@pytest.mark.unit
def test_for_send_matches_a_lineup_chip_s_moves_and_day(stored):
    stored.rows[40] = lineup_row(id=40)
    swap = [(EDWARDS, UT), (JOKIC, BE)]
    assert run(mod.for_send(40, 11, 21, kind="lineup", scoring_period_id=2, moves=swap, now=NOW)).id == 40
    # The order is the client's, and a move the board already shows is left out (where a player
    # starts from is the board's, not the chip's: the write checks that)
    assert run(mod.for_send(40, 11, 21, kind="lineup", scoring_period_id=2, moves=list(reversed(swap)), now=NOW)).id == 40
    assert run(mod.for_send(40, 11, 21, kind="lineup", scoring_period_id=2, moves=swap[1:], now=NOW)).id == 40
    # Nothing at all, another slot, a player the chip doesn't move, another day, another kind
    for kw in ({"moves": []}, {"moves": [(EDWARDS, PG), (JOKIC, BE)]}, {"moves": [*swap, (KAWHI, BE)]},
               {"scoring_period_id": 3}, {"kind": "transaction"}):
        with pytest.raises(AppError) as err:
            run(mod.for_send(40, 11, 21, now=NOW, **{"kind": "lineup", "scoring_period_id": 2, "moves": swap, **kw}))
        assert (err.value.error_code, err.value.status_code) == ("AI_PROPOSAL_MISMATCH", 422)
    # An add / drop chip can't be sent as a lineup
    with pytest.raises(AppError, match="not the one this chip proposes"):
        run(mod.for_send(31, 11, 21, kind="lineup", scoring_period_id=2, moves=swap, now=NOW))
    stored.rows[40] = lineup_row(id=40, expires_at=NOW)
    with pytest.raises(ConflictError) as err:
        run(mod.for_send(40, 11, 21, kind="lineup", scoring_period_id=2, moves=swap, now=NOW))
    assert err.value.data == {"state": "expired"}


@pytest.mark.unit
def test_check_reads_the_chip_s_own_day_s_board_for_a_lineup_chip(stored, monkeypatch):
    h = SimpleNamespace(today=state(lineup_board()), friday=state(lineup_board(), period=4, nba_date="2026-10-23"), reads=[])

    async def read_board(team, league_info, scoring_period_id=None):
        h.reads.append(scoring_period_id)
        return h.friday if scoring_period_id == 4 else h.today
    monkeypatch.setattr(mod, "_board", read_board)

    stored.rows[40] = lineup_row(id=40)
    chip, problems = run(mod.check(40, 11, TEAM, LEAGUE, NOW))
    assert (chip["kind"], chip["moves"], problems, h.reads) == ("lineup", SWAP_JSON, [], [None])
    # A later day's chip is checked against that day's board
    stored.rows[41] = lineup_row(id=41, day=4, scoring_period_id=4, nba_date=date(2026, 10, 23),
                                 expires_at=mod.rollover_at(date(2026, 10, 24)))
    assert run(mod.check(41, 11, TEAM, LEAGUE, NOW))[1] == [] and h.reads == [None, None, 4]
    # The board moved on since the chip was made (an earlier day's write carried forward, or an edit on
    # ESPN): the chip's moves are checked from where each player is now, as the Week desk sends its own
    h.today = state([lplayer(JOKIC, "Nikola Jokic", UT, nba_id=NBA_JOKIC, game=False),
                     lplayer(EDWARDS, "Anthony Edwards", PG, nba_id=NBA_EDWARDS, team="MIN")], counts={"0": 1, "11": 1, "12": 1})
    assert run(mod.check(40, 11, TEAM, LEAGUE, NOW))[1] == []
    assert [(m.player_id, m.from_slot_id, m.to_slot_id, m.role) for m in mod.live_moves(h.today, stored.rows[40])] == \
        [(EDWARDS, PG, UT, "shift"), (JOKIC, UT, BE, "bench")]
    # Half of it is done already: only the rest is checked; all of it: nothing is left to send
    h.today = state([lplayer(JOKIC, "Nikola Jokic", UT, nba_id=NBA_JOKIC, game=False),
                     lplayer(EDWARDS, "Anthony Edwards", UT, nba_id=NBA_EDWARDS, team="MIN")], counts={"0": 1, "11": 2, "12": 1})
    assert [(m.player_id, m.from_slot_id, m.to_slot_id) for m in mod.live_moves(h.today, stored.rows[40])] == [(JOKIC, UT, BE)]
    assert run(mod.check(40, 11, TEAM, LEAGUE, NOW))[1] == []
    h.today = state([lplayer(JOKIC, "Nikola Jokic", BE, nba_id=NBA_JOKIC, game=False),
                     lplayer(EDWARDS, "Anthony Edwards", UT, nba_id=NBA_EDWARDS, team="MIN")])
    assert run(mod.check(40, 11, TEAM, LEAGUE, NOW))[1] == ["This lineup is already set on ESPN."]
    # A player who left the roster is named, as the Week desk's own check would
    h.today = state([lplayer(JOKIC, "Nikola Jokic", UT, nba_id=NBA_JOKIC, game=False)])
    assert run(mod.check(40, 11, TEAM, LEAGUE, NOW))[1] == ["Player is not on this roster"]
    h.today = state(lineup_board(edwards_locked=True), can_write=False, reason="writes_disabled")
    assert run(mod.check(40, 11, TEAM, LEAGUE, NOW))[1] == ["Roster changes are blocked for this team (writes_disabled).",
                                                            "Anthony Edwards is locked (game started)"]
    stored.rows[40] = lineup_row(id=40, state="sent")
    assert run(mod.check(40, 11, TEAM, LEAGUE, NOW))[1] == ["This move was already sent."]
