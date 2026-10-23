"""
services.roster_transaction_service without ESPN, the writer or Postgres: the
board read is a queue of prebuilt LineupStates (before and after the write),
the pool lookup a dict, the writer a queue of results/exceptions, and the
audit repository functions in-memory fakes — the lineup editor's harness, with
a pool.

Covers the gates (disabled, not ESPN, blocked, stale), every validation reason
(the lane holder and a pending scheduled pickup's drop included),
the three writer failures, verified add-only / drop-only / both, the
unverified re-read, and the verify-read failure that still settles the audit.
"""

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from core.errors import ProviderAuthError, ProviderTimeout
from schemas.common import FantasyProvider, LeagueInfo
from schemas.lineup_editor import LineupPlayer, LineupState
from schemas.roster_transaction import RosterTransactionReq
from services import fantasy_writer_client, roster_transaction_service as svc
from services.espn_service import PoolEntry
from services.fantasy_writer_client import (
    FantasyWriterAuthRejected,
    FantasyWriterRejected,
    FantasyWriterUnavailable,
    WriterResult,
)
from services.providers.capabilities import unavailable_message

PG, UT, BE = 0, 11, 12
COUNTS = {"11": 1, "12": 1}
LEAGUE = LeagueInfo(provider=FantasyProvider.ESPN, league_id=552315826, team_name="GloatingSoap369", year=2027,
                    espn_s2="s2", swid="{SWID}", espn_team_id=1)
TEAM = SimpleNamespace(team_id=21, user_id=11, league_info_json="{}", league_id=None, league=None)
JOKIC, EDWARDS, KAWHI, EMBIID = 3112335, 4594268, 6450, 3059318
MORNING = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)      # 11 AM ET on the board's day, before its 19:00 tip
EVENING = datetime(2026, 10, 21, 1, 30, tzinfo=timezone.utc)      # 9:30 PM ET, its games under way


def player(pid, name, slot=UT, *, locked=False, team="DEN"):
    return LineupPlayer(
        player_id=pid, name=name, team=team, lineup_slot_id=slot, lineup_slot=str(slot),
        eligible_slot_ids=[PG, UT, BE], eligible_slots=["PG", "UT", "BE"], lineup_locked=locked,
        has_game_today=True, opponent="vs LAL", game_time_et="19:30", game_started=False, locked=locked,
        playable=True, avg_points=40.0, value_kind="fpts", value_source="rolling",
    )


def state(players, *, can_write=True, reason=None, version="v1", period=1, nba_date="2026-10-20", final=167):
    return LineupState(
        provider=FantasyProvider.ESPN, team_name="GloatingSoap369", espn_team_id=1, nba_date=nba_date,
        scoring_period_id=period, scoring_period_source="provider", final_scoring_period_id=final,
        first_game_time_et="19:00",
        slot_counts=COUNTS, slots=[], lock_type="INDIVIDUAL_GAME", players=players,
        can_write=can_write, write_blocked_reason=reason, roster_version=version, fetched_at="now",
    )


def board(**kw):
    return [player(JOKIC, "Nikola Jokic", **kw), player(EDWARDS, "Anthony Edwards", BE, team="MIN")]


def pool(pid=KAWHI, status="FREEAGENT", on_team=0, locked=False, name="Kawhi Leonard", team="LAC", until=None):
    return PoolEntry(pid, status, on_team, locked, name, team, until)


def pickup(*, id=1, team_id=21, status="pending", drop=EDWARDS, add_name="Kawhi Leonard", nba_date=date(2026, 10, 22)):
    """A usr.scheduled_pickups row as the repository returns it (day 2026-10-22 is a Thursday)."""
    return SimpleNamespace(id=id, team_id=team_id, status=status, add_player_id=KAWHI, add_name=add_name,
                           drop_player_id=drop, nba_date=nba_date)


