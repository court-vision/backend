"""One game's sheet after the fact: the score by period, the margin, and every line.

The live sheet (`/teams/{abbrev}/live-game` plus `/live/players/today`) only
knows tonight. Once a night is over this is the same game, frozen where it
stood at the final buzzer:

- **Lines** come from the game log the post-game run writes
  (`nba.player_game_stats`), the settled record. Until it lands, the live
  pipeline's last read of the box score (`nba.live_player_stats`) stands in;
  those rows last until the next night's first live run.
- **Score history** is the live pipeline's score snapshots, which data-platform
  keeps past their night (pre-tip snapshots excepted). A game whose night was
  wiped before they were kept has none, and its sheet draws no margin.
- **Score by period** is read from the league's box score feed: nothing stores
  it. The feed keeps a finished game's box score, so one read per game, cached
  with the rest of the body, is all a past game costs.

The game's score and status prefer the box score too, for the hours after the
6 AM ET date flip when `nba.games` can still be waiting on the post-game run.
"""

from __future__ import annotations

from datetime import date

from core.logging import get_logger
from db.base import DB_RUNTIME_ERRORS, run_db
from db.models.nba.games import Game
from db.models.nba.live_game_score_snapshots import LiveGameScoreSnapshot
from db.models.nba.live_player_stats import LivePlayerStats
from db.models.nba.player_game_stats import PlayerGameStats
from db.models.nba.player_season_stats import PlayerSeasonStats
from db.models.nba.players import Player
from schemas.common import ApiStatus
from schemas.games import GameDetailData, GameDetailResp, GameInfo, GameLine
from schemas.teams import GameScoreSnapshot
from services.providers.blocking import run_blocking_provider

from core.nba_calendar import nba_date_et as _get_nba_today  # noqa: E402


