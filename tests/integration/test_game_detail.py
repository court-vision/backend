"""One game's sheet after the fact (GET /v1/games/{date}/{game_id}), against the real migrations."""

import asyncio
from datetime import date, datetime, timedelta

import pytest

from core.settings import settings
from db.models.nba.games import Game
from db.models.nba.live_game_score_snapshots import LiveGameScoreSnapshot
from db.models.nba.live_player_stats import LivePlayerStats
from db.models.nba.player_game_stats import PlayerGameStats
from db.models.nba.player_season_stats import PlayerSeasonStats
from db.models.nba.players import Player
from db.models.nba.teams import NBATeam
from schemas.common import ApiStatus
from services import game_detail_service
from services.game_detail_service import GameDetailService

pytestmark = pytest.mark.integration

NIGHT = date(2026, 10, 20)
GAME = "0022600001"
TIP = datetime(2026, 10, 20, 19, 10)
STATS = dict(pts=0, reb=0, ast=0, stl=0, blk=0, tov=0, fgm=0, fga=0, fg3m=0, fg3a=0, ftm=0, fta=0)

# The league feed's box score for the game: the period line, and each side's players.
BOX = {
    "gameStatus": 3, "period": 4, "gameClock": "",
    "homeTeam": {"teamTricode": "DET", "score": 121, "players": [{"personId": 3}],
                 "periods": [{"period": p, "score": s} for p, s in enumerate((24, 21, 32, 44), 1)]},
    "awayTeam": {"teamTricode": "BOS", "score": 113, "players": [{"personId": 1}],
                 "periods": [{"period": p, "score": s} for p, s in enumerate((23, 35, 23, 32), 1)]},
}


@pytest.fixture(autouse=True)
def night(clean_tables, monkeypatch):
    monkeypatch.setattr(settings, "nba_season", "2026-27")
    monkeypatch.setattr(game_detail_service, "_get_nba_today", lambda: NIGHT + timedelta(days=1))
    for team in ("DET", "BOS"):
        NBATeam.get_or_create(id=team, defaults={"name": team, "city": team, "conference": "East", "division": "Central"})
    # nba.games before the post-game run: still scheduled, no score.
    Game.create(game_id=GAME, game_date=NIGHT, season="2026-27", home_team="DET", away_team="BOS", status="scheduled")
    for pid, name in ((1, "Paul George"), (2, "Jaylen Brown"), (3, "A Rookie")):
        Player.create(id=pid, espn_id=9000 + pid, name=name, name_normalized=name.lower(), position="F")


def box_score(monkeypatch, box=BOX):
    calls = []

    async def fake(provider, operation, fn, *args):
        calls.append(args)
        if isinstance(box, Exception):
            raise box
        return box

    monkeypatch.setattr(game_detail_service, "run_blocking_provider", fake)
    return calls


def get(day=NIGHT, game_id=GAME):
    return asyncio.run(GameDetailService.get_game(day, game_id))


def settled_line(pid, team, fpts, pts, **extra):
    return PlayerGameStats.create(player=pid, team=team, game=GAME, game_date=NIGHT,
                                  **{**STATS, "pts": pts, **extra}, fpts=fpts, min=30)


def live_line(pid, fpts, pts):
    return LivePlayerStats.create(player=pid, game_id=GAME, game_date=NIGHT, game_status=3, period=4,
                                  **{**STATS, "pts": pts}, fpts=fpts, min=0)


def snap(minute, home, away, status=2, period=1, clock="PT07M20.00S"):
    LiveGameScoreSnapshot.create(game_id=GAME, game_date=NIGHT, home_team="DET", away_team="BOS",
                                 home_score=home, away_score=away, period=period, game_clock=clock,
                                 game_status=status, captured_at=TIP + timedelta(minutes=minute))


