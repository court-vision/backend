"""Percentiles: the league pool is aggregated once per season scope, not once per call.

The reference below is the percentile code as it was before the pool was cached
(verbatim apart from the decorator), so the equivalence cases prove the
in-memory lookup returns exactly what the per-call GROUP BY did.
"""

from datetime import date
from types import SimpleNamespace

import pytest
from peewee import Case, fn

from core.errors import NotFoundError
from core.settings import settings
from db.base import db
from db.models.nba.player_game_stats import PlayerGameStats
from db.models.nba.players import Player
from schemas.common import ApiStatus
from schemas.player import PercentileData, PlayerPercentilesResp
from services import player_service
from services.player_service import PlayerService, _recent_seasons, _season_opening_night, scaled_min_games

pytestmark = pytest.mark.integration

SEASON = "2025-26"  # opening night 2025-10-21


def _percentiles_before_the_cache(player_id: int, min_games: int = 20) -> PlayerPercentilesResp:
    """PlayerService.get_player_percentiles as of origin/main ac5f69a."""
    player = Player.get_or_none(Player.id == player_id)
    if not player:
        raise NotFoundError("PLAYER_NOT_FOUND", "Player not found")
    stat_fields = {
        "avg_fpts": fn.AVG(PlayerGameStats.fpts),
        "avg_points": fn.AVG(PlayerGameStats.pts),
        "avg_rebounds": fn.AVG(PlayerGameStats.reb),
        "avg_assists": fn.AVG(PlayerGameStats.ast),
        "avg_steals": fn.AVG(PlayerGameStats.stl),
        "avg_blocks": fn.AVG(PlayerGameStats.blk),
        "avg_turnovers": fn.AVG(PlayerGameStats.tov),
        "avg_minutes": fn.AVG(PlayerGameStats.min),
        "avg_fg3m": fn.AVG(PlayerGameStats.fg3m),
        "avg_fg_pct": Case(None, [(fn.SUM(PlayerGameStats.fga) > 0,
                                   fn.SUM(PlayerGameStats.fgm) * 100.0 / fn.SUM(PlayerGameStats.fga))], 0.0),
        "avg_fg3_pct": Case(None, [(fn.SUM(PlayerGameStats.fg3a) > 0,
                                    fn.SUM(PlayerGameStats.fg3m) * 100.0 / fn.SUM(PlayerGameStats.fg3a))], 0.0),
        "avg_ft_pct": Case(None, [(fn.SUM(PlayerGameStats.fta) > 0,
                                   fn.SUM(PlayerGameStats.ftm) * 100.0 / fn.SUM(PlayerGameStats.fta))], 0.0),
    }

    def _pool(start, end):
        q = PlayerGameStats.select(
            PlayerGameStats.player_id,
            fn.COUNT(PlayerGameStats.id).alias("games"),
            *[expr.alias(name) for name, expr in stat_fields.items()],
        ).where(PlayerGameStats.for_fantasy() & _recent_seasons(PlayerGameStats))
        if start is not None:
            q = q.where(PlayerGameStats.game_date >= start)
        if end is not None:
            q = q.where(PlayerGameStats.game_date < end)
        return list(q.group_by(PlayerGameStats.player_id).dicts())

    opening = _season_opening_night()
    all_players = _pool(opening, None)
    if not all_players and opening is not None:
        all_players = _pool(date(opening.year - 1, 8, 1), opening)
    if not all_players:
        return PlayerPercentilesResp(status=ApiStatus.SUCCESS, message="No qualifying players yet — percentiles start after opening night", data=None)
    min_games = scaled_min_games(min_games, max(int(r["games"]) for r in all_players))
    all_players = [r for r in all_players if int(r["games"]) >= min_games]
    if not all_players:
        return PlayerPercentilesResp(status=ApiStatus.SUCCESS, message="No qualifying players yet — percentiles start after opening night", data=None)
    target_row = None
    for row in all_players:
        if row["player"] == player_id:
            target_row = row
            break
    if target_row is None:
        return PlayerPercentilesResp(status=ApiStatus.SUCCESS, message=f"Player does not meet minimum games threshold ({min_games})", data=None)
    total = len(all_players)
    invert_stats = {"avg_turnovers"}
    percentiles = {}
    for stat_name in stat_fields:
        target_val = float(target_row[stat_name])
        if stat_name in invert_stats:
            lower_count = sum(1 for r in all_players if float(r[stat_name]) > target_val)
        else:
            lower_count = sum(1 for r in all_players if float(r[stat_name]) < target_val)
        percentiles[stat_name] = round((lower_count / total) * 100)
    return PlayerPercentilesResp(status=ApiStatus.SUCCESS, message="Percentiles calculated successfully", data=PercentileData(**percentiles))


