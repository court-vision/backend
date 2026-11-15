"""
The live matchup (GET /v1/internal/matchups/live/{team_id}) on the 15 s beat.

Its answer says how old the newest box score line on the two rosters is
(`live_age_ms`), so the Week desk can read again just after the next write, and
when its provider read was made (`provider_read_ms`). A desk that hands that
stamp back (`reuse_from`) gets fresh box scores over the ESPN read it already
has for a few seconds, never across a move (services/matchup_hold.py).

Nothing here touches the database or a provider: the provider's matchup, the
box score lines and the scoring are handed in.
"""

from __future__ import annotations

import asyncio
from calendar import timegm
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from freezegun import freeze_time

from core.inflight import provider_write
from schemas.common import ApiStatus
from schemas.matchup import MatchupData, MatchupPlayerResp, MatchupResp, MatchupTeamResp
from services import matchup_service
from services.matchup_service import MatchupService
from services.matchup_window import MatchupWindow
from services.scoring.resolver import resolve_scoring

pytestmark = pytest.mark.unit

USER, TEAM = 7, 28
TONIGHT = date(2026, 11, 15)
NOW = datetime(2026, 11, 16, 1, 30, 16)      # 8:30 PM ET, a second after the :15 write
WINDOW = MatchupWindow(include_live=True, overlay_date=TONIGHT, display_date=TONIGHT,
                       seed_zero_baseline=True, stale_days=0, reason="test")
GUARD, WING, CENTER = 101, 102, 103          # ESPN ids


def line(espn_id: int, fpts: int, age: float = 1.0, status: int = 2) -> SimpleNamespace:
    """A player's row of nba.live_player_stats as `_live_stat_snapshot` hands it over."""
    return SimpleNamespace(
        player_id=espn_id - 100, espn_id=espn_id, name_normalized=f"p{espn_id}", game_status=status,
        game_clock="PT04M41.00S", period=2, last_updated=NOW - timedelta(seconds=age), fpts=fpts,
        pts=fpts, reb=0, ast=0, stl=0, blk=0, tov=0, min=12, fgm=0, fga=0, fg3m=0, fg3a=0, ftm=0, fta=0,
    )


class Desk:
    """MatchupService.get_live_matchup_by_team_id over a handed-in provider and box scores."""

    def __init__(self, monkeypatch):
        self.provider_reads = 0
        self.provider_status = ApiStatus.SUCCESS
        self.slots = {GUARD: "PG", WING: "BE"}                 # your lineup, as ESPN has it
        self.lines: dict[int, SimpleNamespace] = {}

        async def provider(user_id, team_id, avg_window="season"):
            assert (user_id, team_id) == (USER, TEAM)
            self.provider_reads += 1
            if self.provider_status != ApiStatus.SUCCESS:
                return MatchupResp(status=self.provider_status, message="ESPN is down", data=None)
            return MatchupResp(status=ApiStatus.SUCCESS, message="ok", data=self._matchup())

        async def run_db(name, fn, *args):
            if fn.__name__ == "_baseline_snapshot":
                return None
            if fn.__name__ == "_live_stat_maps":
                return dict(self.lines), {}
            assert fn.__name__ == "resolve_scoring_for_team"
            return resolve_scoring(None)

        monkeypatch.setattr(MatchupService, "get_matchup_by_team_id", staticmethod(provider))
        monkeypatch.setattr(matchup_service, "run_db", run_db)
        monkeypatch.setattr(MatchupService, "_watermark_window", staticmethod(lambda md, baseline: WINDOW))
        monkeypatch.setattr(MatchupService, "_legacy_window", staticmethod(lambda md, baseline: WINDOW))

    def _matchup(self) -> MatchupData:
        def player(espn_id: int, slot: str) -> MatchupPlayerResp:
            return MatchupPlayerResp(player_id=espn_id, name=f"P{espn_id}", team="BOS", position="PG", lineup_slot=slot,
                                     avg_points=20.0, projected_points=0.0, games_remaining=1, injured=False)

        return MatchupData(
            matchup_period=4, matchup_period_start="2026-11-09", matchup_period_end="2026-11-15",
            your_team=MatchupTeamResp(team_name="You", team_id=1, current_score=0, projected_score=0,
                                      roster=[player(espn_id, slot) for espn_id, slot in self.slots.items()]),
            opponent_team=MatchupTeamResp(team_name="Opp", team_id=5, current_score=0, projected_score=0,
                                          roster=[player(CENTER, "C")]),
            projected_winner="You", projected_margin=0,
        )

    def read(self, reuse_from: int | None = None, at: datetime = NOW):
        with freeze_time(at):
            return asyncio.run(MatchupService.get_live_matchup_by_team_id(USER, TEAM, reuse_from))

    def data(self, reuse_from: int | None = None, at: datetime = NOW):
        resp = self.read(reuse_from, at)
        assert resp.status == ApiStatus.SUCCESS
        return resp.data


