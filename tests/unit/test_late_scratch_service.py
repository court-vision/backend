"""
services.late_scratch_service without ESPN, the writer or Postgres: the board read
is a queue of prebuilt LineupStates, the writer a queue of results, and the team
list, verdicts, today's swaps and the audit row are in-memory fakes.

A small league (PG, UT, two bench): Brunson starts at PG, the UT is the player whose
verdict changes, one bench guard has a game and one doesn't.
"""

import asyncio
from collections import defaultdict
from datetime import date
from types import SimpleNamespace

import pytest

from schemas.common import FantasyProvider, LeagueInfo
from schemas.lineup_editor import LineupPlayer, LineupReactReq, LineupState
from services import fantasy_writer_client, late_scratch_service as late, lineup_editor_service as svc
from services.fantasy_writer_client import FantasyWriterRejected, WriterResult

PG, UT, BE = 0, 11, 12
COUNTS = {"0": 1, "11": 1, "12": 2}
LEAGUE = LeagueInfo(provider=FantasyProvider.ESPN, league_id=552315826, team_name="Test", year=2027,
                    espn_s2="s2", swid="{SWID}", espn_team_id=3)
DAY = date(2026, 11, 10)
UT_STARTER, BENCH_GUARD, IDLE = 102, 103, 104   # NBA ids; ESPN ids are these minus 100


def player(nba_id, slot, *, game=True, status=None, locked=False, value=10.0):
    return LineupPlayer(
        player_id=nba_id - 100, nba_player_id=nba_id, name=f"P{nba_id}", team="NYK", lineup_slot_id=slot,
        lineup_slot=str(slot), eligible_slot_ids=[PG, UT, BE], eligible_slots=["PG", "UT", "BE"],
        injured=status == "OUT", injury_status=status, lineup_locked=locked, has_game_today=game,
        opponent="vs BOS" if game else None, game_time_et="19:30" if game else None, locked=locked,
        playable=game and status != "OUT", avg_points=value,
    )


def board(*, ut=None, bench=None, nba_date="2026-11-10", can_write=True, version="v1"):
    players = [player(101, PG), ut or player(UT_STARTER, UT), bench or player(BENCH_GUARD, BE),
               player(IDLE, BE, game=False)]
    return LineupState(
        provider=FantasyProvider.ESPN, team_name="Test", espn_team_id=3, nba_date=nba_date, scoring_period_id=24,
        scoring_period_source="provider", first_game_time_et="19:30", slot_counts=COUNTS, players=players,
        can_write=can_write, roster_version=version, fetched_at="now",
    )


def swapped():
    return board(ut=player(BENCH_GUARD, UT), bench=player(UT_STARTER, BE), version="v2")


def change(verdict="out", pid=UT_STARTER, previous="questionable"):
    return LineupReactReq(player_id=pid, game_date=DAY, verdict=verdict, previous_verdict=previous,
                          reason="Left Ankle; Sprain", source="espn_injuries")


@pytest.fixture
def h(monkeypatch):
    h = SimpleNamespace(teams=[(22, 7, "act")], reads=[], writer=[], writer_calls=[], audits=[], updates=[],
                        verdicts={}, swaps=(0, set()))

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

    def audit_insert(user_id, team_id, nba_date, period, source, moves, key, **kw):
        h.audits.append({"team_id": team_id, "source": source, "moves": moves, **kw})
        return len(h.audits)

    monkeypatch.setattr(svc.LineupReadService, "read", staticmethod(fake_read))
    monkeypatch.setattr(fantasy_writer_client, "apply_lineup", fake_apply)
    monkeypatch.setattr(svc, "run_db", direct_run_db)
    monkeypatch.setattr(late, "run_db", direct_run_db)
    monkeypatch.setattr(svc, "_audit_insert", audit_insert)
    monkeypatch.setattr(svc, "_audit_update", lambda audit_id, status, **kw: h.updates.append((audit_id, status)))
    monkeypatch.setattr(late, "_load_team_for_job", lambda team_id, user_id: (LEAGUE, {}))
    monkeypatch.setattr(late, "_teams", lambda: list(h.teams))
    monkeypatch.setattr(late, "_verdicts", lambda ids, day: dict(h.verdicts))
    monkeypatch.setattr(late, "_swaps_today", lambda team_id, day: h.swaps)
    monkeypatch.setattr(late.settings, "roster_writes_enabled", True)
    monkeypatch.setattr(late.settings, "late_scratch_swaps_per_day", 3)
    # Each test runs its own event loop; a lock waited on in one can't be waited on in the next.
    monkeypatch.setattr(late, "_team_locks", defaultdict(asyncio.Lock))
    return h


