"""
services.scheduled_pickup_service without ESPN, the writer or Postgres: boards come
from a dict keyed by the day asked for (None = today), the pool from a dict, both
writer calls from queues, and every repository function is an in-memory fake —
the roster-transaction harness, plus the day-D board and the lineup writer the seat
step needs.

Covers scheduling (gates, bounds, locks and waivers ignored, the window, duplicates),
the executor's branches (executed on D-1 as FUTURE_ROSTER and on D as ROSTER, the
seat step, every deferral and every settled outcome), and the run wrapper (claim,
writes-off release, a broken row).
"""

import asyncio
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest

from core.errors import BadRequestError
from schemas.common import FantasyProvider, LeagueInfo
from schemas.lineup_editor import LineupPlayer, LineupState
from schemas.scheduled_pickup import PickupExecuteReq, SchedulePickupReq
from services import fantasy_writer_client
from services import lineup_editor_service as editor
from services import roster_transaction_service as txn
from services import scheduled_pickup_service as svc
from services.espn_service import PoolEntry
from services.fantasy_writer_client import FantasyWriterAuthRejected, FantasyWriterRejected, FantasyWriterUnavailable, WriterResult
from services.lineup_read_service import LineupReadService

PG, UT, BE, IR = 0, 11, 12, 13
COUNTS = {"0": 1, "11": 1, "12": 2, "13": 1}          # 4 non-IR seats
LEAGUE = LeagueInfo(provider=FantasyProvider.ESPN, league_id=552315826, team_name="GloatingSoap369", year=2027,
                    espn_s2="s2", swid="{SWID}", espn_team_id=1)
TEAM = SimpleNamespace(team_id=21, user_id=11, league_info_json="{}", league_id=None, league=None)
JOKIC, EDWARDS, KAWHI, EMBIID = 3112335, 4594268, 6450, 3059318
TODAY, DAY_D = date(2026, 10, 21), date(2026, 10, 22)       # ESPN days 2 and 3 (opening night = day 1)
NOW = datetime(2026, 10, 21, 23, 5, tzinfo=timezone.utc)    # 7:05 PM EDT Wed, after the first tip
DEADLINE = svc.et_at(DAY_D, time(19, 30))
ROLLOVER = svc.rollover_at(DAY_D)


def player(pid, name, slot=UT, *, team="DEN", game=False, started=False, locked=False, eligible=(PG, UT, BE)):
    return LineupPlayer(
        player_id=pid, name=name, team=team, lineup_slot_id=slot, lineup_slot=str(slot),
        eligible_slot_ids=list(eligible), eligible_slots=[str(s) for s in eligible], lineup_locked=locked,
        has_game_today=game, opponent="vs LAL" if game else None, game_time_et="19:30" if game else None,
        game_started=started, locked=locked or started, playable=game, avg_points=30.0, value_kind="fpts",
    )


def state(players, *, period=2, current=2, final=167, nba_date="2026-10-21", can_write=True, reason=None,
          version="v1"):
    return LineupState(
        provider=FantasyProvider.ESPN, team_name="GloatingSoap369", espn_team_id=1, nba_date=nba_date,
        scoring_period_id=period, scoring_period_source="provider", current_scoring_period_id=current,
        final_scoring_period_id=final, first_game_time_et="19:00", slot_counts=COUNTS, slots=[],
        lock_type="INDIVIDUAL_GAME", players=players, can_write=can_write, write_blocked_reason=reason,
        roster_version=version, fetched_at="now",
    )


def pool(pid=KAWHI, status="FREEAGENT", on_team=0, locked=False, name="Kawhi Leonard", team="LAC"):
    return PoolEntry(pid, status, on_team, locked, name, team, None)