@pytest.fixture
def harness(monkeypatch):
    h = SimpleNamespace(reads=[], read_days=[], pool={}, pool_calls=[], writer=[], writer_calls=[], audits=[],
                        updates=[], lane_holders=set(), pickups=[], scheduled_calls=[])

    async def fake_read(team_id, league_info, **kwargs):
        h.read_days.append(kwargs.get("scoring_period_id"))     # None: today's board
        nxt = h.reads.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def fake_pool(league_info, player_ids, *, scoring_period_id=None):
        h.pool_calls.append((list(player_ids), scoring_period_id))
        return {pid: e for pid, e in h.pool.items() if pid in player_ids}

    async def fake_apply(payload):
        h.writer_calls.append(payload)
        nxt = h.writer.pop(0) if h.writer else WriterResult(True, 200, 200, "{}")
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def direct_run_db(name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def audit_insert(user_id, team_id, nba_date, period, source, moves, key, kind="lineup"):
        h.audits.append({"user_id": user_id, "team_id": team_id, "nba_date": nba_date, "period": period,
                         "source": source, "moves": moves, "key": key, "kind": kind})
        return len(h.audits)

    def audit_update(audit_id, status, *, provider_status=None, error=None):
        h.updates.append((audit_id, status, provider_status, error))

    def scheduled_drop(team_id, player_id):
        """The repository query in memory: the team's soonest pending pickup dropping the player."""
        h.scheduled_calls.append((team_id, player_id))
        rows = [r for r in h.pickups if r.team_id == team_id and r.status == "pending" and r.drop_player_id == player_id]
        return min(rows, key=lambda r: (r.nba_date, r.id), default=None)

    monkeypatch.setattr(svc.LineupReadService, "read", staticmethod(fake_read))
    monkeypatch.setattr(svc.EspnService, "get_player_pool_entries", staticmethod(fake_pool))
    monkeypatch.setattr(fantasy_writer_client, "apply_transaction", fake_apply)
    monkeypatch.setattr(svc, "run_db", direct_run_db)
    monkeypatch.setattr(svc, "_lane_holders", lambda team_id: h.lane_holders)   # no lane unless a test sets one
    monkeypatch.setattr(svc, "_scheduled_drop", scheduled_drop)                # no pickups unless a test adds one
    monkeypatch.setattr(svc, "_audit_insert", audit_insert)
    monkeypatch.setattr(svc, "_audit_update", audit_update)
    monkeypatch.setattr(svc.settings, "roster_writes_enabled", True)
    monkeypatch.setattr(svc, "_now", lambda: MORNING)                         # before the first tip unless a test moves it
    return h


def req(add=None, drop=None, *, version="v1", period=1):
    return RosterTransactionReq(add_player_id=add, drop_player_id=drop, expected_scoring_period_id=period,
                                roster_version=version)


def apply(r):
    return asyncio.run(svc.RosterTransactionService.apply(TEAM, LEAGUE, r))


# ---- gates ---------------------------------------------------------------------------


@pytest.mark.unit
def test_writes_disabled_is_refused_before_any_read(harness, monkeypatch):
    monkeypatch.setattr(svc.settings, "roster_writes_enabled", False)
    with pytest.raises(svc.RosterWriteDisabled):
        apply(req(add=KAWHI))
    assert harness.reads == [] and harness.writer_calls == []


@pytest.mark.unit
def test_yahoo_teams_are_blocked_with_a_reason(harness):
    yahoo = LEAGUE.model_copy(update={"provider": FantasyProvider.YAHOO})
    with pytest.raises(svc.RosterWriteBlocked) as exc:
        asyncio.run(svc.RosterTransactionService.apply(TEAM, yahoo, req(add=KAWHI)))
    assert exc.value.data == {"reason": "provider_not_supported"} and exc.value.message == unavailable_message("roster_changes", "yahoo")


@pytest.mark.unit
def test_blocked_board_carries_its_reason(harness):
    harness.reads = [state(board(), can_write=False, reason="not_team_owner")]
    with pytest.raises(svc.RosterWriteBlocked) as exc:
        apply(req(add=KAWHI))
    assert exc.value.data == {"reason": "not_team_owner"} and harness.writer_calls == []


@pytest.mark.unit
@pytest.mark.parametrize("kwargs", [{"version": "v9"}, {"period": 2}])
def test_stale_board_hands_back_the_fresh_one(harness, kwargs):
    harness.reads = [state(board(), **kwargs)]
    with pytest.raises(svc.RosterStale) as exc:
        apply(req(add=KAWHI, version="v1", period=1))
    assert exc.value.data["lineup"]["roster_version"] == kwargs.get("version", "v1")
    assert harness.writer_calls == [] and harness.pool_calls == []


# ---- validation ----------------------------------------------------------------------


def _invalid(harness, r, reason, player_id):
    harness.reads = [state(board(locked=reason == "drop_locked"))]
    with pytest.raises(svc.RosterTransactionInvalid) as exc:
        apply(r)
    assert exc.value.data == {"reason": reason, "player_id": player_id}, exc.value.data
    assert exc.value.status_code == 422 and exc.value.error_code == "ROSTER_TRANSACTION_INVALID"
    assert harness.writer_calls == [] and harness.audits == []
    return exc.value


@pytest.mark.unit
def test_nothing_to_do_and_same_player(harness):
    _invalid(harness, req(), "nothing_to_do", None)
    _invalid(harness, req(add=KAWHI, drop=KAWHI), "same_player", KAWHI)
    assert harness.pool_calls == []


@pytest.mark.unit
def test_drop_must_be_on_the_board_and_unlocked(harness):
    _invalid(harness, req(drop=KAWHI), "drop_not_on_roster", KAWHI)
    exc = _invalid(harness, req(add=KAWHI, drop=JOKIC), "drop_locked", JOKIC)
    assert "Nikola Jokic" in exc.message
    assert harness.pool_calls == []                                   # the board refuses before the pool is asked


@pytest.mark.unit
def test_the_player_a_stream_lane_holds_its_spot_with_cannot_be_dropped(harness):
    harness.lane_holders = {JOKIC, EDWARDS}     # two lanes: either holder is refused
    harness.pool = {KAWHI: pool()}
    exc = _invalid(harness, req(add=KAWHI, drop=EDWARDS), "drop_is_lane_holder", EDWARDS)
    assert "stream lane" in exc.message
    harness.lane_holders = {JOKIC}              # another player's spot: this drop goes through
    after = [player(JOKIC, "Nikola Jokic"), player(KAWHI, "Kawhi Leonard", BE, team="LAC")]
    harness.reads = [state(board()), state(after, version="v2")]
    assert apply(req(add=KAWHI, drop=EDWARDS)).data.verified is True


@pytest.mark.unit
def test_a_drop_a_pending_scheduled_pickup_already_makes_is_refused(harness):
    """That pickup would find him gone on its day. Drop-only and add+drop alike."""
    harness.pickups = [pickup(id=2, add_name="Joel Embiid", nba_date=date(2026, 10, 24)), pickup()]
    exc = _invalid(harness, req(drop=EDWARDS), "drop_is_scheduled", EDWARDS)
    assert exc.message == ("Anthony Edwards is already being dropped for Kawhi Leonard on Thu Oct 22 — "
                           "cancel that pickup to drop them now")                 # the soonest of the two
    harness.pool = {EMBIID: pool(EMBIID, name="Joel Embiid", team="PHI")}
    _invalid(harness, req(add=EMBIID, drop=EDWARDS), "drop_is_scheduled", EDWARDS)
    assert harness.scheduled_calls == [(21, EDWARDS), (21, EDWARDS)]


@pytest.mark.unit
def test_a_lane_holder_is_refused_as_one_before_his_lanes_pickup(harness):
    """A lane's switch is itself a pending pickup dropping the holder: the lane is what to stop."""
    harness.lane_holders = {EDWARDS}
    harness.pickups = [pickup()]
    _invalid(harness, req(drop=EDWARDS), "drop_is_lane_holder", EDWARDS)
    assert harness.scheduled_calls == []


@pytest.mark.unit
@pytest.mark.parametrize("other", [
    {"status": "executed"}, {"status": "skipped"}, {"status": "failed"}, {"status": "cancelled"},
    {"status": "expired"}, {"drop": JOKIC}, {"drop": None}, {"team_id": 99},
])
def test_a_settled_pickup_or_one_dropping_someone_else_does_not_block(harness, other):
    harness.pickups = [pickup(**other)]
    harness.reads = [state(board()), state([player(JOKIC, "Nikola Jokic")], version="v2")]
    resp = apply(req(drop=EDWARDS))
    assert resp.data.verified is True and resp.data.dropped.name == "Anthony Edwards"
    assert harness.scheduled_calls == [(21, EDWARDS)] and len(harness.writer_calls) == 1


@pytest.mark.unit
def test_an_add_alone_never_looks_for_scheduled_drops(harness):
    harness.pickups = [pickup()]
    harness.reads = [state(board()), state(board() + [player(KAWHI, "Kawhi Leonard", BE, team="LAC")], version="v2")]
    harness.pool = {KAWHI: pool()}
    assert apply(req(add=KAWHI)).data.verified is True and harness.scheduled_calls == []


@pytest.mark.unit
def test_add_already_on_roster_never_asks_the_pool(harness):
    exc = _invalid(harness, req(add=EDWARDS), "add_already_on_roster", EDWARDS)
    assert "Anthony Edwards" in exc.message and harness.pool_calls == []


@pytest.mark.unit
def test_pool_refusals(harness):
    harness.pool = {}
    _invalid(harness, req(add=KAWHI), "add_not_found", KAWHI)
    assert harness.pool_calls == [([KAWHI], 1)]                       # the board's period, never 0

    harness.pool = {KAWHI: pool(status="WAIVERS", until=date(2026, 10, 25))}
    exc = _invalid(harness, req(add=KAWHI), "add_on_waivers", KAWHI)
    assert "Kawhi Leonard" in exc.message and "2026-10-25" in exc.message

    harness.pool = {KAWHI: pool(status="ONTEAM", on_team=3)}
    _invalid(harness, req(add=KAWHI), "add_not_available", KAWHI)
    harness.pool = {KAWHI: pool(status=None, on_team=3)}              # a status ESPN forgot: onTeamId still says rostered
    _invalid(harness, req(add=KAWHI), "add_not_available", KAWHI)

    harness.pool = {KAWHI: pool(locked=True)}
    _invalid(harness, req(add=KAWHI), "add_locked", KAWHI)


# ---- writer failures ----------------------------------------------------------------


@pytest.mark.unit
def test_rejected_is_audited_and_carries_espn_sentence(harness):
    harness.reads = [state(board())]
    harness.pool = {KAWHI: pool()}
    harness.writer = [FantasyWriterRejected("Roster is full.", espn_status=400, espn_error_code="TRAN_ROSTER_FULL")]
    with pytest.raises(svc.RosterWriteRejected) as exc:
        apply(req(add=KAWHI))
    assert exc.value.message == "Roster is full."
    assert exc.value.data == {"espn_status": 400, "espn_error_code": "TRAN_ROSTER_FULL"}
    assert harness.updates == [(1, "rejected", 400, "Roster is full.")]
    assert len(harness.reads) == 0 and len(harness.writer_calls) == 1  # no re-read after a rejection


@pytest.mark.unit
def test_dead_cookies_become_provider_auth_expired(harness):
    harness.reads = [state(board())]
    harness.writer = [FantasyWriterAuthRejected("nope", espn_status=401)]
    with pytest.raises(ProviderAuthError) as exc:
        apply(req(drop=EDWARDS))
    assert exc.value.data["provider"] == "espn"
    assert harness.updates == [(1, "failed", 401, "provider_auth_rejected")]


@pytest.mark.unit
def test_writer_down_is_unavailable_and_audited_failed(harness):
    harness.reads = [state(board())]
    harness.writer = [FantasyWriterUnavailable("down")]
    with pytest.raises(svc.RosterWriteUnavailable):
        apply(req(drop=EDWARDS))
    assert harness.updates == [(1, "failed", None, "down")]


# ---- applied ----------------------------------------------------------------------


@pytest.mark.unit
def test_add_and_drop_are_one_transaction_verified_by_the_reread(harness):
    after = [player(JOKIC, "Nikola Jokic"), player(KAWHI, "Kawhi Leonard", BE, team="LAC")]
    harness.reads = [state(board()), state(after, version="v2")]
    harness.pool = {KAWHI: pool()}

    resp = apply(req(add=KAWHI, drop=EDWARDS))

    d = resp.data
    assert d.verified is True and d.lineup.roster_version == "v2" and d.audit_id == 1 and d.scoring_period_id == 1
    assert d.counts_from == "2026-10-20" and harness.read_days == [None, None]   # before the tip it counts today
    assert (d.added.player_id, d.added.name, d.added.team) == (KAWHI, "Kawhi Leonard", "LAC")
    assert (d.dropped.player_id, d.dropped.name, d.dropped.team) == (EDWARDS, "Anthony Edwards", "MIN")
    assert resp.message == "Added Kawhi Leonard, Dropped Anthony Edwards — sent to ESPN"

    payload = harness.writer_calls[0]
    assert payload == {"season": 2027, "league_id": 552315826, "espn_team_id": 1, "member_id": "{SWID}",
                       "credentials": {"espn_s2": "s2", "swid": "{SWID}"}, "scoring_period_id": 1,
                       "add_player_id": KAWHI, "drop_player_id": EDWARDS,
                       "idempotency_key": f"21:txn:1:v1:{KAWHI}:{EDWARDS}"}
    assert "moves" not in payload

    audit = harness.audits[0]
    assert (audit["kind"], audit["source"], audit["nba_date"], audit["period"]) == ("transaction", "manual", date(2026, 10, 20), 1)
    assert audit["moves"] == [{"player_id": KAWHI, "action": "add", "name": "Kawhi Leonard"},
                              {"player_id": EDWARDS, "action": "drop", "name": "Anthony Edwards"}]
    assert audit["key"] == f"21:txn:1:v1:{KAWHI}:{EDWARDS}"
    assert harness.updates == [(1, "applied", 200, None)]
    assert harness.pool_calls == [([KAWHI], 1)]


@pytest.mark.unit
def test_an_ai_chip_is_checked_before_the_write_and_marked_sent_after_it(harness, monkeypatch):
    from core.errors import ConflictError
    from services.ai import proposals
    seen = SimpleNamespace(for_send=[], sent=[], refuse=None)

    async def for_send(proposal_id, user_id, team_id, *, kind, add, drop, scoring_period_id=None, now=None):
        seen.for_send.append((proposal_id, user_id, team_id, kind, add, drop, scoring_period_id, len(harness.writer_calls)))
        if seen.refuse:
            raise seen.refuse

    async def mark_sent(proposal_id, *, audit_id=None, pickup_id=None, now=None):
        seen.sent.append((proposal_id, audit_id, pickup_id))
    monkeypatch.setattr(proposals, "for_send", for_send)
    monkeypatch.setattr(proposals, "mark_sent", mark_sent)
    after = [player(JOKIC, "Nikola Jokic"), player(KAWHI, "Kawhi Leonard", BE, team="LAC")]
    harness.reads = [state(board()), state(after, version="v2")]
    harness.pool = {KAWHI: pool()}

    resp = apply(RosterTransactionReq(add_player_id=KAWHI, drop_player_id=EDWARDS, expected_scoring_period_id=1,
                                      roster_version="v1", proposal_id=31))

    assert resp.data.verified is True and resp.data.audit_id == 1
    # Checked before ESPN was asked (no writer call yet), marked with the audit row after
    assert seen.for_send == [(31, 11, 21, "transaction", KAWHI, EDWARDS, None, 0)]
    assert seen.sent == [(31, 1, None)]

    # A chip that isn't this move (or isn't pending any more) stops the write before ESPN
    seen.refuse = ConflictError("AI_PROPOSAL_NOT_PENDING", "This move was already sent", data={"state": "sent"})
    harness.reads = [state(board())]
    with pytest.raises(ConflictError):
        apply(RosterTransactionReq(add_player_id=KAWHI, drop_player_id=EDWARDS, expected_scoring_period_id=1,
                                   roster_version="v1", proposal_id=31))
    assert len(harness.writer_calls) == 1 and len(seen.sent) == 1

    # The Week desk's own moves never touch the proposals
    harness.reads = [state(board()), state(after, version="v2")]
    apply(req(add=KAWHI, drop=EDWARDS))
    assert len(seen.for_send) == 2 and len(seen.sent) == 1


@pytest.mark.unit
def test_add_only(harness):
    harness.reads = [state(board()), state(board() + [player(KAWHI, "Kawhi Leonard", BE, team="LAC")], version="v2")]
    harness.pool = {KAWHI: pool()}
    resp = apply(req(add=KAWHI))
    assert resp.data.verified is True and resp.data.dropped is None and resp.data.added.player_id == KAWHI
    assert resp.message == "Added Kawhi Leonard — sent to ESPN"
    payload = harness.writer_calls[0]
    assert (payload["add_player_id"], payload["drop_player_id"], payload["idempotency_key"]) == (KAWHI, None, f"21:txn:1:v1:{KAWHI}:0")
    assert harness.audits[0]["moves"] == [{"player_id": KAWHI, "action": "add", "name": "Kawhi Leonard"}]


@pytest.mark.unit
def test_drop_only_never_asks_the_pool(harness):
    harness.reads = [state(board()), state([player(JOKIC, "Nikola Jokic")], version="v2")]
    resp = apply(req(drop=EDWARDS))
    assert resp.data.verified is True and resp.data.added is None and resp.data.dropped.name == "Anthony Edwards"
    assert resp.message == "Dropped Anthony Edwards — sent to ESPN"
    payload = harness.writer_calls[0]
    assert (payload["add_player_id"], payload["drop_player_id"], payload["idempotency_key"]) == (None, EDWARDS, f"21:txn:1:v1:0:{EDWARDS}")
    assert harness.pool_calls == []
    assert harness.audits[0]["moves"] == [{"player_id": EDWARDS, "action": "drop", "name": "Anthony Edwards"}]


@pytest.mark.unit
def test_unverified_when_the_drop_is_still_on_the_board(harness):
    after = board() + [player(KAWHI, "Kawhi Leonard", BE, team="LAC")]  # the add landed, Edwards did not leave
    harness.reads = [state(board()), state(after, version="v2")]
    harness.pool = {KAWHI: pool()}
    resp = apply(req(add=KAWHI, drop=EDWARDS))
    assert resp.data.verified is False and resp.message.endswith("not yet confirmed")
    assert harness.updates == [(1, "applied_unverified", 200, None)]


@pytest.mark.unit
def test_unverified_when_the_add_is_missing_from_the_board(harness):
    harness.reads = [state(board()), state(board(), version="v1b")]
    harness.pool = {KAWHI: pool()}
    resp = apply(req(add=KAWHI))
    assert resp.data.verified is False and harness.updates[-1][1] == "applied_unverified"


@pytest.mark.unit
def test_a_failed_verify_read_still_settles_the_audit(harness):
    """ESPN took the transaction; only the read-back died. The row must leave `in_flight`."""
    harness.reads = [state(board()), ProviderTimeout("espn")]
    harness.pool = {KAWHI: pool()}
    resp = apply(req(add=KAWHI, drop=EDWARDS))
    assert resp.data.verified is False and resp.data.lineup.roster_version == "v1"   # the pre-write board
    assert resp.data.added.name == "Kawhi Leonard" and resp.data.dropped.name == "Anthony Edwards"
    audit_id, status, provider_status, error = harness.updates[-1]
    assert (audit_id, status, provider_status) == (1, "applied_unverified", 200)
    assert error.startswith("verify_read_failed:")


@pytest.mark.unit
def test_a_board_without_a_period_cannot_be_written(harness):
    harness.reads = [state(board(), period=None, nba_date=None)]
    with pytest.raises(svc.RosterStale):                                # the request names period 1; the board has none
        apply(req(drop=EDWARDS, period=1))


# ---- after today's first tip: an add counts from tomorrow --------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("now, tip, final, expected", [
    (MORNING, "19:00", 5, None),                        # before the tip: today
    (EVENING, "19:00", 5, 2),                           # after it: tomorrow
    (datetime(2026, 10, 20, 23, 0, tzinfo=timezone.utc), "19:00", 5, 2),      # 19:00 ET on the dot
    (EVENING, None, 5, None),                           # a day without games: today until it turns
    (EVENING, "19:00", 1, None),                        # the season's last day has no tomorrow
    (EVENING, "19:00", None, None),                     # no last day reported: tomorrow's board can't be read
])
def test_next_day_period(now, tip, final, expected):
    b = state(board()).model_copy(update={"first_game_time_et": tip, "final_scoring_period_id": final})
    assert svc.next_day_period(b, now) == expected


