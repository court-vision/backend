"""Public reads exercised against the real migrations and sparse ingestion grains."""

from datetime import date, datetime, timedelta, timezone

import pytest
from freezegun import freeze_time

from core.errors import NotFoundError
from core.settings import settings
from db.base import db
from db.models.api_keys import APIKey
from db.models.nba.draft_market import DraftMarket
from db.models.nba.games import Game
from db.models.nba.player_game_stats import PlayerGameStats
from db.models.nba.player_profiles import PlayerProfile
from db.models.nba.player_projections import PlayerProjection
from db.models.nba.player_rolling_stats import PlayerRollingStats
from db.models.nba.player_season_stats import PlayerSeasonStats
from db.models.nba.players import Player
from db.models.nba.teams import NBATeam
from services.nba_team_live_game_service import _get_team_player_ids
from services.nba_team_roster_service import NBATeamRosterService
from services.player_games_service import PlayerGamesService
from services.player_profile_service import PlayerProfileService
from services.player_service import PlayerService
from services.players_list_service import PlayersListService
from services.public_market_service import PublicMarketService
from services.trends_service import TrendsService

pytestmark = pytest.mark.integration
SEASON = "2025-26"
OLD = date(2026, 3, 1)
NEW = date(2026, 3, 4)


@pytest.fixture(autouse=True)
def setup_public_data(clean_tables, monkeypatch):
    monkeypatch.setattr(settings, "nba_season", SEASON)
    db.execute_sql("REFRESH MATERIALIZED VIEW nba.rankings")
    db.execute_sql("TRUNCATE usr.api_keys")
    for team in ("LAL", "BOS", "DEN"):
        NBATeam.get_or_create(id=team, defaults={"name": team, "city": team, "conference": "East", "division": "Atlantic"})


def player(pid, name=None):
    name = name or f"Player {pid}"
    return Player.create(id=pid, espn_id=9000 + pid, name=name, name_normalized=name.lower(), position="C")


def snapshot(pid, day, team="LAL", season=SEASON, fpts=100):
    return PlayerSeasonStats.upsert_season_stats(pid, day, season, {"gp": 5, "fpts": fpts}, team_id=team)


def test_roster_keeps_inactive_players_and_excludes_traded_players():
    for pid in (1, 2, 3):
        player(pid)
    snapshot(1, NEW)
    snapshot(2, OLD)  # DNP/injured since March 1; must remain on the roster.
    snapshot(3, OLD)
    snapshot(3, NEW, team="BOS")
    response = NBATeamRosterService.get_team_roster.__wrapped__("lal")
    assert response.status == "success"
    assert {p.player_id for p in response.data.players} == {1, 2}
    assert response.data.season == SEASON
    assert _get_team_player_ids("LAL") == {1, 2}
    assert _get_team_player_ids("BOS") == {3}


def test_player_list_uses_fresh_league_ranks_and_accent_search():
    player(1, "Nikola Jokić")
    player(2)
    snapshot(1, OLD, fpts=100)
    snapshot(2, OLD, fpts=200)
    db.execute_sql("REFRESH MATERIALIZED VIEW nba.rankings")
    snapshot(1, NEW, fpts=400)  # Ingestion succeeded; matview refresh did not.
    response = PlayersListService.list_players.__wrapped__(name=" JOKIC ")
    assert response.status == "success"
    assert response.data.total == 1
    assert response.data.players[0].rank == 1
    assert response.data.season == SEASON
    assert response.data.as_of_date == NEW.isoformat()


@freeze_time("2026-09-07T12:30:00Z")
def test_dimension_search_includes_mapped_players_without_season_stats():
    rookie = player(1, "Rookie One")
    player(2, "Rookie Two")
    profile_updated_at = datetime(2026, 9, 7, 12, 30)
    PlayerProfile.create(
        player=rookie,
        first_name="Rookie",
        last_name="One",
        position="F",
        team="LAL",
        updated_at=profile_updated_at,
    )

    response = PlayerProfileService.search_players.__wrapped__("rookie")

    assert response.status == "success"
    assert [item.id for item in response.data.players] == [1, 2]
    assert response.data.players[0].espn_id == 9001
    assert response.data.players[0].team == "LAL"
    assert response.data.players[0].profile_updated_at.isoformat() == "2026-09-07T12:30:00+00:00"
    assert response.data.players[1].profile_updated_at is None
    assert PlayersListService.list_players.__wrapped__().data.players == []


def test_dimension_search_matches_accents_and_both_identifier_systems():
    player(1, "Nikola Jokić")
    search = PlayerProfileService.search_players.__wrapped__

    assert search("jokic").data.players[0].id == 1
    assert search("1").data.players[0].id == 1
    assert search("9001").data.players[0].id == 1


