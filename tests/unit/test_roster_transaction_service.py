"""
services.roster_transaction_service without ESPN, the writer or Postgres: the
board read is a queue of prebuilt LineupStates (before and after the write),
the pool lookup a dict, the writer a queue of results/exceptions, and the
audit repository functions in-memory fakes — the lineup editor's harness, with
a pool.

Covers the gates (disabled, not ESPN, blocked, stale), every validation reason,
the three writer failures, verified add-only / drop-only / both, the
unverified re-read, and the verify-read failure that still settles the audit.
"""

import asyncio
from datetime import date
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

PG, UT, BE = 0, 11, 12
COUNTS = {"11": 1, "12": 1}
LEAGUE = LeagueInfo(provider=FantasyProvider.ESPN, league_id=552315826, team_name="GloatingSoap369", year=2027,
                    espn_s2="s2", swid="{SWID}", espn_team_id=1)
TEAM = SimpleNamespace(team_id=21, user_id=11, league_info_json="{}", league_id=None, league=None)
JOKIC, EDWARDS, KAWHI, EMBIID = 3112335, 4594268, 6450, 3059318


def player(pid, name, slot=UT, *, locked=False, team="DEN"):
    return LineupPlayer(
        player_id=pid, name=name, team=team, lineup_slot_id=slot, lineup_slot=str(slot),
        eligible_slot_ids=[PG, UT, BE], eligible_slots=["PG", "UT", "BE"], lineup_locked=locked,
        has_game_today=True, opponent="vs LAL", game_time_et="19:30", game_started=False, locked=locked,
        playable=True, avg_points=40.0, value_kind="fpts", value_source="rolling",
    )


def state(players, *, can_write=True, reason=None, version="v1", period=1, nba_date="2026-10-20"):
    return LineupState(
        provider=FantasyProvider.ESPN, team_name="GloatingSoap369", espn_team_id=1, nba_date=nba_date,
        scoring_period_id=period, scoring_period_source="provider", first_game_time_et="19:00",
        slot_counts=COUNTS, slots=[], lock_type="INDIVIDUAL_GAME", players=players,
        can_write=can_write, write_blocked_reason=reason, roster_version=version, fetched_at="now",
    )


def board(**kw):
    return [player(JOKIC, "Nikola Jokic", **kw), player(EDWARDS, "Anthony Edwards", BE, team="MIN")]


def pool(pid=KAWHI, status="FREEAGENT", on_team=0, locked=False, name="Kawhi Leonard", team="LAC", until=None):
    return PoolEntry(pid, status, on_team, locked, name, team, until)


@pytest.fixture
def harness(monkeypatch):
    h = SimpleNamespace(reads=[], pool={}, pool_calls=[], writer=[], writer_calls=[], audits=[], updates=[])

    async def fake_read(team_id, league_info, **kwargs):
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

    monkeypatch.setattr(svc.LineupReadService, "read", staticmethod(fake_read))
    monkeypatch.setattr(svc.EspnService, "get_player_pool_entries", staticmethod(fake_pool))
    monkeypatch.setattr(fantasy_writer_client, "apply_transaction", fake_apply)
    monkeypatch.setattr(svc, "run_db", direct_run_db)
    monkeypatch.setattr(svc, "_audit_insert", audit_insert)
    monkeypatch.setattr(svc, "_audit_update", audit_update)
    monkeypatch.setattr(svc.settings, "roster_writes_enabled", True)
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
    assert exc.value.data == {"reason": "provider_not_supported"} and exc.value.message == svc.NOT_ESPN_MESSAGE


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