@pytest.mark.unit
def test_an_add_after_the_first_tip_goes_now_and_is_checked_on_tomorrows_board(harness, monkeypatch):
    """James, 2026-10-23: an add for tomorrow once today's games have started is a pickup made now,
    not a scheduled one. ESPN keeps today's roster and puts the swap on tomorrow's: the player
    dropped plays out tonight even with his game under way, and the add's own lock is today's."""
    monkeypatch.setattr(svc, "_now", lambda: EVENING)
    today = [player(JOKIC, "Nikola Jokic"), player(EDWARDS, "Anthony Edwards", BE, team="MIN", locked=True)]
    tomorrow = [player(JOKIC, "Nikola Jokic"), player(KAWHI, "Kawhi Leonard", BE, team="LAC")]
    harness.reads = [state(today), state(today, period=2, nba_date="2026-10-21"),
                     state(tomorrow, period=2, nba_date="2026-10-21", version="t2"), state(today, version="v1")]
    harness.pool = {KAWHI: pool(locked=True)}

    resp = apply(req(add=KAWHI, drop=EDWARDS))

    d = resp.data
    assert harness.read_days == [None, 2, 2, None]       # the board, tomorrow's to check and to verify, today's to hand back
    assert harness.pool_calls == [([KAWHI], 1)]          # the pool stays today's
    assert d.verified is True and d.counts_from == "2026-10-21"
    assert (d.lineup.scoring_period_id, d.lineup.nba_date, d.scoring_period_id) == (1, "2026-10-20", 1)
    assert harness.writer_calls[0]["scoring_period_id"] == 1                  # sent on today's board, as ESPN takes it
    assert harness.audits[0]["period"] == 1 and harness.updates == [(1, "applied", 200, None)]
    assert resp.message == "Added Kawhi Leonard, Dropped Anthony Edwards — sent to ESPN, counts from Wed Oct 21"