def react(req):
    return asyncio.run(late.LateScratchService.react(req)).data


def moves(team):
    return [(m.player_id, m.from_slot_id, m.to_slot_id, m.role) for m in team.moves]


@pytest.mark.unit
def test_a_starter_ruled_out_sits_for_the_healthy_bench_guard(h):
    h.reads = [board(), swapped()]
    team = react(change()).teams[0]

    assert (team.team_id, team.user_id, team.level, team.outcome, team.verified) == (22, 7, "act", "applied", True)
    assert team.player_name == "P102"
    assert moves(team) == [(3, BE, UT, "start"), (2, UT, BE, "bench")]
    assert team.moves[1].note == "OUT"
    assert h.audits == [{"team_id": 22, "source": "availability", "trigger_player_id": UT_STARTER,
                         "moves": [{"player_id": 3, "from_slot_id": BE, "to_slot_id": UT, "role": "start",
                                    "note": "vs BOS · 7:30 PM"},
                                   {"player_id": 2, "from_slot_id": UT, "to_slot_id": BE, "role": "bench",
                                    "note": "OUT"}]}]
    assert h.updates == [(1, "applied")]


@pytest.mark.unit
def test_doubtful_is_a_swap_too_but_doubtful_after_out_is_not(h):
    h.reads = [board(), swapped()]
    assert react(change("doubtful")).teams[0].outcome == "applied"

    h.reads = [board()]
    team = react(change("doubtful", previous="out")).teams[0]
    assert (team.outcome, team.reason) == ("noop", "no_better_bench_player")
    assert h.writer_calls[1:] == []


@pytest.mark.unit
def test_a_bench_player_now_available_starts_over_one_ruled_out(h):
    h.reads = [board(ut=player(UT_STARTER, UT, status="OUT"), bench=player(BENCH_GUARD, BE, status="OUT")),
               swapped()]
    h.verdicts = {UT_STARTER: ("out", "questionable")}
    team = react(change("available", pid=BENCH_GUARD, previous="out")).teams[0]

    assert team.outcome == "applied"
    assert moves(team) == [(3, BE, UT, "start"), (2, UT, BE, "bench")]
    assert h.audits[0]["trigger_player_id"] == BENCH_GUARD


@pytest.mark.unit
def test_our_verdicts_on_the_rest_of_the_board_count(h):
    """ESPN still lists the bench guard healthy, but our verdict has him out: nobody can take the slot."""
    h.reads = [board()]
    h.verdicts = {BENCH_GUARD: ("out", "available")}
    assert react(change()).teams[0].outcome == "noop"


@pytest.mark.unit
@pytest.mark.parametrize("read,req,reason", [
    (board(nba_date="2026-11-11"), change(), "not_today"),
    (board(), change(pid=999), "not_rostered"),
    (board(ut=player(UT_STARTER, UT, locked=True)), change(), "locked"),
    (board(ut=player(UT_STARTER, UT, game=False)), change(), "no_game"),
    (board(), change("available"), "not_on_bench"),
    (board(), change(pid=BENCH_GUARD), "not_starting"),
])
def test_skips_with_a_reason(h, read, req, reason):
    h.reads = [read]
    team = react(req).teams[0]
    assert (team.outcome, team.reason) == ("skipped", reason)
    assert h.writer_calls == [] and h.audits == []


@pytest.mark.unit
def test_with_writes_off_or_an_unwritable_board_it_is_a_plan(h, monkeypatch):
    monkeypatch.setattr(late.settings, "roster_writes_enabled", False)
    h.reads = [board()]
    team = react(change()).teams[0]
    assert (team.outcome, team.reason) == ("planned", "writes_disabled")
    assert moves(team) == [(3, BE, UT, "start"), (2, UT, BE, "bench")]

    monkeypatch.setattr(late.settings, "roster_writes_enabled", True)
    h.reads = [board(can_write=False)]
    assert react(change()).teams[0].outcome == "planned"
    assert h.writer_calls == []


