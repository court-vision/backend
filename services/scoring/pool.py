"""
Player pools for category ranking and category value: per-game PoolRows built
from the stored stat tables.

- load_pool(window):      the latest fresh rolling snapshot (7/14/30) or each
                          player's latest row of the current season.
- load_baseline_pool():   every player's final row of the previous season
                          (gp >= BASELINE_MIN_GP), the fallback while the current
                          season has no data yet.

Rows carry the player's ESPN id and normalized name so callers can key values
by either without a second lookup.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from db.models.nba.player_rolling_stats import PlayerRollingStats
from db.models.nba.player_season_stats import PlayerSeasonStats
from db.models.nba.players import Player
from services.scoring.category_rank import PoolRow
from services.scoring.models import StatLine

# Last season's final row only counts as a baseline with a real sample behind it
BASELINE_MIN_GP = 10


def baseline_season(season: Optional[str] = None) -> str:
    """The season a baseline is drawn from: `season`, or the one before the configured season."""
    from core.season import previous_season
    from core.settings import settings

    return season or previous_season(settings.nba_season)


def baseline_records(season: Optional[str] = None, where=None):
    """Each player's final PlayerSeasonStats row of `season` (gp >= BASELINE_MIN_GP), Player joined."""
    cond = (PlayerSeasonStats.season == baseline_season(season)) & (PlayerSeasonStats.gp >= BASELINE_MIN_GP)
    if where is not None:
        cond = cond & where
    return (
        PlayerSeasonStats.select(PlayerSeasonStats, Player)
        .join(Player)
        .where(cond)
        .distinct([PlayerSeasonStats.player])
        .order_by(PlayerSeasonStats.player, PlayerSeasonStats.as_of_date.desc())
    )


def _season_row(rec, gp: int) -> PoolRow:
    """A season-totals row as a per-game PoolRow."""
    line = StatLine.from_row(rec).scaled(1 / gp)
    line.gp = 1.0
    fpts_total = float(rec.fpts or 0)
    return PoolRow(
        id=rec.player_id, name=rec.player.name, team=rec.team_id, gp=gp, line=line,
        fpts_avg=round(fpts_total / gp, 2), fpts_total=fpts_total,
        espn_id=rec.player.espn_id, name_normalized=rec.player.name_normalized,
    )


def load_pool(window: Optional[int], season: Optional[str] = None) -> tuple[Optional[date], list[PoolRow]]:
    """Per-game pool rows for a window: the latest fresh rolling snapshot, or each
    player's latest season snapshot converted from totals to per-game.

    A stale rolling snapshot (last season's L14 read in October) is reported as
    an empty pool. `season` scopes the season path; by default it is the season
    of the newest season-stats row (as the nba.rankings view does).
    """
    if window is not None:
        latest_date, records = PlayerRollingStats.get_latest_for_window(window)
        pool: list[PoolRow] = []
        for rec in records:
            gp = int(rec.gp or 0)
            if gp < 1:
                continue
            fpts_avg = float(rec.fpts)
            pool.append(PoolRow(
                id=rec.player_id, name=rec.player.name, team=rec.team_id, gp=gp,
                line=StatLine.from_row(rec, gp=1.0),
                fpts_avg=fpts_avg, fpts_total=round(fpts_avg * gp, 1),
                espn_id=rec.player.espn_id, name_normalized=rec.player.name_normalized,
            ))
        return latest_date, pool

    # Season rows are only written on days a player's GP changes, so take each
    # player's latest row within the season rather than a single as_of_date.
    target_season = season or (
        PlayerSeasonStats.select(PlayerSeasonStats.season)
        .order_by(PlayerSeasonStats.as_of_date.desc())
        .limit(1)
        .scalar()
    )
    if not target_season:
        return None, []

    records = (
        PlayerSeasonStats.select(PlayerSeasonStats, Player)
        .join(Player)
        .where(PlayerSeasonStats.season == target_season)
        .distinct([PlayerSeasonStats.player])
        .order_by(PlayerSeasonStats.player, PlayerSeasonStats.as_of_date.desc())
    )

    as_of: Optional[date] = None
    pool = []
    for rec in records:
        gp = int(rec.gp or 0)
        if gp < 1:
            continue
        if as_of is None or rec.as_of_date > as_of:
            as_of = rec.as_of_date
        pool.append(_season_row(rec, gp))
    return as_of, pool


def load_baseline_pool(season: Optional[str] = None) -> list[PoolRow]:
    """Every player's final previous-season row (gp >= BASELINE_MIN_GP) as per-game PoolRows."""
    pool: list[PoolRow] = []
    for rec in baseline_records(season):
        gp = int(rec.gp or 0)
        if gp < 1:
            continue
        pool.append(_season_row(rec, gp))
    return pool