def test_a_past_game_is_the_settled_game_log_with_the_box_scores_period_line(monkeypatch):
    box_score(monkeypatch)
    settled_line(1, "BOS", fpts=72, pts=38, plus_minus=-4)
    settled_line(3, "DET", fpts=40, pts=20)
    settled_line(2, "BOS", fpts=40, pts=25)
    live_line(1, fpts=1, pts=1)   # last night's live read: not used once the game log has landed

    resp = get()

    assert resp.status == ApiStatus.SUCCESS
    data = resp.data
    assert (data.game.status, data.game.away_score, data.game.home_score) == ("final", 113, 121)
    assert data.game.game_clock is None
    assert (data.home_periods, data.away_periods) == ([24, 21, 32, 44], [23, 35, 23, 32])
    assert data.lines_source == "settled"
    # By fantasy points, then points.
    assert [(l.player_name, l.team, l.fpts, l.min) for l in data.lines] == [
        ("Paul George", "BOS", 72, 30), ("Jaylen Brown", "BOS", 40, 30), ("A Rookie", "DET", 40, 30),
    ]
    assert data.lines[0].plus_minus == -4 and data.lines[0].espn_id == 9001


def test_before_the_game_log_lands_the_live_read_stands_in_with_its_team_from_the_box_score(monkeypatch):
    box_score(monkeypatch)
    # Jaylen Brown has a season row; the rookie does not, and only the box score knows their side.
    PlayerSeasonStats.upsert_season_stats(2, NIGHT, "2026-27", {"gp": 1, "fpts": 40}, team_id="BOS")
    live_line(1, fpts=72, pts=38)
    live_line(2, fpts=40, pts=25)
    live_line(3, fpts=20, pts=10)

    data = get().data

    assert data.lines_source == "live"
    assert [(l.player_id, l.team) for l in data.lines] == [(1, "BOS"), (2, "BOS"), (3, "DET")]
    assert all(l.plus_minus is None for l in data.lines)


def test_without_the_box_score_the_lines_still_come_and_the_period_line_is_empty(monkeypatch):
    box_score(monkeypatch, RuntimeError("Access Denied"))
    PlayerSeasonStats.upsert_season_stats(2, NIGHT, "2026-27", {"gp": 1, "fpts": 40}, team_id="BOS")
    live_line(2, fpts=40, pts=25)
    live_line(3, fpts=20, pts=10)

    resp = get()

    assert resp.status == ApiStatus.SUCCESS
    assert (resp.data.home_periods, resp.data.away_periods) == ([], [])
    assert resp.data.game.status == "scheduled"   # what nba.games says, until the post-game run
    assert [(l.player_id, l.team) for l in resp.data.lines] == [(2, "BOS"), (3, None)]


def test_score_history_starts_at_the_tip_and_keeps_only_the_ends_of_each_unchanged_run(monkeypatch):
    box_score(monkeypatch)
    for m in range(-20, 0):
        snap(m, 0, 0, status=1, period=None, clock=None)        # pre-tip polls
    for m in range(0, 5):
        snap(m, 8, 9)                                            # the feed held 9-8 for five polls
    snap(5, 19, 23, clock="PT03M24.00S")
    for m in range(6, 9):
        snap(m, 121, 113, status=3, period=4, clock=None)        # read until the box score settles

    history = get().data.score_history

    assert [(h.captured_at[11:16], h.away_score, h.home_score, h.game_status) for h in history] == [
        ("19:10", 9, 8, 2), ("19:14", 9, 8, 2), ("19:15", 23, 19, 2), ("19:16", 113, 121, 3), ("19:18", 113, 121, 3),
    ]


def test_a_game_with_nothing_on_record_is_a_bare_game(monkeypatch):
    box_score(monkeypatch)

    data = get().data

    assert (data.lines, data.lines_source, data.score_history) == ([], None, [])


def test_a_game_on_another_night_or_an_unknown_id_is_not_found(monkeypatch):
    box_score(monkeypatch)

    assert get(day=NIGHT + timedelta(days=1)).status == ApiStatus.NOT_FOUND
    assert get(game_id="0022600099").status == ApiStatus.NOT_FOUND


def test_a_game_not_yet_due_never_asks_the_league_feed(monkeypatch):
    calls = box_score(monkeypatch)
    monkeypatch.setattr(game_detail_service, "_get_nba_today", lambda: NIGHT - timedelta(days=1))

    data = get().data

    assert calls == []
    assert data.game.status == "scheduled" and data.home_periods == []