@pytest.mark.unit
def test_once_per_player_and_at_most_the_daily_cap(h):
    h.reads = [board()]
    h.swaps = (1, {UT_STARTER})
    assert react(change()).teams[0].reason == "already_swapped"

    h.reads = [board()]
    h.swaps = (3, {1, 2, 3})
    assert react(change()).teams[0].reason == "daily_cap"
    assert h.writer_calls == []


@pytest.mark.unit
def test_a_rejected_write_or_a_failed_read_is_reported_not_raised(h):
    h.teams = [(22, 7, "act"), (23, 7, "act")]
    h.reads = [board(), RuntimeError("espn down")]
    h.writer = [FantasyWriterRejected("ESPN says the roster is locked", espn_status=409)]
    first, second = react(change()).teams

    assert (first.outcome, first.reason) == ("rejected", "ESPN says the roster is locked")
    assert (second.team_id, second.outcome, second.reason) == (23, "failed", "read_failed")


@pytest.mark.unit
def test_no_team_at_notify_or_act_is_no_team_asked(h):
    h.teams = []
    data = react(change())
    assert data.teams == [] and (data.player_id, data.verdict) == (UT_STARTER, "out")


@pytest.mark.unit
def test_at_notify_the_swap_is_a_plan_for_the_email(h):
    h.teams = [(22, 7, "notify")]
    h.reads = [board()]
    team = react(change()).teams[0]
    assert (team.level, team.outcome, team.reason) == ("notify", "planned", "notify")
    assert moves(team) == [(3, BE, UT, "start"), (2, UT, BE, "bench")]
    assert h.writer_calls == [] and h.audits == []


@pytest.mark.unit
def test_an_error_in_one_teams_pass_is_that_teams_failure(h, monkeypatch):
    h.teams = [(22, 7, "act"), (23, 8, "act")]
    h.reads = [board(), board(), swapped()]
    calls = []

    def verdicts(ids, day):
        calls.append(day)
        if len(calls) == 1:
            raise RuntimeError("database capacity")
        return {}

    monkeypatch.setattr(late, "_verdicts", verdicts)
    first, second = react(change()).teams

    assert (first.team_id, first.user_id, first.outcome, first.reason) == (22, 7, "failed", "unexpected_error")
    assert (second.team_id, second.outcome) == (23, "applied")


@pytest.mark.unit
def test_a_write_that_raises_after_it_was_sent_is_uncertain(h, monkeypatch):
    h.reads = [board(), swapped()]

    def audit_update(audit_id, status, **kw):
        raise RuntimeError("duplicate key value violates unique constraint")

    monkeypatch.setattr(svc, "_audit_update", audit_update)
    team = react(change()).teams[0]

    assert (team.outcome, team.reason) == ("failed", "write_uncertain")
    assert moves(team) == [(3, BE, UT, "start"), (2, UT, BE, "bench")]
    assert len(h.writer_calls) == 1


@pytest.mark.unit
def test_two_changes_answered_at_once_keep_the_daily_cap(h, monkeypatch):
    monkeypatch.setattr(late.settings, "late_scratch_swaps_per_day", 1)
    monkeypatch.setattr(late, "_swaps_today",
                        lambda team_id, day: (sum(1 for _, status in h.updates if status == "applied"), set()))
    h.reads = [board(), board(), swapped()]

    async def slow_apply(payload):
        h.writer_calls.append(payload)
        await asyncio.sleep(0)  # the second change reads the count while the first is at ESPN
        return WriterResult(True, 200, 200, "{}")

    monkeypatch.setattr(fantasy_writer_client, "apply_lineup", slow_apply)

    async def both():
        return await asyncio.gather(late.LateScratchService.react(change()), late.LateScratchService.react(change()))

    first, second = (r.data.teams[0] for r in asyncio.run(both()))
    assert first.outcome == "applied"
    assert (second.outcome, second.reason) == ("skipped", "daily_cap")
    assert len(h.writer_calls) == 1