def test_profile_exposes_identity_snapshot_and_allows_a_missing_profile():
    profiled = player(1, "Rookie One")
    player(2, "Rookie Two")
    PlayerProfile.create(
        player=profiled,
        first_name="Rookie",
        last_name="One",
        birthdate=date(2005, 1, 1),
        height="6-8",
        weight=205,
        position="F",
        jersey_number="2",
        team="LAL",
        draft_year=2026,
        draft_round=1,
        draft_number=1,
        season_exp=0,
        country="USA",
        school="Example University",
        from_year=2026,
        to_year=2026,
        updated_at=datetime(2026, 9, 7, 12, 30),
    )

    response = PlayerProfileService.get_profile.__wrapped__(1)

    assert response.data.id == 1 and response.data.espn_id == 9001
    assert response.data.updated_at.tzinfo == timezone.utc
    assert response.data.profile.height_inches == 80
    assert response.data.profile.team == "LAL"
    assert response.data.profile.updated_at.tzinfo == timezone.utc
    assert PlayerProfileService.get_profile.__wrapped__(2).data.profile is None
    with pytest.raises(NotFoundError):
        PlayerProfileService.get_profile.__wrapped__(999)


def test_roster_and_list_disclose_previous_season_fallback(monkeypatch):
    player(1)
    snapshot(1, NEW)
    monkeypatch.setattr(settings, "nba_season", "2026-27")
    assert NBATeamRosterService.get_team_roster.__wrapped__("LAL").data.season == SEASON
    assert PlayersListService.list_players.__wrapped__().data.season == SEASON


@freeze_time("2026-03-05T15:00:00Z")
def test_trends_do_not_resurrect_omitted_or_stale_rolling_rows():
    player(1)
    player(2)
    PlayerRollingStats.upsert_rolling_stats(1, OLD, 7, 2, {"fpts": 99})
    PlayerRollingStats.upsert_rolling_stats(2, NEW, 7, 1, {"fpts": 10})
    PlayerRollingStats.upsert_rolling_stats(1, date(2025, 4, 15), 14, 3, {"fpts": 55})
    PlayerRollingStats.upsert_rolling_stats(1, NEW, 30, 3, {"fpts": 22})
    response = TrendsService.get_player_trends.__wrapped__(1)
    assert response.status == "success"
    assert set(response.data.trends) == {"last_30_days"}
    assert response.data.trends["last_30_days"].as_of_date == NEW.isoformat()
    assert PlayerService.get_last_n_day_avg(1, 7) is None
    assert PlayerService.get_last_n_day_avg(1, 14) is None
    assert PlayerService.get_last_n_day_avg(1, 30) == 22


def test_game_logs_join_historical_team_and_keep_unscheduled_rows():
    player(1)
    for day, team in ((OLD, "LAL"), (NEW, "BOS"), (date(2026, 3, 5), None)):
        PlayerGameStats.upsert_game_stats(1, day, {"pts": 20}, team_id=team)
    Game.create(game_id="home", game_date=OLD, season=SEASON, home_team="LAL", away_team="DEN")
    Game.create(game_id="away", game_date=NEW, season=SEASON, home_team="DEN", away_team="BOS")
    response = PlayerGamesService.get_player_games.__wrapped__(1)
    assert response.status == "success"
    assert [(g.game_id, g.opponent, g.home) for g in response.data.games] == [
        (None, None, None), ("away", "DEN", False), ("home", "DEN", True),
    ]


def test_percentiles_read_normalized_game_rows():
    player(1)
    player(2)
    PlayerGameStats.upsert_game_stats(1, OLD, {"pts": 20, "fpts": 40})
    PlayerGameStats.upsert_game_stats(2, OLD, {"pts": 10, "fpts": 20})
    response = PlayerService.get_player_percentiles.__wrapped__(1, min_games=1)
    assert response.status == "success"
    assert response.data.avg_points == 50


def test_stats_keep_nba_and_espn_ids_distinct_and_normalize_name_and_team():
    player(1, "Nikola Jokić")
    player(9001)
    PlayerGameStats.upsert_game_stats(1, OLD, {"pts": 20}, team_id="DEN")
    PlayerGameStats.upsert_game_stats(9001, OLD, {"pts": 10}, team_id="BOS")
    lookup = PlayerService.get_player_stats.__wrapped__
    assert lookup(espn_id=9001).data.id == 1
    assert lookup(player_id=9001).data.id == 9001
    assert lookup(name="Nikola Jokic", team=" den ").data.id == 1