percentiles = PlayerService.get_player_percentiles.__wrapped__


@pytest.fixture(autouse=True)
def league(clean_tables, monkeypatch):
    monkeypatch.setattr(settings, "nba_season", SEASON)
    # Eight players with 1-8 games each and varied lines, so floors, ties and
    # shooting rates all move the answer.
    for pid in range(1, 9):
        Player.create(id=pid, espn_id=7000 + pid, name=f"Player {pid}", name_normalized=f"player {pid}", position="G")
        for n in range(pid):
            day = date(2025, 11, 1 + n)
            PlayerGameStats.upsert_game_stats(pid, day, {
                "pts": 10 + (pid * 7 + n * 3) % 25, "reb": (pid + n) % 12, "ast": (pid * 3 + n) % 9,
                "stl": n % 3, "blk": pid % 3, "tov": (pid + 2 * n) % 5, "min": 20 + pid + n,
                "fgm": 4 + (pid + n) % 6, "fga": 9 + pid % 5, "fg3m": (pid + n) % 4, "fg3a": 2 + pid % 4,
                "ftm": pid % 4, "fta": (pid % 4) + n % 2, "fpts": 20 + (pid * 5 + n * 11) % 37,
            })
    # A preseason row (by its game id, and before opening night): outside the pool.
    PlayerGameStats.upsert_game_stats(1, date(2025, 10, 10), {"pts": 99, "fpts": 99}, game_id="0012500010")
    yield


@pytest.fixture
def pool_queries(monkeypatch):
    statements: list[str] = []
    real = db.execute_sql

    def counting(sql, params=None, *args, **kwargs):
        statements.append(sql)
        return real(sql, params, *args, **kwargs)

    monkeypatch.setattr(db, "execute_sql", counting)
    return lambda: [s for s in statements if "GROUP BY" in s]


@pytest.mark.parametrize("min_games", [1, 2, 3, 4, 5, 20, 82])
def test_every_player_gets_what_the_per_call_aggregate_gave(min_games):
    answered = set()
    for pid in range(1, 9):
        old = _percentiles_before_the_cache(pid, min_games=min_games)
        assert percentiles(pid, min_games=min_games).model_dump_json() == old.model_dump_json()
        if old.data is not None:
            answered.add(pid)

    # Not vacuous: the floor (scaled to at most half of 8 games) leaves some players out.
    assert 8 in answered and (min_games == 1) == (answered == set(range(1, 9)))
    with pytest.raises(NotFoundError):
        percentiles(99, min_games=min_games)


def test_the_pool_is_aggregated_once_for_every_player_and_floor(pool_queries):
    for pid in range(1, 9):
        for min_games in (1, 4, 20):
            assert percentiles(pid, min_games=min_games).status == ApiStatus.SUCCESS

    assert len(pool_queries()) == 1


@pytest.fixture
def pool_clock(monkeypatch):
    """The pool's monotonic clock, at the start of a ten-minute window and moved by hand."""
    clock = SimpleNamespace(now=6000.0)
    monkeypatch.setattr(player_service, "time", SimpleNamespace(monotonic=lambda: clock.now))
    return clock


def test_the_pool_is_aggregated_again_in_the_next_window(pool_queries, pool_clock):
    percentiles(1, min_games=1)
    pool_clock.now += 599
    percentiles(2, min_games=1)
    assert len(pool_queries()) == 1

    pool_clock.now += 1
    percentiles(3, min_games=1)
    assert len(pool_queries()) == 2


def test_a_pool_built_late_in_a_window_ends_with_it(pool_queries, pool_clock):
    """Fixed windows, not ten minutes from each build: a pool built ten seconds
    before a window's end is replaced at that end, the moment the percentile
    route stops serving bodies derived from it (api/v1/public/players.py)."""
    pool_clock.now += 590
    percentiles(1, min_games=1)
    pool_clock.now += 10
    percentiles(1, min_games=1)

    assert len(pool_queries()) == 2


