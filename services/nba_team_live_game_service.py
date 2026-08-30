"""NBA team live/upcoming game data with isolated DB and provider boundaries."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytz

from core.logging import get_logger
from db.base import DB_RUNTIME_ERRORS, run_db
from db.models.nba.games import Game
from db.models.nba.teams import NBATeam
from db.models.nba.players import Player
from db.models.nba.player_season_stats import PlayerSeasonStats
from db.models.nba.live_player_stats import LivePlayerStats
from db.models.nba.live_game_score_snapshots import LiveGameScoreSnapshot
from db.models.nba.player_injuries import PlayerInjury
from schemas.common import ApiStatus
from schemas.teams import (
    NBATeamLiveGameResp,
    NBATeamLiveGameData,
    GameScoreSnapshot,
    TopPerformer,
    InjuredPlayer,
)
from services.providers.blocking import run_blocking_provider


from core.nba_calendar import nba_date_et as _get_nba_today  # noqa: E402


_GAME_STATUS_MAP = {1: "scheduled", 2: "in_progress", 3: "final"}


def _game_dict(game) -> dict:
    return {
        "game_id": game.game_id,
        "game_date": game.game_date,
        "home_team": game.home_team_id,
        "away_team": game.away_team_id,
        "home_score": game.home_score,
        "away_score": game.away_score,
        "status": game.status,
        "start_time_et": game.start_time_et,
        "arena": game.arena,
    }


def _get_team_player_ids(team_id: str) -> set[int]:
    latest_date = (
        PlayerSeasonStats.select(PlayerSeasonStats.as_of_date)
        .where(PlayerSeasonStats.team == team_id)
        .order_by(PlayerSeasonStats.as_of_date.desc())
        .limit(1)
        .scalar()
    )
    if not latest_date:
        return set()
    return {
        row.player_id
        for row in PlayerSeasonStats.select(PlayerSeasonStats.player).where(
            (PlayerSeasonStats.team == team_id) & (PlayerSeasonStats.as_of_date == latest_date)
        )
    }


def _get_injured_players(player_ids: set[int]) -> list[InjuredPlayer]:
    if not player_ids:
        return []
    names = {row.id: row.name for row in Player.select(Player.id, Player.name).where(Player.id.in_(player_ids))}
    return [
        InjuredPlayer(
            player_id=injury.player_id,
            name=names.get(injury.player_id, "Unknown"),
            status=injury.status,
            injury_type=injury.injury_type,
            expected_return=injury.expected_return.isoformat() if injury.expected_return else None,
        )
        for injury in PlayerInjury.get_injured_players()
        if injury.player_id in player_ids
    ]


def _live_top_performers(game_id: str, player_ids: set[int], limit: int = 15) -> list[TopPerformer]:
    rows = list(
        LivePlayerStats.select(LivePlayerStats, Player)
        .join(Player)
        .where((LivePlayerStats.game_id == game_id) & (LivePlayerStats.player_id.in_(player_ids)))
        .order_by(LivePlayerStats.pts.desc())
        .limit(limit)
    )
    return [
        TopPerformer(
            player_id=row.player_id,
            name=row.player.name,
            pts=row.pts,
            reb=row.reb,
            ast=row.ast,
            stl=row.stl,
            blk=row.blk,
            min=row.min,
            fgm=row.fgm,
            fga=row.fga,
            fg3m=row.fg3m,
        )
        for row in rows
    ]


class NBATeamLiveGameService:
    @staticmethod
    def _load_context(team_id: str, nba_today: date) -> dict | None:
        team = NBATeam.get_or_none(NBATeam.id == team_id)
        if team is None:
            return None
        player_ids = _get_team_player_ids(team_id)
        today_games = Game.get_team_games(team_id=team_id, start_date=nba_today, end_date=nba_today)
        all_games = [] if today_games else Game.get_team_games(team_id=team_id)
        return {
            "team_name": team.name,
            "player_ids": player_ids,
            "today_game": _game_dict(today_games[0]) if today_games else None,
            "all_games": [_game_dict(game) for game in all_games],
            "injured": _get_injured_players(player_ids),
        }

    @staticmethod
    def _latest_live_snapshot(game_id: str):
        snapshot = LiveGameScoreSnapshot.get_latest_for_game(game_id)
        if snapshot is None or not LiveGameScoreSnapshot.is_game_live(game_id):
            return None
        return SimpleNamespace(
            home_score=snapshot.home_score,
            away_score=snapshot.away_score,
            period=snapshot.period,
            game_clock=snapshot.game_clock,
            game_status=snapshot.game_status,
        )

    @staticmethod
    def _live_extras(game: dict, status: str, team_id: str, player_ids: set[int]) -> dict:
        home_performers: list[TopPerformer] = []
        away_performers: list[TopPerformer] = []
        if game["game_id"] and status in ("in_progress", "final"):
            home_performers = _live_top_performers(game["game_id"], _get_team_player_ids(game["home_team"]))
            away_performers = _live_top_performers(game["game_id"], _get_team_player_ids(game["away_team"]))
        score_history: list[GameScoreSnapshot] = []
        if game["game_id"]:
            score_history = [
                GameScoreSnapshot(
                    captured_at=row.captured_at.isoformat(),
                    period=row.period,
                    game_clock=row.game_clock,
                    home_score=row.home_score,
                    away_score=row.away_score,
                    game_status=row.game_status,
                )
                for row in LiveGameScoreSnapshot.get_snapshots_for_game(game["game_id"])
            ]
        return {
            "home_performers": home_performers,
            "away_performers": away_performers,
            "injured": _get_injured_players(player_ids) if status == "scheduled" else [],
            "score_history": score_history,
        }

    @staticmethod
    async def get_live_game(team_abbrev: str) -> NBATeamLiveGameResp:
        log = get_logger()
        team_id = team_abbrev.upper()
        nba_today = _get_nba_today()
        try:
            context = await run_db("nba_team.live_context", NBATeamLiveGameService._load_context, team_id, nba_today)
            if context is None:
                return NBATeamLiveGameResp(
                    status=ApiStatus.NOT_FOUND, message=f"Team '{team_id}' not found", data=None
                )

            game = context["today_game"]
            if game is not None:
                home_score, away_score = game["home_score"], game["away_score"]
                status = game["status"]
                period = game_clock = None
                home_periods: list[int] = []
                away_periods: list[int] = []
                if status in ("in_progress", "scheduled"):
                    try:
                        from pipelines.extractors.nba_api import NBAApiExtractor

                        box = await run_blocking_provider(
                            "nba", "live_box_score", NBAApiExtractor().get_live_box_score, game["game_id"]
                        )
                        if box:
                            home = box.get("homeTeam", {})
                            away = box.get("awayTeam", {})
                            home_score = home.get("score", home_score)
                            away_score = away.get("score", away_score)
                            period = box.get("period")
                            game_clock = box.get("gameClock")
                            status = _GAME_STATUS_MAP.get(box.get("gameStatus", 1), status)
                            home_periods = [row.get("score", 0) for row in home.get("periods", [])]
                            away_periods = [row.get("score", 0) for row in away.get("periods", [])]
                        else:
                            snapshot = await run_db(
                                "nba_team.live_snapshot", NBATeamLiveGameService._latest_live_snapshot, game["game_id"]
                            )
                            if snapshot:
                                home_score, away_score = snapshot.home_score, snapshot.away_score
                                period, game_clock = snapshot.period, snapshot.game_clock
                                status = _GAME_STATUS_MAP.get(snapshot.game_status, status)
                    except DB_RUNTIME_ERRORS:
                        raise
                    except Exception as exc:
                        log.warning("live_box_score_fetch_failed", error=str(exc), game_id=game["game_id"])
                        snapshot = await run_db(
                            "nba_team.live_snapshot", NBATeamLiveGameService._latest_live_snapshot, game["game_id"]
                        )
                        if snapshot:
                            home_score, away_score = snapshot.home_score, snapshot.away_score
                            period, game_clock = snapshot.period, snapshot.game_clock
                            status = _GAME_STATUS_MAP.get(snapshot.game_status, status)

                extras = await run_db(
                    "nba_team.live_extras", NBATeamLiveGameService._live_extras,
                    game, status, team_id, context["player_ids"],
                )
                return NBATeamLiveGameResp(
                    status=ApiStatus.SUCCESS,
                    message=f"Today's game for {context['team_name']}",
                    data=NBATeamLiveGameData(
                        game_id=game["game_id"], game_date=game["game_date"].isoformat(),
                        home_team=game["home_team"], away_team=game["away_team"],
                        home_score=home_score, away_score=away_score, status=status,
                        period=period, game_clock=game_clock,
                        start_time_et=game["start_time_et"].strftime("%H:%M") if game["start_time_et"] else None,
                        arena=game["arena"], home_periods=home_periods, away_periods=away_periods,
                        home_top_performers=extras["home_performers"],
                        away_top_performers=extras["away_performers"],
                        injured_players=extras["injured"], is_today=True,
                        is_upcoming=status == "scheduled", score_history=extras["score_history"],
                    ),
                )

            upcoming = [g for g in context["all_games"] if g["status"] == "scheduled" and g["game_date"] > nba_today]
            if upcoming:
                game = upcoming[0]
                return NBATeamLiveGameResp(
                    status=ApiStatus.SUCCESS, message=f"Next game for {context['team_name']}",
                    data=NBATeamLiveGameData(
                        game_id=game["game_id"], game_date=game["game_date"].isoformat(),
                        home_team=game["home_team"], away_team=game["away_team"], status=game["status"],
                        start_time_et=game["start_time_et"].strftime("%H:%M") if game["start_time_et"] else None,
                        arena=game["arena"], injured_players=context["injured"], is_today=False, is_upcoming=True,
                    ),
                )
            past = [g for g in context["all_games"] if g["status"] == "final" and g["game_date"] < nba_today]
            if past:
                game = past[-1]
                return NBATeamLiveGameResp(
                    status=ApiStatus.SUCCESS, message=f"Most recent game for {context['team_name']}",
                    data=NBATeamLiveGameData(
                        game_id=game["game_id"], game_date=game["game_date"].isoformat(),
                        home_team=game["home_team"], away_team=game["away_team"],
                        home_score=game["home_score"], away_score=game["away_score"], status=game["status"],
                        start_time_et=game["start_time_et"].strftime("%H:%M") if game["start_time_et"] else None,
                        arena=game["arena"], is_today=False, is_upcoming=False,
                    ),
                )
            return NBATeamLiveGameResp(
                status=ApiStatus.NOT_FOUND, message=f"No games found for {context['team_name']}", data=None
            )
        except DB_RUNTIME_ERRORS:
            raise
        except Exception as exc:
            log.error("nba_team_live_game_fetch_error", error=str(exc), team=team_id)
            return NBATeamLiveGameResp(status=ApiStatus.ERROR, message="Failed to fetch game data", data=None)
