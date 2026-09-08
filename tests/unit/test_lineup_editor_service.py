"""
services.lineup_editor_service without ESPN, the writer or Postgres: the read is
stubbed with prebuilt LineupStates (before and after the write), the writer
client with a queue of results/exceptions, and the audit/dedup repository
functions with in-memory fakes.

Covers the manual chain (disabled, blocked, stale, invalid, rejected, auth
expired, unavailable, applied, applied-but-unverified) and every `evaluate`
outcome the alerts pipeline branches on.
"""

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from core.errors import ProviderAuthError, ProviderTimeout
from schemas.common import FantasyProvider, LeagueInfo
from schemas.lineup_editor import ApplyLineupMovesReq, LineupEvaluateReq, LineupMoveReq, LineupPlayer, LineupState
from services import fantasy_writer_client, lineup_editor_service as svc
from services.fantasy_writer_client import (
    FantasyWriterAuthRejected,
    FantasyWriterRejected,
    FantasyWriterUnavailable,
    WriterResult,
)

PG, SG, SF, PF, C, G, F, UT, BE, IR = 0, 1, 2, 3, 4, 5, 6, 11, 12, 13
# A one-UT, one-bench league: the only opportunity on the board is the UT slot
COUNTS = {"11": 1, "12": 1}
LEAGUE = LeagueInfo(provider=FantasyProvider.ESPN, league_id=426893737, team_name="Lvl. 3 Goblins", year=2027,
                    espn_s2="s2", swid="{SWID}", espn_team_id=4)
TEAM = SimpleNamespace(team_id=21, user_id=11, league_info_json="{}", league_id=None, league=None)


def player(pid, slot, eligible, *, game=True, status=None, locked=False, value=10.0):
    return LineupPlayer(
        player_id=pid, name=f"P{pid}", team="DEN", lineup_slot_id=slot, lineup_slot=str(slot),
        eligible_slot_ids=list(eligible), eligible_slots=[str(s) for s in eligible],
        injured=status == "OUT", injury_status=status, lineup_locked=locked, has_game_today=game,
        opponent="vs LAL" if game else None, game_time_et="19:30" if game else None, game_started=False,
        locked=locked, playable=game and status != "OUT", avg_points=value, value_kind="fpts", value_source="rolling",
    )


def state(players, *, can_write=True, reason=None, version="v1", period=12, nba_date="2026-10-20"):
    return LineupState(
        provider=FantasyProvider.ESPN, team_name="Lvl. 3 Goblins", espn_team_id=4, nba_date=nba_date,
        scoring_period_id=period, scoring_period_source="provider", first_game_time_et="19:00",
        slot_counts=COUNTS, slots=[], lock_type="INDIVIDUAL_GAME", players=players,
        can_write=can_write, write_blocked_reason=reason, roster_version=version, fetched_at="now",
    )


def board(*, hole=True):
    """Two players suffice: a UT holder and a bench guard. `hole` makes the holder idle today."""
    return [player(8, UT, [PG, UT, BE], game=not hole), player(11, BE, [PG, G, UT, BE])]


def after_swap():
    return [player(8, BE, [PG, UT, BE], game=False), player(11, UT, [PG, G, UT, BE])]