@pytest.mark.unit
def test_an_add_espn_took_for_today_right_at_the_tip_counts_today(harness, monkeypatch):
    # Sent on the minute of the tip, before ESPN cut off: the swap is on today's board as well
    monkeypatch.setattr(svc, "_now", lambda: datetime(2026, 10, 20, 23, 0, 30, tzinfo=timezone.utc))
    swapped = [player(JOKIC, "Nikola Jokic"), player(KAWHI, "Kawhi Leonard", BE, team="LAC")]
    harness.reads = [state(board()), state(board(), period=2, nba_date="2026-10-21"),
                     state(swapped, period=2, nba_date="2026-10-21"), state(swapped, version="v2")]
    harness.pool = {KAWHI: pool()}
    resp = apply(req(add=KAWHI, drop=EDWARDS))
    assert resp.data.verified is True and resp.data.counts_from == "2026-10-20"
    assert resp.data.lineup.roster_version == "v2"
    assert resp.message == "Added Kawhi Leonard, Dropped Anthony Edwards — sent to ESPN"


@pytest.mark.unit
def test_after_the_first_tip_todays_board_unreadable_hands_back_the_one_read_before(harness, monkeypatch):
    monkeypatch.setattr(svc, "_now", lambda: EVENING)
    tomorrow = [player(JOKIC, "Nikola Jokic"), player(KAWHI, "Kawhi Leonard", BE, team="LAC")]
    harness.reads = [state(board()), state(board(), period=2, nba_date="2026-10-21"),
                     state(tomorrow, period=2, nba_date="2026-10-21"), ProviderTimeout("espn")]
    harness.pool = {KAWHI: pool()}
    d = apply(req(add=KAWHI, drop=EDWARDS)).data
    assert d.verified is True and d.lineup.scoring_period_id == 1 and d.lineup.roster_version == "v1"


