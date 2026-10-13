"""
LineupEditorService.apply_manual over the real ESPN reader (EspnAdapter ->
LineupReadService.read): how many ESPN reads and valuations each write path
costs, and that every board handed back to the client is exactly what a full
read of that board returns, as it was when each read carried values.

ESPN is faked at the league endpoint (EspnService.fetch_league), following the
2026-10-05 probe: `scoringPeriodId=N` comes back as the top-level
`scoringPeriodId`, `status.latestScoringPeriod` stays on today, any N answers
200. On 2026-10-13 the plain read of a public league and its read of today by
number were byte-identical, so the fake answers both from today's roster. The
writer stand-in applies the moves to the day written, so the re-read sees them.

Before this change a later day's write cost 3 ESPN reads and 3 valuations
(today's board, that day's, the re-read), and today's write 2 and 2. The
valuation is the expensive half for a category league: it re-scores the whole
player pool (~250-400 ms per pass, docs/PERF_AUDIT_2026-10.md).
"""

import asyncio
from datetime import date, time, timedelta
from types import SimpleNamespace

import pytest
from freezegun import freeze_time
from structlog.testing import capture_logs

from core.errors import BadRequestError, ProviderTimeout
from schemas.common import FantasyProvider, LeagueInfo
from schemas.lineup_editor import ApplyLineupMovesReq, LineupMoveReq
from services import fantasy_writer_client, lineup_editor_service as svc, lineup_read_service as reader
from services.fantasy_writer_client import FantasyWriterRejected, WriterResult
from services.player_value_service import ValueResult
from services.providers.adapters import EspnAdapter
from utils.espn_helpers import PRO_TEAM_MAP, TEAM_ABBREV_CORRECTIONS

SWID = "{ABCDEF01-2345-6789-ABCD-EF0123456789}"
LEAGUE = LeagueInfo(provider=FantasyProvider.ESPN, league_id=1, team_name="Lvl. 3 Goblins", year=2027,
                    espn_s2="x", swid=SWID, espn_team_id=4)
TEAM = SimpleNamespace(team_id=21, user_id=11, league_id=None, league=None)
OPENING = date(2026, 10, 20)
TODAY, LATER, FINAL = 12, 15, 167                    # day 12 is 2026-10-31
NOON_ET = "2026-10-31T16:00:00Z"                     # tonight's 7:30 PM games have not tipped
DEN = TEAM_ABBREV_CORRECTIONS.get(PRO_TEAM_MAP[7], PRO_TEAM_MAP[7])

PG, G, UT, BE = 0, 5, 11, 12
ELIGIBLE = {8: [PG, UT, BE], 11: [PG, G, UT, BE], 20: [PG, UT, BE]}
START = {8: UT, 11: BE, 20: BE}                      # player 20 has no stored value
SWAP = [(11, BE, UT), (8, UT, BE)]


def _day(period):
    return OPENING + timedelta(days=period - 1)


def _entry(pid, slot):
    return {"playerId": pid, "lineupSlotId": slot, "injuryStatus": "ACTIVE",
            "playerPoolEntry": {"id": pid, "lineupLocked": False, "onTeamId": 4,
                                "player": {"id": pid, "fullName": f"Player {pid}", "proTeamId": 7,
                                           "eligibleSlots": ELIGIBLE[pid], "injured": False,
                                           "injuryStatus": "ACTIVE", "defaultPositionId": 1}}}


