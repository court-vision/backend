"""The box-score and games tables hold every season since 2012-13 and the
playoffs (migration 0031). Fantasy readers see exactly what they saw before:
this season and last, regular season and the Cup final. Name lookups see only
today's players."""

from datetime import date

import pytest

from core.settings import settings
from db.models.nba.games import Game
from db.models.nba.player_game_stats import PlayerGameStats
from db.models.nba.player_profiles import PlayerProfile
from db.models.nba.player_season_stats import PlayerSeasonStats
from db.models.nba.players import Player
from db.models.nba.teams import NBATeam
from services.player_games_service import PlayerGamesService
from services.player_service import PlayerService

pytestmark = pytest.mark.integration
SEASON = "2025-26"
SGA = 1628983


@pytest.fixture(autouse=True)
def history(clean_tables, monkeypatch):
    monkeypatch.setattr(settings, "nba_season", SEASON)
    for team in ("OKC", "MEM", "DEN", "SEA"):
        NBATeam.get_or_create(id=team, defaults={"name": team, "conference": "West", "division": "Northwest"})
    Player.create(id=SGA, espn_id=4278073, name="Shai Gilgeous-Alexander", name_normalized="shai gilgeous-alexander")
    PlayerProfile.create(player=SGA)
    # One regular-season game this season, one last season, one from his
    # history, and two playoff games: only the first two are fantasy rows.
    for game_id, day, pts in (
        ("0022500010", date(2026, 3, 1), 30),
        ("0022400010", date(2025, 3, 1), 20),
        ("0022100010", date(2022, 3, 1), 10),
        ("0042500101", date(2026, 4, 25), 50),
        ("0042400101", date(2025, 4, 25), 50),
    ):
        Game.create(game_id=game_id, game_date=day, season=f"20{game_id[3:5]}-{int(game_id[3:5]) + 1}",
                    home_team_id="OKC", away_team_id="MEM", status="final")
        PlayerGameStats.upsert_game_stats(SGA, day, {"fpts": pts, "pts": pts}, team_id="OKC", game_id=game_id)
    yield


def test_rows_take_season_and_type_from_their_game():
    rows = {r.game_id: (r.season, r.season_type) for r in PlayerGameStats.select()}
    assert rows["0022500010"] == ("2025-26", "regular")
    assert rows["0042500101"] == ("2025-26", "playoffs")
    assert rows["0022100010"] == ("2021-22", "regular")
    assert Game.get_by_id("0042500101").season_type == "playoffs"


def test_player_stats_keep_to_fantasy_rows_of_this_season_and_last():
    data = PlayerService.get_player_stats.__wrapped__(player_id=SGA).data
    # 2025-26's one regular-season game: not its playoff game, not 2024-25's.
    assert data.games_played == 1
    assert data.avg_stats.avg_points == 30


def test_the_game_log_never_shows_playoffs_or_history():
    games = PlayerGamesService.get_player_games.__wrapped__(SGA, limit=10).data.games
    assert [g.date for g in games] == ["2026-03-01", "2025-03-01"]


def test_the_fantasy_schedule_leaves_out_the_playoffs():
    from db.models.nba.games import FANTASY_SEASON_TYPES

    games = Game.get_team_games("OKC", season=SEASON, season_types=FANTASY_SEASON_TYPES)
    assert [g.game_id for g in games] == ["0022500010"]
    assert len(Game.get_team_games("OKC", seasons=("2024-25", SEASON))) == 4


def test_a_name_means_one_player_in_play():
    Player.create(id=203183, name="Tony Mitchell", name_normalized="tony mitchell")      # retired
    Player.create(id=203502, name="Tony Mitchell", name_normalized="tony mitchell")      # retired
    Player.create(id=1, name="Shai Gilgeous-Alexander", name_normalized="shai gilgeous-alexander")  # namesake
    assert Player.find_by_name("Tony Mitchell") is None
    assert Player.find_by_name("Shai Gilgeous-Alexander").id == SGA
    PlayerProfile.create(player=1)
    assert Player.find_by_name("Shai Gilgeous-Alexander") is None   # two current players: no guess


def test_in_play_is_a_profile_or_recent_season_stats():
    Player.create(id=2, name="Recent Vet", name_normalized="recent vet")
    PlayerSeasonStats.upsert_season_stats(2, date(2025, 4, 1), "2024-25", {"gp": 1, "fpts": 0}, team_id="OKC")
    Player.create(id=3, name="Long Gone", name_normalized="long gone")
    in_play = {p.id for p in Player.select(Player.id).where(Player.in_play())}
    assert in_play == {SGA, 2}