_GAME_STATUS_MAP = {1: "scheduled", 2: "in_progress", 3: "final"}
_STATS = ("fpts", "pts", "reb", "ast", "stl", "blk", "tov", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta")


def score_history(game_id: str) -> list[GameScoreSnapshot]:
    """The score from the tip to the final, oldest first, one point per change.

    Pre-tip snapshots are left out: the live pipeline polls from 15 minutes
    before the night's first tip, so the 9:30 PM game of 2026-10-20's slate
    (first tip 3 PM) carried 870 of them, 0-0 each, and its margin chart was
    three-quarters flat line.

    Of a run of identical snapshots only the first and the last are kept: the
    league's scoreboard feed, which the snapshots read, moved about every ten
    minutes that night, so a game's ~300 in-play snapshots held 15 distinct
    scores. Keeping both ends of each run keeps its time on the clock, so a
    chart drawn by `captured_at` is the same line, at a tenth of the body.
    """
    rows = [
        GameScoreSnapshot(
            captured_at=row.captured_at.isoformat(),
            period=row.period,
            game_clock=row.game_clock,
            home_score=row.home_score,
            away_score=row.away_score,
            game_status=row.game_status,
        )
        for row in (
            LiveGameScoreSnapshot.select()
            .where((LiveGameScoreSnapshot.game_id == game_id) & (LiveGameScoreSnapshot.game_status != 1))
            .order_by(LiveGameScoreSnapshot.captured_at.asc())
        )
    ]

    def same(a: GameScoreSnapshot, b: GameScoreSnapshot) -> bool:
        return (a.home_score, a.away_score, a.period, a.game_clock, a.game_status) == (
            b.home_score, b.away_score, b.period, b.game_clock, b.game_status,
        )

    return [
        snap
        for i, snap in enumerate(rows)
        if i == 0 or i == len(rows) - 1 or not same(snap, rows[i - 1]) or not same(snap, rows[i + 1])
    ]


def line_score(box: dict | None) -> tuple[list[int], list[int]]:
    """(home, away) points per period from a live box score; empty without one."""
    if not box:
        return [], []

    def side(key: str) -> list[int]:
        return [int(p.get("score") or 0) for p in (box.get(key) or {}).get("periods") or []]

    return side("homeTeam"), side("awayTeam")


def _box_teams(box: dict | None) -> dict[int, str]:
    """Player id -> the tricode of the side the box score lists them on."""
    out: dict[int, str] = {}
    for key in ("homeTeam", "awayTeam"):
        team = (box or {}).get(key) or {}
        tricode = team.get("teamTricode")
        for p in team.get("players") or []:
            if p.get("personId") and tricode:
                out[int(p["personId"])] = tricode
    return out


def _line(row, team: str | None, plus_minus: int | None) -> GameLine:
    return GameLine(
        player_id=row.player_id,
        espn_id=row.player.espn_id,
        player_name=row.player.name,
        team=team,
        min=row.min,
        plus_minus=plus_minus,
        **{k: getattr(row, k) for k in _STATS},
    )


class GameDetailService:
    @staticmethod
    def _load(game_id: str) -> dict | None:
        game = Game.get_or_none(Game.game_id == game_id)
        if game is None:
            return None
        settled = list(
            PlayerGameStats.select(PlayerGameStats, Player)
            .join(Player)
            .where(PlayerGameStats.game == game_id)
        )
        live = [] if settled else list(
            LivePlayerStats.select(LivePlayerStats, Player)
            .join(Player)
            .where(LivePlayerStats.game_id == game_id)
        )
        # Who a live row played for, when the box score cannot say: each
        # player's latest team this season (last season's before it starts).
        season_teams: dict[int, str] = {}
        if live:
            season_teams = {
                row.player_id: row.team_id
                for row in PlayerSeasonStats.latest_per_player(PlayerSeasonStats.available_season())
                .where(PlayerSeasonStats.team.in_((game.home_team_id, game.away_team_id)))
            }
        return {
            "game": {
                "game_id": game.game_id,
                "game_date": game.game_date,
                "home_team": game.home_team_id,
                "away_team": game.away_team_id,
                "home_score": game.home_score,
                "away_score": game.away_score,
                "status": game.status,
                "arena": game.arena,
                "start_time_et": game.start_time_et.strftime("%H:%M") if game.start_time_et else None,
            },
            "settled": settled,
            "live": live,
            "season_teams": season_teams,
            "score_history": score_history(game_id),
        }

    @staticmethod
    async def get_game(game_date: date, game_id: str) -> GameDetailResp:
        log = get_logger()
        try:
            ctx = await run_db("games.detail", GameDetailService._load, game_id)
            if ctx is None or ctx["game"]["game_date"] != game_date:
                return GameDetailResp(
                    status=ApiStatus.NOT_FOUND,
                    message=f"No game {game_id} on {game_date.isoformat()}",
                    data=None,
                )
            game = ctx["game"]

            # A game not yet due has no box score to read.
            box: dict | None = None
            if game_date <= _get_nba_today():
                try:
                    from pipelines.extractors.nba_api import NBAApiExtractor

                    box = await run_blocking_provider(
                        "nba", "live_box_score", NBAApiExtractor().get_live_box_score, game_id
                    )
                except DB_RUNTIME_ERRORS:
                    raise
                except Exception as exc:
                    log.warning("game_detail_box_score_failed", error=str(exc), game_id=game_id)

            info = {**game, "game_date": game_date.isoformat(), "period": None, "game_clock": None}
            if box:
                home, away = box.get("homeTeam") or {}, box.get("awayTeam") or {}
                status = _GAME_STATUS_MAP.get(box.get("gameStatus"), game["status"])
                info.update(
                    home_score=home.get("score", game["home_score"]),
                    away_score=away.get("score", game["away_score"]),
                    status=status,
                    period=box.get("period") or None,
                    game_clock=(box.get("gameClock") or None) if status == "in_progress" else None,
                )
            home_periods, away_periods = line_score(box)

            if ctx["settled"]:
                source = "settled"
                lines = [_line(row, row.team_id, row.plus_minus) for row in ctx["settled"]]
            elif ctx["live"]:
                source = "live"
                teams = {**ctx["season_teams"], **_box_teams(box)}
                lines = [_line(row, teams.get(row.player_id), None) for row in ctx["live"]]
            else:
                source, lines = None, []
            lines.sort(key=lambda line: (-line.fpts, -line.pts, line.player_name))

            return GameDetailResp(
                status=ApiStatus.SUCCESS,
                message=f"{game['away_team']} at {game['home_team']}, {game_date.isoformat()}",
                data=GameDetailData(
                    game=GameInfo(**info),
                    home_periods=home_periods,
                    away_periods=away_periods,
                    score_history=ctx["score_history"],
                    lines=lines,
                    lines_source=source,
                ),
            )
        except DB_RUNTIME_ERRORS:
            raise
        except Exception as exc:
            log.error("game_detail_fetch_error", error=str(exc), game_id=game_id)
            return GameDetailResp(status=ApiStatus.ERROR, message="Failed to fetch the game", data=None)
