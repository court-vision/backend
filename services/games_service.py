"""
Service for game-related operations.

Today's slate carries each game's live score, status, period and clock. They
come from the live pipeline's newest score snapshot of the game
(nba.live_game_score_snapshots), which is the game as its live box score had it
within the last 30 seconds. They used to be read from the NBA's scoreboard file
on every request, and that file moves about once in ten minutes (the median
gap between changes was 600 s in every game of 2026-11-09), so the slate still
showed a tip time for minutes after the tip and held each score for ten.

The scoreboard is still asked when the snapshots cannot answer: before the
pipeline's window opens there are none, and one that has stopped moving for a
game not yet final means the pipeline is not running. Then each game takes
whichever of the two is further along. Neither is taken over a stored row that
is further along than both: a final the game log has written since stands.
"""

import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta

import pytz

from services.schedule_service import get_upcoming_games_on_date
from core.logging import get_logger
from db.models.nba.games import Game, STATUS_ORDER
from db.models.nba.live_game_score_snapshots import LatestSnapshot, LiveGameScoreSnapshot
from db.base import DB_RUNTIME_ERRORS, run_db
from services.providers.blocking import run_blocking_provider
from schemas.common import ApiStatus
from schemas.games import GamesOnDateResp, GamesOnDateData, GameInfo


# The shared 6 AM ET rule; the alias keeps this module's historical name.
from core.nba_calendar import nba_date_et as _get_nba_today  # noqa: E402


_GAME_STATUS_MAP = {1: "scheduled", 2: "in_progress", 3: "final"}

# The live pipeline snapshots every game it reads each 30 s. A game not yet
# final whose newest snapshot is older than this has gone six polls without
# one: the pipeline is down or cannot read it, and the scoreboard is asked.
SNAPSHOT_FRESH_SECONDS = 180


def needs_scoreboard(game_ids: list[str], snapshots: Mapping[str, LatestSnapshot], now: datetime) -> bool:
    """Whether the stored snapshots leave any of these games without a current word.

    `now` is UTC and naive, like `captured_at`. A final game's snapshot does
    not age: the pipeline stops reading a game once its final box score is
    stored, and the last snapshot taken is the final score.
    """
    for game_id in game_ids:
        snapshot = snapshots.get(game_id)
        if snapshot is None:
            return True
        if snapshot.game_status != 3 and (now - snapshot.captured_at).total_seconds() > SNAPSHOT_FRESH_SECONDS:
            return True
    return False


def _progress(status, period, home, away) -> tuple[int, int, int]:
    """How far along a game is: its status, then its period, then the points on the board."""
    return (status or 1, period or 0, (home or 0) + (away or 0))


_GAME_CLOCK = re.compile(r"^PT(\d+)M(\d+(?:\.\d+)?)S$")


def _clock_seconds(clock) -> float | None:
    """Seconds left in the period on an NBA game clock ("PT06M30.00S"); None for anything else."""
    match = _GAME_CLOCK.match(clock) if isinstance(clock, str) else None
    return int(match[1]) * 60 + float(match[2]) if match else None


def _further_along(listed: Mapping, snapshot: LatestSnapshot) -> bool:
    """Whether the scoreboard's entry is strictly later than the snapshot.

    Level on status, period and points in play (a stretch with no score), the
    one with less of the period left on its clock is the later. A clock that
    cannot be read settles nothing, and the snapshot stands.
    """
    theirs = _progress(listed.get("game_status"), listed.get("period"), listed.get("home_score"), listed.get("away_score"))
    ours = _progress(snapshot.game_status, snapshot.period, snapshot.home_score, snapshot.away_score)
    if theirs != ours:
        return theirs > ours
    if snapshot.game_status != 2:
        return False
    theirs_left, ours_left = _clock_seconds(listed.get("game_clock")), _clock_seconds(snapshot.game_clock)
    return theirs_left is not None and ours_left is not None and theirs_left < ours_left


def live_fields(game: Mapping, snapshot: LatestSnapshot | None, listed: Mapping | None) -> dict:
    """A stored game's live score, status, period and clock; nothing with no word on it.

    `snapshot` is the live pipeline's newest and `listed` the game's entry on
    the NBA scoreboard, when that was read. The snapshot is the fresher by
    minutes whenever the pipeline is running, so it stands unless the
    scoreboard is strictly further along.

    Nothing either when the stored row is further along than the word chosen.
    The game log closes a game with its final score whether or not the live
    pipeline saw the end of it, and a snapshot left mid-game by a pipeline that
    stopped would otherwise reopen it whenever the scoreboard could not be
    read, or no longer listed the game.
    """
    if listed is not None and (snapshot is None or _further_along(listed, snapshot)):
        # Before the tip the scoreboard says period 0 and clock "": null, as a snapshot has them.
        status, period, clock = listed["game_status"], listed["period"] or None, listed["game_clock"] or None
        home, away = listed["home_score"], listed["away_score"]
    elif snapshot is not None:
        status, period, clock = snapshot.game_status, snapshot.period, snapshot.game_clock
        home, away = snapshot.home_score, snapshot.away_score
    else:
        return {}
    status = _GAME_STATUS_MAP.get(status, game["status"])
    if STATUS_ORDER.get(status, 0) < STATUS_ORDER.get(game["status"], 0):
        return {}
    return {"home_score": home, "away_score": away, "status": status, "period": period, "game_clock": clock}