def test_within_a_window_new_box_scores_wait_for_the_next_aggregate():
    """The documented staleness: a pipeline write shows within ten minutes, not at once."""
    before = percentiles(1, min_games=1).data
    Player.create(id=50, espn_id=7050, name="Late Signing", name_normalized="late signing", position="C")
    PlayerGameStats.upsert_game_stats(50, date(2025, 11, 20), {"pts": 0, "fpts": 0, "min": 1})

    assert percentiles(50, min_games=1).message.startswith("Player does not meet")  # not in the cached pool
    assert percentiles(1, min_games=1).data == before

    player_service.reset_percentile_pool_cache()
    assert percentiles(50, min_games=1).data is not None


def test_the_kill_switch_aggregates_on_every_call_as_before_the_cache(pool_queries, monkeypatch):
    """PUBLIC_READ_CACHE_ENABLED=false restores the uncached route exactly: no
    pool is held, so a pipeline write shows on the very next call."""
    monkeypatch.setattr(settings, "public_read_cache_enabled", False)
    for pid in range(1, 9):
        assert percentiles(pid, min_games=1).model_dump_json() == _percentiles_before_the_cache(pid, min_games=1).model_dump_json()
    Player.create(id=50, espn_id=7050, name="Late Signing", name_normalized="late signing", position="C")
    PlayerGameStats.upsert_game_stats(50, date(2025, 11, 20), {"pts": 0, "fpts": 0, "min": 1})

    late = percentiles(50, min_games=1)

    assert late.data is not None
    assert late.model_dump_json() == _percentiles_before_the_cache(50, min_games=1).model_dump_json()
    assert len(pool_queries()) == 2 * 9   # nine calls, each aggregating once, and so did the reference


def test_switching_the_cache_off_drops_the_pool_held_until_then(monkeypatch):
    """Nothing aggregated before the switch is kept for when it is turned back on."""
    percentiles(1, min_games=1)   # a pool is held
    Player.create(id=50, espn_id=7050, name="Late Signing", name_normalized="late signing", position="C")
    PlayerGameStats.upsert_game_stats(50, date(2025, 11, 20), {"pts": 0, "fpts": 0, "min": 1})

    monkeypatch.setattr(settings, "public_read_cache_enabled", False)
    assert percentiles(50, min_games=1).data is not None
    monkeypatch.setattr(settings, "public_read_cache_enabled", True)
    assert percentiles(50, min_games=1).data is not None


def test_another_season_is_another_pool(pool_queries, monkeypatch):
    percentiles(1, min_games=1)
    monkeypatch.setattr(settings, "nba_season", "2026-27")
    percentiles(1, min_games=1)

    assert len(pool_queries()) >= 2


def test_before_opening_night_the_fallback_pool_is_cached_too(pool_queries):
    """No 2025-26 box scores yet: last season's pool answers, and is reused."""
    db.execute_sql("DELETE FROM nba.player_game_stats")
    for pid in (1, 2, 3):
        PlayerGameStats.upsert_game_stats(pid, date(2025, 3, pid), {"pts": 10 * pid, "fpts": 20 * pid})

    first = percentiles(3, min_games=1)
    second = percentiles(1, min_games=1)

    assert first.data.avg_points == 67 and second.data.avg_points == 0
    # Current season (empty), then last season: two aggregates for the first call, none after.
    assert len(pool_queries()) == 2
    assert first.model_dump_json() == _percentiles_before_the_cache(3, min_games=1).model_dump_json()


def test_an_unknown_player_is_still_a_404_with_the_pool_cached():
    percentiles(1, min_games=1)

    with pytest.raises(NotFoundError):
        percentiles(12345, min_games=1)


def test_an_empty_league_is_the_empty_state():
    db.execute_sql("DELETE FROM nba.player_game_stats")

    resp = percentiles(1, min_games=1)
    assert resp.status == ApiStatus.SUCCESS and resp.data is None
    assert resp.model_dump_json() == _percentiles_before_the_cache(1, min_games=1).model_dump_json()