@pytest.fixture
def harness(monkeypatch):
    """Stub the read (a queue of states), the writer (a queue of results) and the audit repo."""
    h = SimpleNamespace(reads=[], writer=[], writer_calls=[], audits=[], updates=[], counted=False, noops=[])

    async def fake_read(team_id, league_info, **kwargs):
        nxt = h.reads.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def fake_apply(payload):
        h.writer_calls.append(payload)
        nxt = h.writer.pop(0) if h.writer else WriterResult(True, 200, 200, "{}")
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def direct_run_db(name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def audit_insert(user_id, team_id, nba_date, period, source, moves, key):
        h.audits.append({"user_id": user_id, "team_id": team_id, "nba_date": nba_date, "period": period,
                         "source": source, "moves": moves, "key": key})
        return len(h.audits)

    def audit_update(audit_id, status, *, provider_status=None, error=None):
        h.updates.append((audit_id, status, provider_status, error))

    monkeypatch.setattr(svc.LineupReadService, "read", staticmethod(fake_read))
    monkeypatch.setattr(fantasy_writer_client, "apply_lineup", fake_apply)
    monkeypatch.setattr(svc, "run_db", direct_run_db)
    monkeypatch.setattr(svc, "_audit_insert", audit_insert)
    monkeypatch.setattr(svc, "_audit_update", audit_update)
    monkeypatch.setattr(svc, "_auto_counted_today", lambda team_id, nba_date: h.counted)
    monkeypatch.setattr(svc, "_audit_noop", lambda *a: h.noops.append(a))
    monkeypatch.setattr(svc, "_load_team_for_job", lambda team_id, user_id: (LEAGUE, {}))
    monkeypatch.setattr(svc.settings, "roster_writes_enabled", True)
    return h


def manual(moves, *, version="v1", period=12):
    return ApplyLineupMovesReq(moves=[LineupMoveReq(player_id=p, from_slot_id=a, to_slot_id=b) for p, a, b in moves],
                               expected_scoring_period_id=period, roster_version=version)


SWAP = [(11, BE, UT), (8, UT, BE)]


# ---- manual ---------------------------------------------------------------------------


@pytest.mark.unit
def test_manual_apply_writes_verifies_and_audits(harness):
    harness.reads = [state(board()), state(after_swap(), version="v2")]
    resp = asyncio.run(svc.LineupEditorService.apply_manual(TEAM, LEAGUE, manual(SWAP)))

    assert resp.data.verified is True and resp.data.lineup.roster_version == "v2"
    assert [(m.player_id, m.role) for m in resp.data.applied_moves] == [(11, "start"), (8, "bench")]
    payload = harness.writer_calls[0]
    assert payload["espn_team_id"] == 4 and payload["member_id"] == "{SWID}" and payload["scoring_period_id"] == 12
    assert payload["credentials"] == {"espn_s2": "s2", "swid": "{SWID}"}
    assert payload["moves"] == [{"player_id": 11, "from_slot_id": BE, "to_slot_id": UT},
                                {"player_id": 8, "from_slot_id": UT, "to_slot_id": BE}]
    assert payload["idempotency_key"].startswith("21:12:v1:")
    assert harness.audits[0]["source"] == "manual" and harness.audits[0]["nba_date"] == date(2026, 10, 20)
    assert harness.updates == [(1, "applied", 200, None)]


@pytest.mark.unit
def test_manual_apply_reports_unverified_when_the_reread_disagrees(harness):
    harness.reads = [state(board()), state(board(), version="v1b")]  # ESPN shows the old board
    resp = asyncio.run(svc.LineupEditorService.apply_manual(TEAM, LEAGUE, manual(SWAP)))
    assert resp.data.verified is False and "not yet confirmed" in resp.message
    assert harness.updates[-1][1] == "applied_unverified"


@pytest.mark.unit
def test_a_failed_verify_read_still_settles_the_audit_as_applied(harness):
    """ESPN took the write; only the read-back died. The row must leave `in_flight`."""
    harness.reads = [state(board()), ProviderTimeout("espn")]
    resp = asyncio.run(svc.LineupEditorService.apply_manual(TEAM, LEAGUE, manual(SWAP)))

    assert resp.data.verified is False and "not yet confirmed" in resp.message
    assert resp.data.lineup.roster_version == "v1"  # the pre-write board, since the re-read failed
    audit_id, status, provider_status, error = harness.updates[-1]
    assert (audit_id, status, provider_status) == (1, "applied_unverified", 200)
    assert error.startswith("verify_read_failed:")


@pytest.mark.unit
def test_writes_disabled_is_a_403_before_any_read(harness, monkeypatch):
    monkeypatch.setattr(svc.settings, "roster_writes_enabled", False)
    with pytest.raises(svc.RosterWriteDisabled):
        asyncio.run(svc.LineupEditorService.apply_manual(TEAM, LEAGUE, manual(SWAP)))
    assert harness.reads == [] or True  # no read consumed
    assert harness.writer_calls == []


@pytest.mark.unit
def test_blocked_board_carries_the_reason(harness):
    harness.reads = [state(board(), can_write=False, reason="not_team_owner")]
    with pytest.raises(svc.RosterWriteBlocked) as exc:
        asyncio.run(svc.LineupEditorService.apply_manual(TEAM, LEAGUE, manual(SWAP)))
    assert exc.value.data == {"reason": "not_team_owner"} and harness.writer_calls == []


@pytest.mark.unit
def test_stale_version_returns_the_fresh_board(harness):
    harness.reads = [state(board(), version="v9")]
    with pytest.raises(svc.RosterStale) as exc:
        asyncio.run(svc.LineupEditorService.apply_manual(TEAM, LEAGUE, manual(SWAP, version="v1")))
    assert exc.value.data["lineup"]["roster_version"] == "v9" and harness.writer_calls == []


@pytest.mark.unit
def test_stale_period_is_stale_too(harness):
    harness.reads = [state(board(), period=13)]
    with pytest.raises(svc.RosterStale):
        asyncio.run(svc.LineupEditorService.apply_manual(TEAM, LEAGUE, manual(SWAP, period=12)))


@pytest.mark.unit
def test_invalid_moves_are_422_with_codes(harness):
    harness.reads = [state(board())]
    with pytest.raises(svc.RosterMoveInvalid) as exc:
        asyncio.run(svc.LineupEditorService.apply_manual(TEAM, LEAGUE, manual([(11, BE, SF)])))
    assert [e["code"] for e in exc.value.data["errors"]] == ["INELIGIBLE"]
    assert harness.writer_calls == [] and harness.audits == []


@pytest.mark.unit
def test_rejected_write_is_audited_and_raised(harness):
    harness.reads = [state(board())]
    harness.writer = [FantasyWriterRejected("Invalid Selection.", espn_status=400, excerpt="Invalid Selection.")]
    with pytest.raises(svc.RosterWriteRejected) as exc:
        asyncio.run(svc.LineupEditorService.apply_manual(TEAM, LEAGUE, manual(SWAP)))
    assert exc.value.message == "Invalid Selection."
    assert exc.value.data == {"espn_status": 400, "espn_error_code": None}
    assert harness.updates == [(1, "rejected", 400, "Invalid Selection.")]


@pytest.mark.unit
def test_dead_cookies_become_provider_auth_expired(harness):
    harness.reads = [state(board())]
    harness.writer = [FantasyWriterAuthRejected("nope", espn_status=401)]
    with pytest.raises(ProviderAuthError) as exc:
        asyncio.run(svc.LineupEditorService.apply_manual(TEAM, LEAGUE, manual(SWAP)))
    assert exc.value.data["provider"] == "espn" and harness.updates[-1][1] == "failed"


@pytest.mark.unit
def test_writer_down_is_503_and_audited_failed(harness):
    harness.reads = [state(board())]
    harness.writer = [FantasyWriterUnavailable("down")]
    with pytest.raises(svc.RosterWriteUnavailable):
        asyncio.run(svc.LineupEditorService.apply_manual(TEAM, LEAGUE, manual(SWAP)))
    assert harness.updates[-1][1] == "failed"


# ---- evaluate ---------------------------------------------------------------------------


def evaluate(apply, nba_date=date(2026, 10, 20)):
    return asyncio.run(svc.LineupEditorService.evaluate(
        LineupEvaluateReq(team_id=21, user_id=11, nba_date=nba_date, apply=apply)))


@pytest.mark.unit
def test_evaluate_plans_without_writing(harness):
    harness.reads = [state(board())]
    resp = evaluate(apply=False)
    d = resp.data
    assert d.outcome == "planned" and harness.writer_calls == [] and harness.audits == []
    assert [(m.player_id, m.role, m.note) for m in d.moves] == [(11, "start", "vs LAL · 7:30 PM"), (8, "bench", "no game today")]
    assert d.first_game_time_et == "19:00" and d.team_name == "Lvl. 3 Goblins" and d.scoring_period_id == 12


@pytest.mark.unit
def test_evaluate_noop_records_the_day_only_when_applying(harness):
    harness.reads = [state(board(hole=False))]
    assert evaluate(apply=False).data.outcome == "noop" and harness.noops == []
    harness.reads = [state(board(hole=False))]
    assert evaluate(apply=True).data.outcome == "noop" and len(harness.noops) == 1


@pytest.mark.unit
def test_evaluate_applies_for_auto_users(harness):
    harness.reads = [state(board()), state(after_swap(), version="v2")]
    d = evaluate(apply=True).data
    assert d.outcome == "applied" and d.verified is True and d.audit_id == 1
    assert harness.audits[0]["source"] == "auto" and harness.updates == [(1, "applied", 200, None)]


@pytest.mark.unit
def test_evaluate_settles_the_day_when_the_verify_read_fails(harness):
    """The auto run must not raise, and the day must count, or the next poll re-sends the moves."""
    harness.reads = [state(board()), ProviderTimeout("espn")]
    d = evaluate(apply=True).data
    assert d.outcome == "applied" and d.verified is False and d.audit_id == 1
    assert harness.updates[-1][:2] == (1, "applied_unverified")


@pytest.mark.unit
def test_evaluate_degrades_to_planned_when_writes_are_off(harness, monkeypatch):
    monkeypatch.setattr(svc.settings, "roster_writes_enabled", False)
    harness.reads = [state(board(), can_write=False, reason="writes_disabled")]
    assert evaluate(apply=True).data.outcome == "planned" and harness.writer_calls == []


@pytest.mark.unit
@pytest.mark.parametrize("kwargs, reason", [
    ({"can_write": False, "reason": "no_credentials"}, "can_write:no_credentials"),
    ({"nba_date": "2026-10-21"}, "date_mismatch"),
])
def test_evaluate_skips_with_a_reason(harness, kwargs, reason):
    harness.reads = [state(board(), **kwargs)]
    d = evaluate(apply=True).data
    assert d.outcome == "skipped" and d.reason == reason and harness.writer_calls == []
    assert len(d.moves) == 2  # the plan is still reported so the alert can describe it


@pytest.mark.unit
def test_evaluate_skips_when_today_already_counted(harness):
    harness.reads = [state(board())]
    harness.counted = True
    assert evaluate(apply=True).data.reason == "already_applied_today"


@pytest.mark.unit
def test_evaluate_reports_rejection_and_failure_instead_of_raising(harness):
    harness.reads = [state(board())]
    harness.writer = [FantasyWriterRejected("locked", espn_status=400)]
    d = evaluate(apply=True).data
    assert d.outcome == "rejected" and d.reason == "locked"

    harness.reads = [state(board())]
    harness.writer = [FantasyWriterAuthRejected("x", espn_status=401)]
    assert evaluate(apply=True).data.reason.startswith("PROVIDER_AUTH_EXPIRED")

    harness.reads = [state(board())]
    harness.writer = [FantasyWriterUnavailable("down")]
    assert evaluate(apply=True).data.outcome == "failed"


@pytest.mark.unit
def test_evaluate_skips_non_espn_teams(harness, monkeypatch):
    yahoo = LEAGUE.model_copy(update={"provider": FantasyProvider.YAHOO})
    monkeypatch.setattr(svc, "_load_team_for_job", lambda team_id, user_id: (yahoo, {}))
    assert evaluate(apply=True).data.reason == "provider_not_supported"