def row(**kw):
    base = dict(id=1, team_id=21, user_id=11, add_player_id=KAWHI, drop_player_id=EDWARDS, add_name="Kawhi Leonard",
                add_team="LAC", drop_name="Anthony Edwards", drop_team="MIN", scoring_period_id=3, nba_date=DAY_D,
                not_before_at=NOW - timedelta(minutes=5), deadline_at=DEADLINE, next_attempt_at=NOW + svc.LEASE,
                attempts=1, status="pending", reason=None, detail=None, audit_id=None, lineup_audit_id=None,
                seated_slot_id=None, created_at=NOW - timedelta(days=1), updated_at=NOW, executed_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def h(monkeypatch):
    h = SimpleNamespace(boards={}, after_txn={}, after_lineup={}, reads=[], pool={}, pool_calls=[],
                        txn=[], txn_calls=[], lineup=[], lineup_calls=[], audits=[], updates=[],
                        settled=[], deferred=[], team=(LEAGUE, dict(COUNTS)), claimed=[], released=[])

    async def fake_read(team_id, league_info, *, fallback_slot_counts=None, now=None, scoring_period_id=None):
        h.reads.append(scoring_period_id)
        board = h.boards.get(scoring_period_id)
        if board is None:
            raise AssertionError(f"no board for scoring_period_id={scoring_period_id}")
        if isinstance(board, Exception):
            raise board
        return board

    async def fake_pool(league_info, player_ids, *, scoring_period_id=None):
        h.pool_calls.append((list(player_ids), scoring_period_id))
        return {pid: e for pid, e in h.pool.items() if pid in player_ids}

    async def fake_txn(payload):
        h.txn_calls.append(payload)
        nxt = h.txn.pop(0) if h.txn else WriterResult(True, 200, 200, "{}")
        if isinstance(nxt, Exception):
            raise nxt
        h.boards.update(h.after_txn)
        return nxt

    async def fake_lineup(payload):
        h.lineup_calls.append(payload)
        nxt = h.lineup.pop(0) if h.lineup else WriterResult(True, 200, 200, "{}")
        if isinstance(nxt, Exception):
            raise nxt
        h.boards.update(h.after_lineup)
        return nxt

    async def direct_run_db(name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def audit_insert(user_id, team_id, nba_date, period, source, moves, key, kind="lineup"):
        h.audits.append({"team_id": team_id, "nba_date": nba_date, "period": period, "source": source,
                         "moves": moves, "key": key, "kind": kind})
        return len(h.audits)

    def audit_update(audit_id, status, *, provider_status=None, error=None):
        h.updates.append((audit_id, status, provider_status, error))

    def settle_row(pickup_id, now, **fields):
        h.settled.append((pickup_id, fields))

    def defer_row(pickup_id, now, when, reason, detail, audit_id=None):
        h.deferred.append((pickup_id, when, reason, detail))

    monkeypatch.setattr(LineupReadService, "read", staticmethod(fake_read))
    monkeypatch.setattr(txn.EspnService, "get_player_pool_entries", staticmethod(fake_pool))
    monkeypatch.setattr(fantasy_writer_client, "apply_transaction", fake_txn)
    monkeypatch.setattr(fantasy_writer_client, "apply_lineup", fake_lineup)
    for module in (svc, txn, editor):
        monkeypatch.setattr(module, "run_db", direct_run_db)
    for module in (txn, editor):
        monkeypatch.setattr(module, "_audit_insert", audit_insert)
        monkeypatch.setattr(module, "_audit_update", audit_update)
    monkeypatch.setattr(svc, "_load_team_for_job", lambda team_id, user_id: h.team)
    monkeypatch.setattr(svc, "_settle_row", settle_row)
    monkeypatch.setattr(svc, "_defer_row", defer_row)
    monkeypatch.setattr(svc.settings, "roster_writes_enabled", True)
    return h


def arrange_day_before(h, *, drop_game=False, drop_started=False):
    """Wednesday evening (ESPN day 2), pickup for Thursday (day 3): Jokic plays tonight,
    Edwards (the drop) sits at BE today and at PG on Thursday's board."""
    jokic = player(JOKIC, "Nikola Jokic", UT, game=True, started=True)
    edwards = player(EDWARDS, "Anthony Edwards", BE, team="MIN", game=drop_game, started=drop_started)
    h.boards = {None: state([jokic, edwards]),
                3: state([player(JOKIC, "Nikola Jokic", UT), player(EDWARDS, "Anthony Edwards", PG, team="MIN")],
                         period=3, nba_date="2026-10-22", version="d3")}
    h.pool = {KAWHI: pool()}
    kawhi = player(KAWHI, "Kawhi Leonard", BE, team="LAC")
    h.after_txn = {None: state([jokic, kawhi], version="v2"),
                   3: state([player(JOKIC, "Nikola Jokic", UT), kawhi], period=3, nba_date="2026-10-22", version="d3b")}
    h.after_lineup = {3: state([player(JOKIC, "Nikola Jokic", UT), player(KAWHI, "Kawhi Leonard", PG, team="LAC")],
                               period=3, nba_date="2026-10-22", version="d3c")}


def arrange_day_of(h):
    """Thursday morning (ESPN day 3 = the pickup's day): the drop sits at PG with no game yet."""
    jokic = player(JOKIC, "Nikola Jokic", UT, game=True)
    edwards = player(EDWARDS, "Anthony Edwards", PG, team="MIN")
    h.boards = {None: state([jokic, edwards], period=3, current=3, nba_date="2026-10-22")}
    h.pool = {KAWHI: pool()}
    kawhi = player(KAWHI, "Kawhi Leonard", BE, team="LAC")
    h.after_txn = {None: state([jokic, kawhi], period=3, current=3, nba_date="2026-10-22", version="v2")}
    h.after_lineup = {None: state([jokic, player(KAWHI, "Kawhi Leonard", PG, team="LAC")], period=3, current=3,
                                  nba_date="2026-10-22", version="v3")}


def execute(h, r=None, now=NOW):
    return run(svc.ScheduledPickupService._execute_one(r or row(), now))


# ---- executed ------------------------------------------------------------------------


@pytest.mark.unit
def test_executed_the_day_before_sends_todays_transaction_and_a_future_roster_seat(h):
    arrange_day_before(h)
    r = execute(h)

    assert (r.outcome, r.reason, r.verified, r.seated_slot, r.audit_id) == ("executed", None, True, "PG", 1)
    assert r.team_name == "GloatingSoap369" and r.add.name == "Kawhi Leonard" and r.drop.name == "Anthony Edwards"
    pickup_id, fields = h.settled[0]
    assert pickup_id == 1 and fields["status"] == "executed" and fields["executed_at"] == NOW
    assert (fields["audit_id"], fields["lineup_audit_id"], fields["seated_slot_id"]) == (1, 2, PG)
    # the add/drop goes out against today's board, as a same-day transaction
    t = h.txn_calls[0]
    assert (t["add_player_id"], t["drop_player_id"], t["scoring_period_id"]) == (KAWHI, EDWARDS, 2)
    assert "current_scoring_period_id" not in t
    # the seat goes out against Thursday's board, as FUTURE_ROSTER, into the drop's old seat
    s = h.lineup_calls[0]
    assert (s["scoring_period_id"], s["current_scoring_period_id"]) == (3, 2)
    assert s["moves"] == [{"player_id": KAWHI, "from_slot_id": BE, "to_slot_id": PG}]
    assert [(a["source"], a["kind"]) for a in h.audits] == [("scheduled", "transaction"), ("scheduled", "lineup")]
    assert h.pool_calls == [([KAWHI], 2)]
    assert h.reads == [None, 3, None, 3, 3]     # today, D for the seat, verify, D after the add, D verify


@pytest.mark.unit
def test_executed_on_the_day_itself_seats_him_with_a_plain_roster_write(h):
    arrange_day_of(h)
    r = execute(h)

    assert (r.outcome, r.seated_slot, r.verified) == ("executed", "PG", True)
    assert h.txn_calls[0]["scoring_period_id"] == 3
    s = h.lineup_calls[0]
    assert s["scoring_period_id"] == 3 and "current_scoring_period_id" not in s
    assert h.reads == [None, None, None, None]


@pytest.mark.unit
def test_no_open_seat_leaves_him_on_the_bench_and_the_pickup_executed(h):
    arrange_day_before(h)
    full = state([player(JOKIC, "Nikola Jokic", UT), player(EMBIID, "Joel Embiid", PG, team="PHI"),
                  player(KAWHI, "Kawhi Leonard", BE, team="LAC")], period=3, nba_date="2026-10-22", version="d3b")
    h.after_txn[3] = full
    r = execute(h)

    assert (r.outcome, r.seated_slot, r.detail) == ("executed", None, "seat:no_open_slot")
    assert h.lineup_calls == [] and h.settled[0][1]["seated_slot_id"] is None


@pytest.mark.unit
def test_a_refused_seat_never_undoes_the_pickup(h):
    arrange_day_before(h)
    h.lineup = [FantasyWriterRejected("Lineup locked.", espn_status=400, espn_error_code="X")]
    r = execute(h)
    assert r.outcome == "executed" and r.seated_slot is None
    assert r.detail.startswith("seat:ROSTER_WRITE_REJECTED: Lineup locked.")
    assert h.settled[0][1]["status"] == "executed"


@pytest.mark.unit
def test_an_unverified_add_is_still_executed(h):
    arrange_day_before(h)
    h.after_txn = {None: h.boards[None], 3: h.boards[3]}       # the re-read does not show him yet
    r = execute(h)
    assert (r.outcome, r.reason, r.verified) == ("executed", "unverified", False)
    assert r.detail == "seat:not_on_board"


@pytest.mark.unit
def test_a_missing_drop_with_a_seat_free_becomes_add_only(h):
    arrange_day_before(h)
    h.boards[None] = state([player(JOKIC, "Nikola Jokic", UT, game=True)])   # Edwards gone, 1 of 4 seats used
    r = execute(h)
    assert (r.outcome, r.reason) == ("executed", "drop_missing")
    assert h.txn_calls[0]["drop_player_id"] is None and h.txn_calls[0]["add_player_id"] == KAWHI


@pytest.mark.unit
def test_a_missing_drop_without_a_seat_is_skipped(h):
    arrange_day_before(h)
    h.boards[None] = state([player(JOKIC, "Nikola Jokic", UT), player(EMBIID, "Joel Embiid", PG),
                            player(3, "Three", BE), player(4, "Four", BE)])             # 4 of 4 seats
    r = execute(h)
    assert (r.outcome, r.reason) == ("skipped", "drop_missing") and h.txn_calls == []


@pytest.mark.unit
def test_already_rostered_after_an_earlier_landed_write_is_executed_and_seated(h):
    arrange_day_before(h)
    h.boards[None] = h.after_txn[None]
    h.boards[3] = h.after_txn[3]
    r = execute(h, row(audit_id=41))
    assert (r.outcome, r.reason, r.audit_id, r.seated_slot) == ("executed", "already_rostered", 41, "PG")
    assert h.txn_calls == [] and len(h.lineup_calls) == 1


@pytest.mark.unit
def test_already_on_roster_by_hand_is_skipped(h):
    arrange_day_before(h)
    h.boards[None] = h.after_txn[None]
    r = execute(h)
    assert (r.outcome, r.reason) == ("skipped", "already_on_roster") and h.txn_calls == []


# ---- deferred -------------------------------------------------------------------------


@pytest.mark.unit
def test_a_drop_who_plays_tonight_waits_for_the_rollover(h):
    arrange_day_before(h, drop_game=True)
    r = execute(h)
    assert (r.outcome, r.reason, r.next_attempt_at) == ("deferred", "holder_plays_today", ROLLOVER)
    assert h.deferred == [(1, ROLLOVER, "holder_plays_today", None)] and h.txn_calls == []


@pytest.mark.unit
def test_a_locked_drop_the_day_before_waits_for_the_rollover(h):
    arrange_day_before(h)
    h.boards[None] = state([player(JOKIC, "Nikola Jokic", UT, game=True),
                            player(EDWARDS, "Anthony Edwards", BE, team="MIN", locked=True)])
    r = execute(h)
    assert (r.outcome, r.reason, r.next_attempt_at) == ("deferred", "drop_locked", ROLLOVER)


@pytest.mark.unit
def test_a_locked_add_the_day_before_waits_for_the_rollover(h):
    arrange_day_before(h)
    h.pool = {KAWHI: pool(locked=True)}
    r = execute(h)
    assert (r.outcome, r.reason, r.next_attempt_at) == ("deferred", "add_locked", ROLLOVER)


@pytest.mark.unit
def test_the_rollover_attempt_itself_retries_ten_minutes_later_while_espn_lags(h):
    # 2:00 AM Thursday ET: the board still says day 2 and the drop is locked from last night
    arrange_day_before(h)
    h.boards[None] = state([player(JOKIC, "Nikola Jokic", UT), player(EDWARDS, "Anthony Edwards", BE, team="MIN",
                                                                       game=True, started=True)])
    at = ROLLOVER
    r = execute(h, now=at)
    assert (r.outcome, r.reason, r.next_attempt_at) == ("deferred", "holder_plays_today", at + svc.RETRY)


@pytest.mark.unit
def test_a_locked_add_on_the_day_has_expired(h):
    arrange_day_of(h)
    h.pool = {KAWHI: pool(locked=True)}
    r = execute(h)
    assert (r.outcome, r.reason) == ("expired", "locked_on_day")


@pytest.mark.unit
def test_waivers_retry_hourly(h):
    arrange_day_before(h)
    h.pool = {KAWHI: pool(status="WAIVERS")}
    r = execute(h)
    assert (r.outcome, r.reason, r.next_attempt_at) == ("deferred", "add_on_waivers", NOW + svc.WAIVER_RETRY)


@pytest.mark.unit
def test_writer_outage_retries_in_five_minutes_and_audits_the_failure(h):
    arrange_day_before(h)
    h.txn = [FantasyWriterUnavailable("down", espn_status=None)]
    r = execute(h)
    assert (r.outcome, r.reason, r.next_attempt_at) == ("deferred", "writer_unavailable", NOW + svc.WRITER_RETRY)
    assert h.updates == [(1, "failed", None, "down")] and h.lineup_calls == []


@pytest.mark.unit
def test_a_provider_read_error_retries(h):
    arrange_day_before(h)
    h.boards[None] = RuntimeError("espn 503")
    r = execute(h)
    assert (r.outcome, r.reason, r.detail) == ("deferred", "provider_error", "espn 503")


@pytest.mark.unit
def test_a_deferral_past_the_deadline_expires_the_row(h):
    arrange_day_before(h)
    h.pool = {KAWHI: pool(status="WAIVERS")}
    r = execute(h, row(deadline_at=NOW + timedelta(minutes=30)))
    assert (r.outcome, r.reason) == ("expired", "deadline") and r.detail.startswith("add_on_waivers")
    assert h.deferred == [] and h.settled[0][1]["status"] == "expired"


@pytest.mark.unit
def test_the_attempt_budget_fails_the_row(h):
    arrange_day_before(h)
    h.pool = {KAWHI: pool(status="WAIVERS")}
    r = execute(h, row(attempts=svc.MAX_ATTEMPTS))
    assert (r.outcome, r.reason) == ("failed", "max_attempts")


# ---- settled without a write ------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("entry", [pool(status="ONTEAM", on_team=4), None])
def test_a_player_who_is_gone_is_the_no_op(h, entry):
    arrange_day_before(h)
    h.pool = {KAWHI: entry} if entry else {}
    r = execute(h)
    assert (r.outcome, r.reason) == ("skipped", "unavailable") and h.txn_calls == []
    assert h.settled[0][1]["status"] == "skipped"


@pytest.mark.unit
def test_a_passed_day_has_expired(h):
    arrange_day_of(h)
    h.boards[None] = state([], period=4, current=4, nba_date="2026-10-23")
    r = execute(h)
    assert (r.outcome, r.reason) == ("expired", "day_passed")


@pytest.mark.unit
def test_the_deadline_is_checked_before_anything_is_sent(h):
    arrange_day_of(h)
    r = execute(h, now=DEADLINE)
    assert (r.outcome, r.reason) == ("expired", "deadline") and h.txn_calls == []


@pytest.mark.unit
def test_roster_full_is_skipped_but_another_rejection_fails(h):
    arrange_day_before(h)
    h.txn = [FantasyWriterRejected("Roster is full.", espn_status=400, espn_error_code="TRAN_ROSTER_FULL")]
    r = execute(h)
    assert (r.outcome, r.reason, r.detail) == ("skipped", "roster_full", "Roster is full.")

    h.settled.clear(); h.txn_calls.clear()
    arrange_day_before(h)
    h.txn = [FantasyWriterRejected("Too many players with default position C (maximum 4)", espn_status=400,
                                   espn_error_code="TRAN_ROSTER_POSITION_LIMIT_EXCEEDED")]
    r = execute(h)
    assert (r.outcome, r.reason) == ("failed", "espn_rejected")
    assert r.detail == "Too many players with default position C (maximum 4)"


@pytest.mark.unit
def test_dead_cookies_fail_the_row(h):
    arrange_day_before(h)
    h.txn = [FantasyWriterAuthRejected("nope", espn_status=401)]
    r = execute(h)
    assert (r.outcome, r.reason) == ("failed", "auth_expired")


@pytest.mark.unit
def test_a_blocked_board_fails_for_a_lasting_reason_and_waits_for_a_passing_one(h):
    arrange_day_before(h)
    h.boards[None] = state([], can_write=False, reason="no_credentials")
    assert (execute(h).outcome, execute(h).reason) == ("failed", "can_write:no_credentials")
    h.boards[None] = state([], can_write=False, reason="no_scoring_period")
    assert execute(h).outcome == "deferred"


@pytest.mark.unit
def test_a_missing_team_fails_the_row(h, monkeypatch):
    from core.errors import NotFoundError

    def gone(team_id, user_id):
        raise NotFoundError("TEAM_NOT_FOUND", "Team not found")
    monkeypatch.setattr(svc, "_load_team_for_job", gone)
    r = execute(h)
    assert (r.outcome, r.reason) == ("failed", "team_not_found")


# ---- execute_due ------------------------------------------------------------------------


@pytest.mark.unit
def test_execute_due_claims_then_attempts_each_row(h, monkeypatch):
    arrange_day_before(h)
    monkeypatch.setattr(svc, "_claim_due", lambda now, limit: h.claimed.append((now, limit)) or [row(), row(id=2, add_player_id=EMBIID)])
    h.pool = {KAWHI: pool(), EMBIID: pool(pid=EMBIID, status="ONTEAM", on_team=4, name="Joel Embiid", team="PHI")}
    resp = run(svc.ScheduledPickupService.execute_due(PickupExecuteReq(limit=4, now=NOW)))
    assert h.claimed == [(NOW, 4)]
    assert resp.data.due == 2 and [r.outcome for r in resp.data.results] == ["executed", "skipped"]


@pytest.mark.unit
def test_execute_due_with_writes_off_pushes_rows_back_without_claiming(h, monkeypatch):
    monkeypatch.setattr(svc.settings, "roster_writes_enabled", False)
    monkeypatch.setattr(svc, "_claim_due", lambda now, limit: pytest.fail("must not claim"))
    monkeypatch.setattr(svc, "_release_due", lambda now, until: h.released.append((now, until)) or 3)
    resp = run(svc.ScheduledPickupService.execute_due(PickupExecuteReq(now=NOW)))
    assert h.released == [(NOW, NOW + svc.RETRY)]
    assert resp.data.due == 3 and resp.data.results == [] and "switched off" in resp.message


@pytest.mark.unit
def test_a_broken_row_is_deferred_and_the_batch_continues(h, monkeypatch):
    arrange_day_before(h)
    monkeypatch.setattr(svc, "_claim_due", lambda now, limit: [row(team_id=99), row(id=2)])

    def load_team(team_id, user_id):
        if team_id == 99:
            raise RuntimeError("db hiccup")
        return h.team
    monkeypatch.setattr(svc, "_load_team_for_job", load_team)

    resp = run(svc.ScheduledPickupService.execute_due(PickupExecuteReq(now=NOW)))
    outcomes = [(r.pickup_id, r.outcome) for r in resp.data.results]
    assert outcomes == [(1, "deferred"), (2, "executed")]
    assert h.deferred[0][2:] == ("error", "db hiccup")


# ---- schedule -----------------------------------------------------------------------------


@pytest.fixture
def scheduling(h, monkeypatch):
    h.windows = []
    h.inserted = []

    def window_for(nba_date, drop_team):
        h.windows.append((nba_date, drop_team))
        return svc.AttemptWindow(not_before_at=NOW, deadline_at=DEADLINE, rule="first_tip_prev")

    def insert(**fields):
        h.inserted.append(fields)
        return SimpleNamespace(id=7, team_id=fields["team"], user_id=fields["user"], attempts=0, next_attempt_at=None,
                               reason=None, detail=None, audit_id=None, lineup_audit_id=None, seated_slot_id=None,
                               executed_at=None, **{k: v for k, v in fields.items() if k not in ("team", "user")})

    monkeypatch.setattr(svc, "_window_for", window_for)
    monkeypatch.setattr(svc, "_insert_pickup", insert)
    # The lineup reader maps an ESPN day through the season calendar; pin it to 2026-27 here.
    from services import schedule_service
    monkeypatch.setattr(schedule_service, "date_for_espn_scoring_period",
                        lambda period, season=None: date(2026, 10, 20) + timedelta(days=int(period) - 1))
    h.boards = {None: state([player(JOKIC, "Nikola Jokic", UT, game=True, started=True),
                             player(EDWARDS, "Anthony Edwards", BE, team="MIN", locked=True)])}
    h.pool = {KAWHI: pool(locked=True)}
    return h


def schedule(req):
    return run(svc.ScheduledPickupService.schedule(TEAM, LEAGUE, req))


@pytest.mark.unit
def test_schedule_stores_the_row_ignoring_todays_locks(scheduling):
    h = scheduling
    resp = schedule(SchedulePickupReq(add_player_id=KAWHI, drop_player_id=EDWARDS, scoring_period_id=3))

    f = h.inserted[0]
    assert (f["team"], f["user"], f["add_player_id"], f["drop_player_id"]) == (21, 11, KAWHI, EDWARDS)
    assert (f["add_name"], f["add_team"], f["drop_name"], f["drop_team"]) == ("Kawhi Leonard", "LAC", "Anthony Edwards", "MIN")
    assert (f["scoring_period_id"], f["nba_date"], f["not_before_at"], f["deadline_at"]) == (3, DAY_D, NOW, DEADLINE)
    assert h.windows == [(DAY_D, "MIN")]
    assert resp.data.id == 7 and resp.data.status == "pending" and resp.data.add.name == "Kawhi Leonard"
    assert resp.data.drop.player_id == EDWARDS and "scheduled for ESPN day 3" in resp.message


@pytest.mark.unit
def test_schedule_accepts_a_player_on_waivers_and_an_open_seat(scheduling):
    h = scheduling
    h.pool = {KAWHI: pool(status="WAIVERS")}
    resp = schedule(SchedulePickupReq(add_player_id=KAWHI, scoring_period_id=5))
    assert resp.data.drop is None and h.windows == [(date(2026, 10, 24), None)]


@pytest.mark.unit
def test_schedule_refuses_today_and_the_past(scheduling):
    for day in (2, 1):
        with pytest.raises(svc.ScheduledPickupInvalid) as exc:
            schedule(SchedulePickupReq(add_player_id=KAWHI, scoring_period_id=day))
        assert exc.value.data["reason"] == "not_future"
    assert scheduling.inserted == []


@pytest.mark.unit
def test_schedule_refuses_a_day_past_the_season(scheduling):
    with pytest.raises(BadRequestError) as exc:
        schedule(SchedulePickupReq(add_player_id=KAWHI, scoring_period_id=168))
    assert exc.value.error_code == "SCORING_PERIOD_OUT_OF_RANGE"


@pytest.mark.unit
@pytest.mark.parametrize("req,reason", [
    (SchedulePickupReq(add_player_id=KAWHI, drop_player_id=EMBIID, scoring_period_id=3), "drop_not_on_roster"),
    (SchedulePickupReq(add_player_id=JOKIC, scoring_period_id=3), "add_already_on_roster"),
    (SchedulePickupReq(add_player_id=KAWHI, drop_player_id=KAWHI, scoring_period_id=3), "same_player"),
])
def test_schedule_translates_the_boards_refusals(scheduling, req, reason):
    with pytest.raises(svc.ScheduledPickupInvalid) as exc:
        schedule(req)
    assert exc.value.data["reason"] == reason


@pytest.mark.unit
def test_schedule_refuses_a_player_on_another_team_or_unknown(scheduling):
    h = scheduling
    h.pool = {KAWHI: pool(status="ONTEAM", on_team=4)}
    with pytest.raises(svc.ScheduledPickupInvalid) as exc:
        schedule(SchedulePickupReq(add_player_id=KAWHI, scoring_period_id=3))
    assert exc.value.data["reason"] == "add_not_available"
    h.pool = {}
    with pytest.raises(svc.ScheduledPickupInvalid) as exc:
        schedule(SchedulePickupReq(add_player_id=KAWHI, scoring_period_id=3))
    assert exc.value.data["reason"] == "add_not_found"


@pytest.mark.unit
def test_schedule_surfaces_a_duplicate_as_409(scheduling, monkeypatch):
    def dup(**fields):
        raise svc.ScheduledPickupDuplicate()
    monkeypatch.setattr(svc, "_insert_pickup", dup)
    with pytest.raises(svc.ScheduledPickupDuplicate):
        schedule(SchedulePickupReq(add_player_id=KAWHI, scoring_period_id=3))


@pytest.mark.unit
def test_schedule_gates(scheduling, monkeypatch):
    h = scheduling
    monkeypatch.setattr(svc.settings, "roster_writes_enabled", False)
    with pytest.raises(editor.RosterWriteDisabled):
        schedule(SchedulePickupReq(add_player_id=KAWHI, scoring_period_id=3))
    monkeypatch.setattr(svc.settings, "roster_writes_enabled", True)
    yahoo = LEAGUE.model_copy(update={"provider": FantasyProvider.YAHOO})
    with pytest.raises(editor.RosterWriteBlocked):
        run(svc.ScheduledPickupService.schedule(TEAM, yahoo, SchedulePickupReq(add_player_id=KAWHI, scoring_period_id=3)))
    h.boards[None] = state([], can_write=False, reason="not_team_owner")
    with pytest.raises(editor.RosterWriteBlocked) as exc:
        schedule(SchedulePickupReq(add_player_id=KAWHI, scoring_period_id=3))
    assert exc.value.data == {"reason": "not_team_owner"}


# ---- list / cancel --------------------------------------------------------------------------


@pytest.mark.unit
def test_list_splits_pending_from_recent_and_resolves_nba_ids(h, monkeypatch):
    rows = [row(id=1, status="pending", nba_date=date(2026, 10, 25)),
            row(id=2, status="pending", nba_date=date(2026, 10, 23)),
            row(id=3, status="executed", updated_at=NOW - timedelta(days=1), seated_slot_id=UT, audit_id=9)]
    monkeypatch.setattr(svc, "_list_pickups", lambda team_id, since: rows)
    monkeypatch.setattr(svc, "_nba_ids", lambda rows: {KAWHI: 1627, EDWARDS: 1630})
    resp = run(svc.ScheduledPickupService.list_for_team(TEAM))
    assert [p.id for p in resp.data.pending] == [2, 1]
    assert [p.id for p in resp.data.recent] == [3]
    assert resp.data.recent[0].seated_slot == "UT" and resp.data.recent[0].add.nba_player_id == 1627


@pytest.mark.unit
def test_cancel_outcomes(h, monkeypatch):
    monkeypatch.setattr(svc, "_cancel_pickup", lambda team_id, pickup_id, now: ("pending", row(status="cancelled")))
    resp = run(svc.ScheduledPickupService.cancel(TEAM, 1))
    assert resp.data.status == "cancelled"

    monkeypatch.setattr(svc, "_cancel_pickup", lambda team_id, pickup_id, now: ("executed", row(status="executed")))
    with pytest.raises(svc.ScheduledPickupNotPending) as exc:
        run(svc.ScheduledPickupService.cancel(TEAM, 1))
    assert exc.value.data["status"] == "executed"

    from core.errors import NotFoundError
    monkeypatch.setattr(svc, "_cancel_pickup", lambda team_id, pickup_id, now: None)
    with pytest.raises(NotFoundError):
        run(svc.ScheduledPickupService.cancel(TEAM, 1))


# ---- the manual route is untouched ----------------------------------------------------------


@pytest.mark.unit
def test_the_manual_transaction_still_audits_as_manual_and_refuses_locks(h):
    arrange_day_before(h)
    h.boards[None] = state([player(JOKIC, "Nikola Jokic", UT), player(EDWARDS, "Anthony Edwards", BE, team="MIN", locked=True)])
    from schemas.roster_transaction import RosterTransactionReq
    with pytest.raises(txn.RosterTransactionInvalid) as exc:
        run(txn.RosterTransactionService.apply(TEAM, LEAGUE, RosterTransactionReq(
            add_player_id=KAWHI, drop_player_id=EDWARDS, expected_scoring_period_id=2, roster_version="v1")))
    assert exc.value.data["reason"] == "drop_locked"
    h.boards[None] = state([player(JOKIC, "Nikola Jokic", UT)])
    run(txn.RosterTransactionService.apply(TEAM, LEAGUE, RosterTransactionReq(
        add_player_id=KAWHI, expected_scoring_period_id=2, roster_version="v1")))
    assert h.audits[0]["source"] == "manual"