@pytest.mark.unit
def test_an_add_across_espns_day_turn_counts_from_the_new_day(harness, monkeypatch):
    # Sent just before 3 AM ET; read again just after, today's board is already Wednesday's, the add on it
    monkeypatch.setattr(svc, "_now", lambda: datetime(2026, 10, 21, 6, 59, 59, tzinfo=timezone.utc))
    swapped = [player(JOKIC, "Nikola Jokic"), player(KAWHI, "Kawhi Leonard", BE, team="LAC")]
    harness.reads = [state(board()), state(board(), period=2, nba_date="2026-10-21"),
                     state(swapped, period=2, nba_date="2026-10-21"),
                     state(swapped, period=2, nba_date="2026-10-21", version="w2")]
    harness.pool = {KAWHI: pool()}
    d = apply(req(add=KAWHI, drop=EDWARDS)).data
    assert d.verified is True and d.counts_from == "2026-10-21"
    assert d.lineup.roster_version == "w2"


@pytest.mark.unit
def test_after_the_first_tip_a_player_added_tonight_can_be_swapped_out_again(harness, monkeypatch):
    # Kawhi came in for Edwards an hour ago: he is on tomorrow's roster only, Edwards on today's only
    monkeypatch.setattr(svc, "_now", lambda: EVENING)
    today = [player(JOKIC, "Nikola Jokic"), player(EDWARDS, "Anthony Edwards", BE, team="MIN", locked=True)]
    tomorrow = [player(JOKIC, "Nikola Jokic"), player(KAWHI, "Kawhi Leonard", BE, team="LAC")]
    after = [player(JOKIC, "Nikola Jokic"), player(EMBIID, "Joel Embiid", BE, team="PHI")]
    harness.reads = [state(today), state(tomorrow, period=2, nba_date="2026-10-21"),
                     state(after, period=2, nba_date="2026-10-21"), state(today)]
    harness.pool = {EMBIID: pool(EMBIID, name="Joel Embiid", team="PHI")}
    resp = apply(req(add=EMBIID, drop=KAWHI))
    assert resp.data.verified is True and resp.data.dropped.name == "Kawhi Leonard"
    assert harness.audits[0]["moves"][1] == {"player_id": KAWHI, "action": "drop", "name": "Kawhi Leonard"}

    # Edwards, gone from tomorrow's roster, can't be dropped a second time
    harness.reads = [state(today), state(tomorrow, period=2, nba_date="2026-10-21")]
    with pytest.raises(svc.RosterTransactionInvalid) as exc:
        apply(req(add=EMBIID, drop=EDWARDS))
    assert exc.value.data["reason"] == "drop_not_on_roster"