class FakeEspn:
    def __init__(self):
        self.calls = []               # the scoringPeriodId of each league read; None = the plain read
        self.days = {}                # day -> {player_id: slot} for days written in the test
        self.latest = TODAY           # status.latestScoringPeriod; None = ESPN left it out
        self.fail_on_call = None      # the n-th read (1-based) times out

    def roster(self, day):
        return self.days.get(day, START)

    async def fetch(self, league_info, views, *, expect_key="teams", scoring_period_id=None):
        self.calls.append(scoring_period_id)
        if self.fail_on_call == len(self.calls):
            raise ProviderTimeout("espn")
        day = scoring_period_id or TODAY
        status = {"firstScoringPeriod": 1, "finalScoringPeriod": FINAL}
        if self.latest is not None:
            status["latestScoringPeriod"] = self.latest
        return {
            "scoringPeriodId": day,
            "status": status,
            "teams": [{"id": 4, "name": "Lvl. 3 Goblins", "owners": [SWID],
                       "roster": {"entries": [_entry(pid, slot) for pid, slot in self.roster(day).items()]}}],
            "settings": {"rosterSettings": {"lineupSlotCounts": {"0": 1, "11": 1, "12": 2},
                                            "lineupLocktimeType": "INDIVIDUAL_GAME"}},
        }