@pytest.fixture
def desk(monkeypatch) -> Desk:
    return Desk(monkeypatch)


def test_the_answer_says_how_old_its_newest_line_is(desk) -> None:
    desk.lines = {GUARD: line(GUARD, 18, age=1.2), CENTER: line(CENTER, 9, age=1.6)}
    assert desk.data().live_age_ms == 1200


def test_final_lines_no_longer_written_still_count_and_show_their_age(desk) -> None:
    """A settled game's rows stop moving: the desk reads the age as a pipeline gone quiet."""
    desk.lines = {GUARD: line(GUARD, 31, age=3600, status=3)}
    assert desk.data().live_age_ms == 3_600_000


def test_with_no_line_on_either_roster_there_is_no_age(desk) -> None:
    assert desk.data().live_age_ms is None


def test_a_first_read_reaches_the_provider_and_is_stamped(desk) -> None:
    data = desk.data()
    assert desk.provider_reads == 1
    assert data.provider_read_ms == timegm(NOW.timetuple()) * 1000


def test_a_desk_that_hands_its_stamp_back_gets_new_box_scores_over_the_same_provider_read(desk) -> None:
    desk.lines = {GUARD: line(GUARD, 18)}
    first = desk.data()
    assert first.your_team.current_score == 18.0

    desk.lines = {GUARD: line(GUARD, 21, age=-14)}             # the next write, 15 s on
    again = desk.data(first.provider_read_ms, at=NOW + timedelta(seconds=15))
    assert desk.provider_reads == 1
    assert again.your_team.current_score == 21.0
    assert again.live_age_ms == 1000
    assert again.provider_read_ms == first.provider_read_ms


def test_without_the_stamp_every_read_reaches_the_provider(desk) -> None:
    """What the desk sends after a move: its lineup must come from ESPN again."""
    desk.data()
    desk.slots = {GUARD: "BE", WING: "PG"}
    desk.lines = {GUARD: line(GUARD, 18), WING: line(WING, 6)}
    moved = desk.data()
    assert desk.provider_reads == 2
    assert moved.your_team.current_score == 6.0
    assert {p.player_id: p.lineup_slot for p in moved.your_team.roster} == {GUARD: "BE", WING: "PG"}


def test_a_move_sent_from_here_is_never_answered_from_a_read_before_it(desk) -> None:
    """Dime or the auto-lineup moved a player: the next poll, stamp and all, reads ESPN."""
    first = desk.data()
    desk.slots = {GUARD: "BE", WING: "PG"}
    with provider_write():
        pass
    after = desk.data(first.provider_read_ms)
    assert desk.provider_reads == 2
    assert {p.player_id: p.lineup_slot for p in after.your_team.roster} == {GUARD: "BE", WING: "PG"}
    assert after.provider_read_ms >= first.provider_read_ms


def test_a_provider_read_that_failed_is_not_kept(desk) -> None:
    first = desk.data()
    with provider_write():
        pass
    desk.provider_status = ApiStatus.ERROR
    assert desk.read(first.provider_read_ms).data is None
    desk.provider_status = ApiStatus.SUCCESS
    assert desk.data(first.provider_read_ms).your_team.team_name == "You"
    assert desk.provider_reads == 3