def test_market_uses_one_snapshot_with_stable_pagination_and_nulls_last():
    for pid in (1, 2, 3, 4):
        player(pid)
    DraftMarket.record_market(4, SEASON, OLD, overall_rank=1)
    for pid, rank in ((1, 2), (2, 2), (3, None)):
        DraftMarket.record_market(pid, SEASON, NEW, overall_rank=rank, adp=10 if pid == 3 else None)
    DraftMarket.record_market(4, "2026-27", NEW, overall_rank=1)
    DraftMarket.record_market(4, SEASON, NEW, overall_rank=1, source="cv")
    response = PublicMarketService.get_market.__wrapped__(limit=1, offset=1)
    assert response.data.total == 3
    assert [p.player_id for p in response.data.players] == [2]
    assert response.data.as_of_date == NEW
    assert response.data.source == "espn"
    by_adp = PublicMarketService.get_market.__wrapped__(sort_by="adp")
    assert [p.player_id for p in by_adp.data.players] == [3, 1, 2]
    assert by_adp.data.players[0].overall_rank is None
    assert by_adp.data.players[1].adp is None


def test_market_history_selects_latest_on_or_before_date_and_empty_season():
    player(1, "Nikola Jokić")
    DraftMarket.record_market(1, SEASON, OLD, overall_rank=2)
    DraftMarket.record_market(1, SEASON, NEW, overall_rank=1)
    response = PublicMarketService.get_market.__wrapped__(as_of=date(2026, 3, 3), name="JOKIC")
    assert response.data.as_of_date == OLD
    assert response.data.players[0].overall_rank == 2
    empty = PublicMarketService.get_market.__wrapped__(season="2026-27")
    assert empty.status == "success"
    assert empty.data.players == [] and empty.data.as_of_date is None


def test_projection_preserves_unknowns_recomputes_rates_and_supports_rookies():
    player(1)  # No game or season statistics required.
    PlayerProjection.record_projection(1, SEASON, OLD, {"pts": 23.5, "fgm": 8, "fga": 16, "fg3m": 0, "fg3a": 0}, projected_gp=65)
    response = PublicMarketService.get_projection.__wrapped__(1)
    assert response.data.player_id == 1 and response.data.espn_id == 9001
    assert response.data.stats.pts == 23.5
    assert response.data.stats.fg_pct == 0.5
    assert response.data.stats.fg3_pct is None and response.data.stats.ft_pct is None
    assert response.data.stats.reb is None
    assert response.data.projected_gp == 65
    assert response.data.as_of_date == OLD


def test_projection_omissions_are_empty_and_unknown_players_are_404():
    player(1)
    player(2)
    PlayerProjection.record_projection(1, SEASON, OLD, {"pts": 30})
    PlayerProjection.record_projection(2, SEASON, NEW, {"pts": 10})
    current = PublicMarketService.get_projection.__wrapped__(1)
    assert current.status == "success" and current.data is None
    historical = PublicMarketService.get_projection.__wrapped__(1, as_of=OLD)
    assert historical.data.stats.pts == 30
    with pytest.raises(NotFoundError):
        PublicMarketService.get_projection.__wrapped__(999)


def test_bulk_projections_resolve_snapshots_search_accents_and_page_stably():
    player(1, "Nikola Jokić")
    player(2, "Aaron Alpha")
    player(3, "Aaron Alpha")
    player(4, "Rookie Four")
    PlayerProjection.record_projection(1, SEASON, OLD, {"pts": 30})
    PlayerProjection.record_projection(2, SEASON, OLD, {"pts": 10})
    PlayerProjection.record_projection(1, SEASON, NEW, {"pts": 25, "fgm": 9, "fga": 18})
    PlayerProjection.record_projection(2, SEASON, NEW, {"pts": 11})
    PlayerProjection.record_projection(3, SEASON, NEW, {"pts": 12})
    PlayerProjection.record_projection(
        4,
        SEASON,
        NEW,
        {"pts": 15, "fg3m": 1, "fg3a": 0},
        projected_gp=None,
    )

    response = PublicMarketService.get_projections.__wrapped__(limit=2, offset=1)

    assert response.data.as_of_date == NEW
    assert response.data.total == 4
    assert [item.player_id for item in response.data.players] == [3, 1]
    assert response.data.players[1].stats.fg_pct == 0.5
    assert response.data.players[1].stats.reb is None

    historical = PublicMarketService.get_projections.__wrapped__(as_of=date(2026, 3, 3))
    assert historical.data.as_of_date == OLD
    assert [item.player_id for item in historical.data.players] == [2, 1]
    assert historical.data.players[1].stats.pts == 30

    searched = PublicMarketService.get_projections.__wrapped__(name="JOKIC")
    assert [item.player_id for item in searched.data.players] == [1]

    rookie = PublicMarketService.get_projections.__wrapped__(name="Rookie").data.players[0]
    assert rookie.projected_gp is None
    assert rookie.stats.fg3_pct is None
    assert PlayerSeasonStats.select().where(PlayerSeasonStats.player == 4).count() == 0

    empty = PublicMarketService.get_projections.__wrapped__(season="2026-27")
    assert empty.status == "success"
    assert empty.data.as_of_date is None and empty.data.players == [] and empty.data.total == 0