@pytest.mark.unit
def test_after_the_first_tip_tomorrows_board_unreadable_checks_the_add_on_todays(harness, monkeypatch):
    monkeypatch.setattr(svc, "_now", lambda: EVENING)
    tomorrow = [player(JOKIC, "Nikola Jokic"), player(KAWHI, "Kawhi Leonard", BE, team="LAC")]
    harness.reads = [state(board()), ProviderTimeout("espn"), state(tomorrow, period=2, nba_date="2026-10-21"),
                     state(board())]
    harness.pool = {KAWHI: pool()}
    d = apply(req(add=KAWHI, drop=EDWARDS)).data
    assert d.verified is True and d.dropped.name == "Anthony Edwards"


@pytest.mark.unit
def test_after_the_first_tip_a_drop_alone_goes_now_and_he_plays_out_today(harness, monkeypatch):
    """James, 2026-10-23: drops behave as adds do. Jokic's game has started; dropped now, ESPN
    takes him off tomorrow's roster and his game tonight still counts."""
    monkeypatch.setattr(svc, "_now", lambda: EVENING)
    today = board(locked=True)
    harness.reads = [state(today), state(board(), period=2, nba_date="2026-10-21"),
                     state([player(EDWARDS, "Anthony Edwards", BE, team="MIN")], period=2, nba_date="2026-10-21",
                           version="t2"),
                     state(today)]
    resp = apply(req(drop=JOKIC))
    d = resp.data
    assert harness.read_days == [None, 2, 2, None]
    assert d.verified is True and d.counts_from == "2026-10-21" and d.lineup.scoring_period_id == 1
    assert harness.writer_calls[0]["scoring_period_id"] == 1
    assert resp.message == "Dropped Nikola Jokic — sent to ESPN, counts from Wed Oct 21"


