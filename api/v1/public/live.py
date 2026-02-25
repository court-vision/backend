"""
Live Game Data - Public Read Endpoints

Read-only endpoints for current in-game stats and scoreboard data.
No authentication required.
"""

from datetime import datetime, timedelta, date

import pytz
from fastapi import APIRouter, Request

from core.logging import get_logger
from core.rate_limit import limiter, PUBLIC_RATE_LIMIT
from db.models.nba.live_player_stats import LivePlayerStats
from db.models.nba.players import Player
from pipelines.extractors.nba_api import NBAApiExtractor

router = APIRouter(prefix="/live", tags=["Live"])
log = get_logger("live_api")


def _get_nba_date() -> date:
    """Return today's NBA game date in ET (before 6am = yesterday)."""
    eastern = pytz.timezone("US/Eastern")
    now_et = datetime.now(eastern)
    if now_et.hour < 6:
        return (now_et - timedelta(days=1)).date()
    return now_et.date()


@router.get("/players/today")
@limiter.limit(PUBLIC_RATE_LIMIT)
async def get_live_players_today(request: Request) -> dict:
    """
    Get live stats for all players with games today.

    Returns current box score stats for every player in an active or
    recently-completed game, ordered by fantasy points descending.
    Updated every ~60 seconds by the live polling pipeline.
    """
    game_date = _get_nba_date()
    log.debug("live_players_request", game_date=str(game_date))

    rows = (
        LivePlayerStats
        .select(LivePlayerStats, Player)
        .join(Player)
        .where(LivePlayerStats.game_date == game_date)
        .order_by(LivePlayerStats.fpts.desc())
    )

    players = [
        {
            "espn_id": row.player.espn_id,
            "player_id": row.player_id,
            "player_name": row.player.name,
            "game_id": row.game_id,
            "game_date": str(row.game_date),
            "game_status": row.game_status,
            "period": row.period,
            "game_clock": row.game_clock,
            "fpts": row.fpts,
            "pts": row.pts,
            "reb": row.reb,
            "ast": row.ast,
            "stl": row.stl,
            "blk": row.blk,
            "tov": row.tov,
            "min": row.min,
            "fgm": row.fgm,
            "fga": row.fga,
            "fg3m": row.fg3m,
            "fg3a": row.fg3a,
            "ftm": row.ftm,
            "fta": row.fta,
            "last_updated": row.last_updated.isoformat() if row.last_updated else None,
        }
        for row in rows
    ]

    return {
        "status": "success",
        "message": f"Live stats for {game_date}",
        "data": {
            "game_date": str(game_date),
            "player_count": len(players),
            "players": players,
        },
    }


@router.get("/schedule/today")
@limiter.limit(PUBLIC_RATE_LIMIT)
async def get_today_schedule(request: Request) -> dict:
    """
    Get game scheduling info for today's live polling.

    Returns the first tip-off time so the cron-runner's live loop can
    sleep until 30 minutes before the first game rather than relying on
    a hardcoded cron start time. Reads from the precomputed game schedule
    in the DB (populated by GameStartTimesPipeline).
    """
    game_date = _get_nba_date()
    log.debug("schedule_today_request", game_date=str(game_date))

    from db.models.nba.games import Game

    games = Game.get_games_on_date(game_date)
    if not games:
        return {
            "status": "success",
            "message": f"No games scheduled for {game_date}",
            "data": {
                "has_games": False,
                "game_date": str(game_date),
                "first_game_et": None,
                "wake_at_et": None,
            },
        }

    earliest_time = Game.get_earliest_game_time_on_date(game_date)
    if not earliest_time:
        # Games exist but start times aren't loaded — start immediately
        return {
            "status": "success",
            "message": f"Games scheduled for {game_date} but start times unavailable",
            "data": {
                "has_games": True,
                "game_date": str(game_date),
                "first_game_et": None,
                "wake_at_et": None,
            },
        }

    eastern = pytz.timezone("US/Eastern")
    first_game_naive = datetime.combine(game_date, earliest_time)
    first_game_et = eastern.localize(first_game_naive)
    wake_at_et = first_game_et - timedelta(minutes=30)

    return {
        "status": "success",
        "message": f"First game at {earliest_time} ET on {game_date}",
        "data": {
            "has_games": True,
            "game_date": str(game_date),
            "first_game_et": first_game_et.isoformat(),
            "wake_at_et": wake_at_et.isoformat(),
        },
    }


@router.get("/scoreboard")
@limiter.limit(PUBLIC_RATE_LIMIT)
async def get_live_scoreboard(request: Request) -> dict:
    """
    Get the current NBA scoreboard with live game statuses.

    Returns each active game's status, period, and game clock.
    Hits NBA's live CDN directly — reflects near-real-time game state.
    """
    game_date = _get_nba_date()
    log.debug("live_scoreboard_request", game_date=str(game_date))

    extractor = NBAApiExtractor()
    try:
        games = extractor.get_scoreboard_games(game_date)
    except Exception as e:
        log.error("live_scoreboard_error", error=str(e))
        return {
            "status": "error",
            "message": "Failed to fetch live scoreboard",
            "data": {"game_date": str(game_date), "games": []},
        }

    status_labels = {1: "scheduled", 2: "in_progress", 3: "final"}

    return {
        "status": "success",
        "message": f"Scoreboard for {game_date}",
        "data": {
            "game_date": str(game_date),
            "game_count": len(games),
            "games": [
                {
                    "game_id": g["game_id"],
                    "game_status": g["game_status"],
                    "game_status_label": status_labels.get(g["game_status"], "unknown"),
                    "period": g["period"],
                    "game_clock": g["game_clock"],
                }
                for g in games
            ],
        },
    }
