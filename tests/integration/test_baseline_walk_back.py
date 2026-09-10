"""
Integration: the baseline walks back past a season a player missed.

`baseline_records(walk_back=True)` is a DISTINCT ON with the season pinned to a
ceiling rather than an equality, which is exactly the sort of thing a fake
cannot check — the ordering that decides which season survives lives in the
query. These run against the real migration chain.

The rule it enforces: a player with no qualifying row in last season is valued
off the most recent season he did play. Without it, going into 2026-27 a lost
season erased Haliburton, Kyrie Irving, VanVleet and Lillard — four of ESPN's
top 70 — because they had no row at all to rank.
"""

from datetime import date, datetime

import pytest

from db.models.nba.players import Player
from db.models.nba.player_season_stats import PlayerSeasonStats
from services.scoring.pool import BASELINE_MIN_GP, baseline_records, load_baseline_pool

LAST = "2025-26"
PRIOR = "2024-25"


def _player(player_id: int, name: str | None = None) -> Player:
    return Player.create(
        id=player_id, name=name or f"P{player_id}", name_normalized=(name or f"p{player_id}").lower(),
        espn_id=9000 + player_id, position="G",
        created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    )


def _season(player_id: int, season: str, gp: int, pts: int, as_of: date | None = None) -> None:
    PlayerSeasonStats.create(
        player=player_id, team=None, season=season, gp=gp, pts=pts,
        as_of_date=as_of or (date(2026, 4, 12) if season == LAST else date(2025, 4, 13)),
        fpts=pts, reb=1, ast=1, stl=1, blk=1, tov=1, min=gp * 30,
        fgm=1, fga=1, fg3m=1, fg3a=1, ftm=1, fta=1,
    )


@pytest.mark.integration
def test_a_missed_season_falls_back_to_the_one_before(integration_db):
    """The Haliburton case: nothing at all in last season."""
    _player(1, "Missed Last Year")
    _season(1, PRIOR, gp=73, pts=1500)

    assert {r.player_id for r in baseline_records(LAST)} == set()
    walked = {r.player_id: r for r in baseline_records(LAST, walk_back=True)}
    assert walked[1].season == PRIOR and walked[1].gp == 73


@pytest.mark.integration
def test_a_sample_under_the_floor_falls_back_too(integration_db):
    """The Kessler case: five games last season, a real season before it. The
    GP floor stops being a cliff — falling below it means look further back."""
    _player(2, "Hurt Early")
    _season(2, LAST, gp=BASELINE_MIN_GP - 5, pts=200)
    _season(2, PRIOR, gp=57, pts=900)

    walked = {r.player_id: r for r in baseline_records(LAST, walk_back=True)}
    assert walked[2].season == PRIOR and walked[2].gp == 57


@pytest.mark.integration
def test_last_season_wins_whenever_it_qualifies(integration_db):
    """Walking back is a fallback, never a preference: a player with both keeps
    the newer season even when the older one was better."""
    _player(3)
    _season(3, LAST, gp=70, pts=1000)
    _season(3, PRIOR, gp=80, pts=2000)

    walked = {r.player_id: r for r in baseline_records(LAST, walk_back=True)}
    assert walked[3].season == LAST and walked[3].gp == 70


@pytest.mark.integration
def test_a_player_who_qualifies_in_no_season_stays_off_the_board(integration_db):
    """What replaces the arbitrary cliff: never having cleared the floor in any
    season is genuinely unknown, which is an honest reason to leave a player
    unvalued. Merely unlucky is not."""
    _player(4)
    _season(4, LAST, gp=3, pts=60)
    _season(4, PRIOR, gp=4, pts=80)

    assert {r.player_id for r in baseline_records(LAST, walk_back=True)} == set()


@pytest.mark.integration
def test_the_newest_snapshot_within_the_chosen_season_still_wins(integration_db):
    """Season ordering must not cost the within-season rule: rows land on every
    date a player's GP changed, and the latest is the season's final line."""
    _player(5)
    _season(5, PRIOR, gp=20, pts=300, as_of=date(2025, 1, 15))
    _season(5, PRIOR, gp=70, pts=1400, as_of=date(2025, 4, 13))

    walked = {r.player_id: r for r in baseline_records(LAST, walk_back=True)}
    assert walked[5].gp == 70


@pytest.mark.integration
def test_walk_back_is_opt_in_and_the_pool_carries_the_season(integration_db):
    """Off by default, because `load_baseline_pool` is also the in-season
    fallback behind PlayerValueService, where a two-year-old line is not what a
    rolling window is being asked for."""
    _player(6, "Played Last Year")
    _season(6, LAST, gp=70, pts=1000)
    _player(7, "Missed Last Year Too")
    _season(7, PRIOR, gp=60, pts=900)

    plain = {r.id: r for r in load_baseline_pool(LAST)}
    walked = {r.id: r for r in load_baseline_pool(LAST, walk_back=True)}

    assert set(plain) == {6}
    assert set(walked) == {6, 7}
    # The season rides along so a caller can say a value is a year older than
    # the rest of the board rather than presenting it as current.
    assert plain[6].season == LAST
    assert walked[7].season == PRIOR