@pytest.fixture
def world(monkeypatch):
    espn = FakeEspn()
    w = SimpleNamespace(espn=espn, valuations=[], writes=[], writer=[], audits=[], updates=[])

    def values_for(league_info, espn_ids):
        w.valuations.append(list(espn_ids))
        return ("cat_value", {8: ValueResult(1.75, "rolling"), 11: ValueResult(0.5, "baseline")},
                {8: 203999, 11: 1629029})

    async def write(payload):
        w.writes.append(payload)
        if w.writer:
            raise w.writer.pop(0)
        day = payload["scoring_period_id"]
        roster = dict(espn.roster(day))
        for m in payload["moves"]:
            roster[m["player_id"]] = m["to_slot_id"]
        espn.days[day] = roster
        return WriterResult(True, 200, 200, "{}")

    async def direct_run_db(name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def audit_insert(user_id, team_id, nba_date, period, source, moves, key):
        w.audits.append((nba_date, period, key))
        return len(w.audits)

    game = SimpleNamespace(home_team_id=DEN, away_team_id="LAL", start_time_et=time(19, 30))
    monkeypatch.setattr(reader.EspnService, "fetch_league", staticmethod(espn.fetch))
    monkeypatch.setattr(reader, "_values_for", values_for)
    monkeypatch.setattr(reader, "_games_on", lambda nba_date: [game])
    monkeypatch.setattr(reader, "run_db", direct_run_db)
    monkeypatch.setattr(reader.schedule_service, "get_nba_today", lambda: _day(TODAY))
    monkeypatch.setattr(reader.schedule_service, "season_day", lambda d=None, season=None: (d - OPENING).days + 1)
    monkeypatch.setattr(reader.schedule_service, "date_for_espn_scoring_period", lambda p, season=None: _day(p))
    monkeypatch.setattr(fantasy_writer_client, "apply_lineup", write)
    monkeypatch.setattr(svc, "run_db", direct_run_db)
    monkeypatch.setattr(svc, "_audit_insert", audit_insert)
    monkeypatch.setattr(svc, "_audit_update", lambda audit_id, status, **kw: w.updates.append((audit_id, status)))
    monkeypatch.setattr(svc.settings, "roster_writes_enabled", True)
    with freeze_time(NOON_ET):
        yield w


def reference(w, period=None):
    """What a full read of that board returns (each read used to carry values), read off the books."""
    calls, valuations = list(w.espn.calls), list(w.valuations)
    board = asyncio.run(EspnAdapter().read_lineup(TEAM.team_id, LEAGUE, fallback_slot_counts={},
                                                  scoring_period_id=period))
    w.espn.calls[:], w.valuations[:] = calls, valuations
    return board.model_dump(mode="json")


def apply(period, version, moves=SWAP):
    req = ApplyLineupMovesReq(moves=[LineupMoveReq(player_id=p, from_slot_id=a, to_slot_id=b) for p, a, b in moves],
                              expected_scoring_period_id=period, roster_version=version)
    return asyncio.run(svc.LineupEditorService.apply_manual(TEAM, LEAGUE, req))


def written_days(logs, event="lineup_write_applied"):
    """(scoring_period_id, current_scoring_period_id) on each write's log line."""
    return [(e["scoring_period_id"], e["current_scoring_period_id"]) for e in logs if e["event"] == event]


# ---- writes that go through ------------------------------------------------------------


@pytest.mark.unit
def test_todays_write_reads_espn_twice_and_values_only_the_board_it_returns(world):
    with capture_logs() as logs:
        resp = apply(TODAY, reference(world)["roster_version"])

    assert world.espn.calls == [TODAY, None]          # today by number, then the plain re-read
    assert len(world.valuations) == 1                 # the re-read's: it is the board handed back
    assert "current_scoring_period_id" not in world.writes[0]          # ROSTER, as before
    assert world.audits[0][:2] == (date(2026, 10, 31), TODAY)
    assert resp.data.verified is True and world.updates == [(1, "applied")]
    assert resp.data.lineup.model_dump(mode="json") == reference(world)
    assert resp.data.lineup.players[0].value_kind == "cat_value"
    assert written_days(logs) == [(TODAY, TODAY)]     # the log line shows it went out as today's


@pytest.mark.unit
def test_a_later_days_write_never_reads_today(world):
    with capture_logs() as logs:
        resp = apply(LATER, reference(world, LATER)["roster_version"])

    assert world.espn.calls == [LATER, LATER]         # that day, then its re-read
    assert len(world.valuations) == 1
    payload = world.writes[0]
    assert (payload["scoring_period_id"], payload["current_scoring_period_id"]) == (LATER, TODAY)   # FUTURE_ROSTER
    assert payload["idempotency_key"].startswith(f"21:{LATER}:")
    assert world.audits[0][:2] == (date(2026, 11, 3), LATER)
    assert resp.data.verified is True
    assert resp.data.lineup.model_dump(mode="json") == reference(world, LATER)
    assert world.espn.roster(TODAY) == START          # today's board was never written
    assert written_days(logs) == [(LATER, TODAY)]


@pytest.mark.unit
def test_a_failed_reread_hands_back_the_pre_write_board_with_its_values(world):
    before = reference(world)
    world.espn.fail_on_call = 2
    with capture_logs() as logs:
        resp = apply(TODAY, before["roster_version"])

    assert world.espn.calls == [TODAY, None] and len(world.valuations) == 1
    assert resp.data.verified is False and world.updates == [(1, "applied_unverified")]
    assert resp.data.lineup.model_dump(mode="json") == before
    assert written_days(logs, "lineup_write_unverified") == [(TODAY, TODAY)]


# ---- writes that stop before ESPN ------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("period, board_day", [(TODAY, None), (LATER, LATER)], ids=["today", "later-day"])
def test_a_stale_version_is_409_with_the_same_board_as_before(world, period, board_day):
    fresh = reference(world, board_day)
    with pytest.raises(svc.RosterStale) as exc:
        apply(period, "an-old-version")

    assert exc.value.status_code == 409 and exc.value.data["lineup"] == fresh
    assert world.espn.calls == [period] and len(world.valuations) == 1
    assert world.writes == [] and world.audits == []


@pytest.mark.unit
def test_a_day_that_rolled_over_is_409_with_todays_board(world):
    fresh = reference(world)
    with pytest.raises(svc.RosterStale) as exc:
        apply(TODAY - 1, reference(world)["roster_version"])

    assert exc.value.data["lineup"] == fresh and exc.value.data["lineup"]["scoring_period_id"] == TODAY
    assert world.espn.calls == [TODAY - 1, None]      # refused, then today's board
    assert len(world.valuations) == 1 and world.writes == []


@pytest.mark.unit
def test_a_day_past_the_season_keeps_its_400(world):
    with pytest.raises(BadRequestError) as exc:
        apply(FINAL + 1, "v")

    assert exc.value.error_code == "SCORING_PERIOD_OUT_OF_RANGE" and "last day" in exc.value.message
    assert world.espn.calls == [FINAL + 1, None] and world.valuations == []
    assert world.writes == [] and world.audits == []


@pytest.mark.unit
def test_refused_moves_and_rejected_writes_value_nothing(world):
    version = reference(world)["roster_version"]
    with pytest.raises(svc.RosterMoveInvalid):
        apply(TODAY, version, moves=[(8, UT, G)])     # not eligible at G
    assert world.espn.calls == [TODAY] and world.valuations == [] and world.writes == []

    world.espn.calls.clear()
    world.writer = [FantasyWriterRejected("Invalid Selection.", espn_status=400)]
    with pytest.raises(svc.RosterWriteRejected):
        apply(TODAY, version)
    assert world.espn.calls == [TODAY] and world.valuations == []
    assert world.updates == [(1, "rejected")]


# ---- when ESPN leaves out its today ------------------------------------------------------


@pytest.mark.unit
def test_without_espns_today_a_lagging_calendar_cannot_make_today_a_future_write(world, monkeypatch):
    # The read of day 12 alone would place it against our calendar's day 11 and call it a
    # later day (FUTURE_ROSTER). ESPN's plain read says 12 is today, so today's board is written.
    world.espn.latest = None
    monkeypatch.setattr(reader.schedule_service, "get_nba_today", lambda: _day(TODAY - 1))
    resp = apply(TODAY, reference(world)["roster_version"])

    assert world.espn.calls == [TODAY, None, None]    # the day, today's plain board, the re-read
    assert "current_scoring_period_id" not in world.writes[0]
    assert resp.data.verified is True and len(world.valuations) == 1


@pytest.mark.unit
def test_without_espns_today_a_later_day_is_read_once(world):
    world.espn.latest = None
    apply(LATER, reference(world, LATER)["roster_version"])

    assert world.espn.calls == [LATER, None, LATER]   # the day, today's plain board, the re-read
    assert (world.writes[0]["scoring_period_id"], world.writes[0]["current_scoring_period_id"]) == (LATER, TODAY)


# ---- when ESPN's two todays disagree ------------------------------------------------------
# The client's today board comes from the plain read, so its day is the plain read's top-level
# scoringPeriodId; the write now places that day against status.latestScoringPeriod instead.
# The two have only been seen equal (preseason, 2026-10-13); docs PENDING_PROD_CHECKS.md item 6
# compares them on opening night and across the 2 AM ET rollover.


@pytest.mark.unit
def test_a_plain_day_behind_espns_today_writes_todays_plain_board_as_before(world):
    world.espn.latest = TODAY + 1
    loaded = reference(world)                         # the board the client loaded as today
    assert (loaded["scoring_period_id"], loaded["current_scoring_period_id"]) == (TODAY, TODAY)
    with capture_logs() as logs:
        resp = apply(TODAY, loaded["roster_version"])

    assert world.espn.calls == [TODAY, None, None]    # refused as passed, today's plain board, the re-read
    assert "current_scoring_period_id" not in world.writes[0]          # ROSTER, as the old path sent it
    assert world.audits[0][:2] == (date(2026, 10, 31), TODAY)
    assert resp.data.verified is True and len(world.valuations) == 1
    assert resp.data.lineup.model_dump(mode="json") == reference(world)
    assert written_days(logs) == [(TODAY, TODAY)]


@pytest.mark.unit
def test_a_plain_day_ahead_of_espns_today_would_send_todays_edit_as_a_later_day(world):
    # The one case where this write differs from the old path, which read the plain board and
    # sent ROSTER for its day. Pinned so the difference is a decision: if opening night ever
    # shows the plain day ahead of latestScoringPeriod, this is the test that changes.
    world.espn.latest = TODAY - 1
    loaded = reference(world)
    assert (loaded["scoring_period_id"], loaded["current_scoring_period_id"]) == (TODAY, TODAY)
    with capture_logs() as logs:
        resp = apply(TODAY, loaded["roster_version"])

    assert world.espn.calls == [TODAY, TODAY]         # the day by number is ESPN's tomorrow, re-read the same way
    payload = world.writes[0]
    assert (payload["scoring_period_id"], payload["current_scoring_period_id"]) == (TODAY, TODAY - 1)
    assert resp.data.verified is True
    assert written_days(logs) == [(TODAY, TODAY - 1)]  # and the log line says so