@pytest.mark.unit
def test_a_drop_espn_took_for_today_right_at_the_tip_counts_today(harness, monkeypatch):
    monkeypatch.setattr(svc, "_now", lambda: datetime(2026, 10, 20, 23, 0, 30, tzinfo=timezone.utc))
    left = [player(EDWARDS, "Anthony Edwards", BE, team="MIN")]
    harness.reads = [state(board()), state(board(), period=2, nba_date="2026-10-21"),
                     state(left, period=2, nba_date="2026-10-21"), state(left, version="v2")]
    d = apply(req(drop=JOKIC)).data
    assert d.verified is True and d.counts_from == "2026-10-20"


@pytest.mark.unit
def test_after_the_first_tip_dropping_a_player_added_tonight_counts_from_tomorrow(harness, monkeypatch):
    # Kawhi came in for Edwards an hour ago: on tomorrow's roster only, so never on today's
    monkeypatch.setattr(svc, "_now", lambda: EVENING)
    today = [player(JOKIC, "Nikola Jokic"), player(EDWARDS, "Anthony Edwards", BE, team="MIN", locked=True)]
    tomorrow = [player(JOKIC, "Nikola Jokic"), player(KAWHI, "Kawhi Leonard", BE, team="LAC")]
    harness.reads = [state(today), state(tomorrow, period=2, nba_date="2026-10-21"),
                     state([player(JOKIC, "Nikola Jokic")], period=2, nba_date="2026-10-21"), state(today)]
    resp = apply(req(drop=KAWHI))
    assert resp.data.verified is True and resp.data.counts_from == "2026-10-21"
    assert resp.message == "Dropped Kawhi Leonard — sent to ESPN, counts from Wed Oct 21"