class GamesService:
    """Service for retrieving game information."""

    @staticmethod
    def _stored_games(game_date: date) -> list[dict]:
        return [
            {
                "game_id": g.game_id,
                "game_date": g.game_date.isoformat(),
                "home_team": g.home_team_id,
                "away_team": g.away_team_id,
                "home_score": g.home_score,
                "away_score": g.away_score,
                "status": g.status,
                "arena": g.arena,
                "period": None,
                "game_clock": None,
                "start_time_et": g.start_time_et.strftime("%H:%M") if g.start_time_et else None,
            }
            for g in Game.get_games_on_date(game_date)
        ]

    @staticmethod
    def _stored_today(game_date: date) -> tuple[list[dict], dict[str, LatestSnapshot]]:
        """Today's stored games and the live pipeline's newest snapshot of each, on one connection."""
        stored = GamesService._stored_games(game_date)
        try:
            snapshots = LiveGameScoreSnapshot.latest_by_game(game["game_id"] for game in stored)
        except DB_RUNTIME_ERRORS:
            raise
        except Exception as e:
            # The scoreboard can still answer for every game.
            get_logger().warning("live_snapshot_overlay_failed", error=str(e), date=game_date.isoformat())
            snapshots = {}
        return stored, snapshots

    @staticmethod
    async def get_games_on_date(game_date: date) -> GamesOnDateResp:
        """
        Get all games on a specific date.

        For today's NBA date, each game's live score, status, period and clock
        are overlaid on top of the DB records (which are written once at
        scheduling time and once when the game is complete), so callers get
        the freshest available state without a separate endpoint. See the
        module docstring for where they come from.

        Args:
            game_date: Date to query

        Returns:
            GamesOnDateResp with list of games
        """
        log = get_logger()

        try:
            today = game_date == _get_nba_today()
            snapshots: dict[str, LatestSnapshot] = {}
            if today:
                stored, snapshots = await run_db("games.on_date", GamesService._stored_today, game_date)
            else:
                stored = await run_db("games.on_date", GamesService._stored_games, game_date)

            if stored:
                scoreboard: dict[str, dict] = {}
                if today and needs_scoreboard([g["game_id"] for g in stored], snapshots, datetime.utcnow()):
                    from pipelines.extractors.nba_api import NBAApiExtractor
                    try:
                        extractor = NBAApiExtractor()
                        live_games = await run_blocking_provider(
                            "nba", "games_scoreboard", extractor.get_scoreboard_games, game_date
                        )
                        scoreboard = {g["game_id"]: g for g in live_games}
                    except Exception as e:
                        log.warning("live_scoreboard_overlay_failed", error=str(e), date=game_date.isoformat())
                        # Fall through with the snapshots there are (or the
                        # stale DB data) rather than failing the request

                game_list = [
                    GameInfo(**{**g, **live_fields(g, snapshots.get(g["game_id"]), scoreboard.get(g["game_id"]))})
                    if today
                    else GameInfo(**g)
                    for g in stored
                ]
            else:
                games = get_upcoming_games_on_date(game_date)
                game_list = [
                    GameInfo(
                        game_id=None,
                        game_date=game_date.isoformat(),
                        home_team=game["homeTeam"],
                        away_team=game["awayTeam"],
                        home_score=None,
                        away_score=None,
                        status="scheduled",
                        arena=None,
                        period=None,
                        game_clock=None,
                    )
                    for game in games
                    if game.get("homeTeam") and game.get("awayTeam")
                ]

            return GamesOnDateResp(
                status=ApiStatus.SUCCESS,
                message=f"Found {len(game_list)} games on {game_date.isoformat()}",
                data=GamesOnDateData(
                    date=game_date.isoformat(),
                    games=game_list,
                    count=len(game_list),
                ),
            )

        except DB_RUNTIME_ERRORS:
            raise
        except Exception as e:
            log.error("games_fetch_error", error=str(e), date=game_date.isoformat())
            return GamesOnDateResp(
                status=ApiStatus.ERROR,
                message="Failed to fetch games",
                data=None,
            )