def test_market_movement_signs_sorting_union_nulls_and_pagination():
    for pid, name in (
        (1, "Nikola Jokić"),
        (2, "Faller Two"),
        (3, "Exit Three"),
        (4, "Entry Four"),
        (5, "Steady Five"),
        (6, "Riser Six"),
    ):
        player(pid, name)
    DraftMarket.record_market(
        1,
        SEASON,
        OLD,
        overall_rank=10,
        adp=20,
        auction_value=10,
        auction_value_avg=None,
    )
    DraftMarket.record_market(
        1,
        SEASON,
        NEW,
        overall_rank=5,
        adp=18,
        auction_value=12,
        auction_value_avg=15,
    )
    DraftMarket.record_market(2, SEASON, OLD, overall_rank=5, adp=30, auction_value=20)
    DraftMarket.record_market(2, SEASON, NEW, overall_rank=8, adp=30, auction_value=15)
    DraftMarket.record_market(3, SEASON, OLD, overall_rank=30, auction_value=1)
    DraftMarket.record_market(4, SEASON, NEW, overall_rank=40, auction_value=2)
    DraftMarket.record_market(5, SEASON, OLD, overall_rank=7)
    DraftMarket.record_market(5, SEASON, NEW, overall_rank=7)
    DraftMarket.record_market(6, SEASON, OLD, overall_rank=20)
    DraftMarket.record_market(6, SEASON, NEW, overall_rank=15)

    movement = PublicMarketService.get_market_movement.__wrapped__(
        from_as_of=date(2026, 3, 3),
        to_as_of=date(2026, 3, 5),
        limit=3,
        offset=1,
    )

    assert movement.data.from_as_of_date == OLD
    assert movement.data.to_as_of_date == NEW
    assert movement.data.total == 6
    assert [item.player_id for item in movement.data.players] == [6, 2, 5]
    by_id = {
        item.player_id: item
        for item in PublicMarketService.get_market_movement.__wrapped__(
            from_as_of=OLD,
            to_as_of=NEW,
        ).data.players
    }
    assert by_id[1].changes.overall_rank == 5
    assert by_id[1].changes.adp == 2
    assert by_id[1].changes.auction_value == 2
    assert by_id[1].changes.auction_value_avg is None
    assert by_id[2].changes.overall_rank == -3
    assert by_id[5].changes.overall_rank == 0
    assert by_id[3].before is not None and by_id[3].after is None
    assert by_id[4].before is None and by_id[4].after is not None
    assert by_id[3].changes.overall_rank is None

    risers = PublicMarketService.get_market_movement.__wrapped__(
        from_as_of=OLD,
        to_as_of=NEW,
        direction="up",
    )
    assert [item.player_id for item in risers.data.players] == [1, 6]
    fallers = PublicMarketService.get_market_movement.__wrapped__(
        from_as_of=OLD,
        to_as_of=NEW,
        direction="down",
    )
    assert [item.player_id for item in fallers.data.players] == [2]
    auction_fallers = PublicMarketService.get_market_movement.__wrapped__(
        from_as_of=OLD,
        to_as_of=NEW,
        metric="auction_value",
        direction="down",
    )
    assert [item.player_id for item in auction_fallers.data.players] == [2]
    searched = PublicMarketService.get_market_movement.__wrapped__(
        from_as_of=OLD,
        to_as_of=NEW,
        name="JOKIC",
    )
    assert [item.player_id for item in searched.data.players] == [1]


def test_market_movement_returns_empty_when_two_distinct_snapshots_cannot_resolve():
    player(1)
    DraftMarket.record_market(1, SEASON, OLD, overall_rank=10)

    same = PublicMarketService.get_market_movement.__wrapped__(
        from_as_of=OLD,
        to_as_of=NEW,
    )
    assert same.status == "success"
    assert same.data.from_as_of_date == same.data.to_as_of_date == OLD
    assert same.data.players == [] and same.data.total == 0
    assert "distinct" in same.message

    empty = PublicMarketService.get_market_movement.__wrapped__(
        from_as_of=OLD,
        to_as_of=NEW,
        season="2026-27",
    )
    assert empty.status == "success"
    assert empty.data.from_as_of_date is None and empty.data.to_as_of_date is None
    assert empty.data.players == []


def test_expiring_api_keys_accept_postgres_timezone_aware_timestamps():
    now = datetime.now(timezone.utc)
    raw, _ = APIKey.create_key("future", ["analytics"], expires_at=now + timedelta(days=1))
    valid = APIKey.verify_key(raw)
    assert valid is not None and valid.last_used_at.tzinfo is not None
    expired, _ = APIKey.create_key("expired", ["analytics"], expires_at=now - timedelta(seconds=1))
    assert APIKey.verify_key(expired) is None