@pytest.mark.unit
def test_on_the_seasons_last_day_an_add_after_the_tip_keeps_todays_locks(harness, monkeypatch):
    monkeypatch.setattr(svc, "_now", lambda: EVENING)
    harness.reads = [state(board(locked=True)).model_copy(update={"final_scoring_period_id": 1})]
    harness.pool = {KAWHI: pool()}
    with pytest.raises(svc.RosterTransactionInvalid) as exc:
        apply(req(add=KAWHI, drop=JOKIC))
    assert exc.value.data["reason"] == "drop_locked"


# ---- the executor's day-before check ---------------------------------------------------


@pytest.mark.unit
def test_check_locks_false_skips_the_locks_but_not_waivers(harness):
    board_state = state([player(JOKIC, "Nikola Jokic"), player(EDWARDS, "Anthony Edwards", BE, team="MIN", locked=True)])
    harness.pool = {KAWHI: pool(locked=True)}
    holder, entry = asyncio.run(svc.RosterTransactionService._validate(LEAGUE, board_state, KAWHI, EDWARDS, check_locks=False))
    assert holder.player_id == EDWARDS and entry.player_id == KAWHI

    harness.pool = {KAWHI: pool(status="WAIVERS")}
    with pytest.raises(svc.RosterTransactionInvalid) as exc:
        asyncio.run(svc.RosterTransactionService._validate(LEAGUE, board_state, KAWHI, EDWARDS, check_locks=False))
    assert exc.value.data["reason"] == "add_on_waivers"

    with pytest.raises(svc.RosterTransactionInvalid) as exc:
        asyncio.run(svc.RosterTransactionService._validate(LEAGUE, board_state, KAWHI, EDWARDS))
    assert exc.value.data["reason"] == "drop_locked"

