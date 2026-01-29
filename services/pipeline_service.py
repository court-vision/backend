"""
Pipeline Service

Consolidates all ETL pipeline logic for scheduled data ingestion tasks.
Can be triggered via API endpoints or cron script.

Features:
- Structured logging with correlation IDs
- Retry logic with exponential backoff
- Circuit breakers for external APIs
- Pipeline run tracking for idempotency
"""

import json
import traceback
import unicodedata
import uuid
from datetime import datetime, timedelta
from typing import Optional

import pytz
import requests
from peewee import fn

from core.logging import get_logger
from core.settings import settings
from core.resilience import (
    with_retry,
    nba_api_circuit,
    espn_api_circuit,
    NetworkError,
    RateLimitError,
    ServerError,
)
from db.models.stats.daily_player_stats import DailyPlayerStats
from db.models.stats.cumulative_player_stats import CumulativePlayerStats
from db.models.stats.daily_matchup_score import DailyMatchupScore
from db.models.pipeline_run import PipelineRun
from db.models.teams import Team
from services.schedule_service import get_matchup_dates
from schemas.pipeline import PipelineResult
from schemas.common import ApiStatus


# ESPN API Configuration (from settings)
ESPN_FANTASY_ENDPOINT = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/{}/segments/0/leagues/{}"


def _normalize_name(name: str) -> str:
    """Normalize a name by removing diacritics and converting to lowercase."""
    normalized = unicodedata.normalize("NFD", name)
    ascii_name = "".join(c for c in normalized if unicodedata.category(c) != "Mn")
    return ascii_name.lower().strip()


@with_retry(
    max_attempts=settings.retry_max_attempts,
    base_delay=settings.retry_base_delay,
    max_delay=settings.retry_max_delay,
)
@espn_api_circuit
def _get_espn_player_data(year: int, league_id: int) -> dict:
    """
    Fetch ESPN player data including ESPN ID and roster percentage.

    Wrapped with retry logic and circuit breaker for resilience.
    """
    log = get_logger("espn_api")

    params = {"view": "kona_player_info", "scoringPeriodId": 0}
    endpoint = ESPN_FANTASY_ENDPOINT.format(year, league_id)
    filters = {
        "players": {
            "filterSlotIds": {"value": []},
            "limit": 750,
            "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
            "sortDraftRanks": {"sortPriority": 2, "sortAsc": True, "value": "STANDARD"},
        }
    }
    headers = {"x-fantasy-filter": json.dumps(filters)}

    log.debug("espn_request_start", endpoint=endpoint)

    try:
        response = requests.get(
            endpoint,
            params=params,
            headers=headers,
            timeout=settings.http_timeout
        )

        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 60))
            raise RateLimitError(f"ESPN rate limited", retry_after=retry_after)

        if response.status_code >= 500:
            raise ServerError(f"ESPN server error", status_code=response.status_code)

        response.raise_for_status()
        data = response.json()

    except requests.exceptions.Timeout:
        raise NetworkError("ESPN request timed out")
    except requests.exceptions.ConnectionError:
        raise NetworkError("ESPN connection failed")

    players = data.get("players", [])
    players = [x.get("player", x) for x in players]

    cleaned_data = {}
    for player in players:
        if player and "fullName" in player:
            normalized = _normalize_name(player["fullName"])
            cleaned_data[normalized] = {
                "espn_id": player["id"],
                "rost_pct": player.get("ownership", {}).get("percentOwned", 0),
            }

    log.info("espn_request_complete", player_count=len(cleaned_data))
    return cleaned_data


@with_retry(
    max_attempts=settings.retry_max_attempts,
    base_delay=settings.retry_base_delay,
    max_delay=settings.retry_max_delay,
)
@nba_api_circuit
def _fetch_nba_game_logs(date_str: str, season: str):
    """
    Fetch player game logs from NBA API.

    Wrapped with retry logic and circuit breaker for resilience.
    """
    log = get_logger("nba_api")

    from nba_api.stats.endpoints import playergamelogs
    import pandas as pd

    log.debug("nba_game_logs_start", date=date_str, season=season)

    try:
        game_logs = playergamelogs.PlayerGameLogs(
            date_from_nullable=date_str,
            date_to_nullable=date_str,
            season_nullable=season,
        )
        stats = game_logs.player_game_logs.get_data_frame()

        log.info("nba_game_logs_complete", record_count=len(stats))
        return stats

    except Exception as e:
        # NBA API uses requests internally, so catch and re-raise as our types
        error_str = str(e).lower()
        if "timeout" in error_str:
            raise NetworkError(f"NBA API timeout: {e}")
        if "connection" in error_str:
            raise NetworkError(f"NBA API connection error: {e}")
        raise


@with_retry(
    max_attempts=settings.retry_max_attempts,
    base_delay=settings.retry_base_delay,
    max_delay=settings.retry_max_delay,
)
@nba_api_circuit
def _fetch_nba_league_leaders():
    """
    Fetch league leaders from NBA API.

    Wrapped with retry logic and circuit breaker for resilience.
    """
    log = get_logger("nba_api")

    from nba_api.stats.endpoints import leagueleaders

    log.debug("nba_leaders_start", season=settings.nba_season)

    try:
        leaders = leagueleaders.LeagueLeaders(
            season=settings.nba_season,
            per_mode48="Totals",
            stat_category_abbreviation="PTS"
        )
        api_data = leaders.get_normalized_dict()["LeagueLeaders"]

        log.info("nba_leaders_complete", player_count=len(api_data))
        return api_data

    except Exception as e:
        error_str = str(e).lower()
        if "timeout" in error_str:
            raise NetworkError(f"NBA API timeout: {e}")
        if "connection" in error_str:
            raise NetworkError(f"NBA API connection error: {e}")
        raise


def _calculate_fantasy_points(stats: dict) -> int:
    """Calculate fantasy points using the league scoring formula."""
    points_score = stats["pts"]
    rebounds_score = stats["reb"]
    assists_score = stats["ast"] * 2
    stocks_score = (stats["stl"] + stats["blk"]) * 4
    turnovers_score = stats["tov"] * -2
    three_pointers_score = stats["fg3m"]
    fg_eff_score = (stats["fgm"] * 2) - stats["fga"]
    ft_eff_score = stats["ftm"] - stats["fta"]

    return int(
        points_score
        + rebounds_score
        + assists_score
        + stocks_score
        + turnovers_score
        + three_pointers_score
        + fg_eff_score
        + ft_eff_score
    )


def _minutes_to_int(min_str) -> int:
    """Convert minutes from MM:SS format to integer minutes."""
    if isinstance(min_str, (int, float)):
        return int(min_str)
    if ":" in str(min_str):
        parts = str(min_str).split(":")
        return int(parts[0])
    return int(min_str) if min_str else 0


def _get_current_matchup_info(current_date) -> Optional[dict]:
    """Determine current matchup period and day index from schedule."""
    for matchup_num in range(1, 25):  # Assume max 24 matchup periods
        try:
            dates = get_matchup_dates(matchup_num)
            if dates:
                start_date, end_date = dates
                if start_date <= current_date <= end_date:
                    return {
                        "matchup_number": matchup_num,
                        "start_date": start_date,
                        "end_date": end_date,
                        "day_index": (current_date - start_date).days,
                    }
        except Exception:
            break
    return None


class PipelineService:
    """Service for running data pipelines with tracking and resilience."""

    @staticmethod
    async def run_daily_player_stats() -> PipelineResult:
        """
        Fetch yesterday's game stats from NBA API and insert into daily_player_stats.
        """
        log = get_logger("pipeline").bind(pipeline="daily_player_stats")
        central_tz = pytz.timezone("US/Central")
        started_at = datetime.now(central_tz)
        records_processed = 0

        # Start pipeline run tracking
        run = PipelineRun.start_run("daily_player_stats")
        run_id = run.id
        log = log.bind(run_id=str(run_id))
        log.info("pipeline_started")

        try:
            import pandas as pd

            yesterday = started_at - timedelta(days=1)
            game_date = yesterday.date()
            date_str = yesterday.strftime("%m/%d/%Y")

            # Determine season string
            season = f"{yesterday.year}-{str(yesterday.year + 1)[-2:]}"
            if yesterday.month < 8:
                season = f"{yesterday.year - 1}-{str(yesterday.year)[-2:]}"

            log.info("fetching_data", date=date_str, season=season)

            # Fetch ESPN player data for roster percentages (with retries)
            espn_data = _get_espn_player_data(settings.espn_year, settings.espn_league_id)
            log.info("espn_data_fetched", player_count=len(espn_data))

            # Fetch NBA game logs (with retries)
            stats = _fetch_nba_game_logs(date_str, season)

            if stats.empty:
                log.info("no_games_found", date=date_str)
                run.mark_success(records_processed=0)
                return PipelineResult(
                    status=ApiStatus.SUCCESS,
                    message=f"No games found for {date_str}",
                    started_at=started_at.isoformat(),
                    completed_at=datetime.now(central_tz).isoformat(),
                    records_processed=0,
                )

            log.info("nba_data_fetched", record_count=len(stats))

            # Process each player
            for _, row in stats.iterrows():
                minutes_value = row["MIN"]
                if pd.isna(minutes_value) or minutes_value == "" or minutes_value is None:
                    continue

                minutes_int = _minutes_to_int(minutes_value)
                if minutes_int == 0:
                    continue

                player_name = row["PLAYER_NAME"]
                normalized_name = _normalize_name(player_name)
                espn_info = espn_data.get(normalized_name)
                espn_id = espn_info["espn_id"] if espn_info else None
                rost_pct = espn_info["rost_pct"] if espn_info else None

                player_stats = {
                    "pts": int(row["PTS"]),
                    "reb": int(row["REB"]),
                    "ast": int(row["AST"]),
                    "stl": int(row["STL"]),
                    "blk": int(row["BLK"]),
                    "tov": int(row["TOV"]),
                    "fgm": int(row["FGM"]),
                    "fga": int(row["FGA"]),
                    "fg3m": int(row["FG3M"]),
                    "fg3a": int(row["FG3A"]),
                    "ftm": int(row["FTM"]),
                    "fta": int(row["FTA"]),
                }
                fpts = _calculate_fantasy_points(player_stats)

                DailyPlayerStats.create(
                    id=int(row["PLAYER_ID"]),
                    espn_id=espn_id,
                    name=player_name,
                    name_normalized=normalized_name,
                    team=row["TEAM_ABBREVIATION"],
                    date=game_date,
                    fpts=fpts,
                    min=minutes_int,
                    rost_pct=rost_pct,
                    pipeline_run_id=run_id,
                    **player_stats,
                )
                records_processed += 1

            completed_at = datetime.now(central_tz)
            run.mark_success(records_processed=records_processed)

            log.info(
                "pipeline_completed",
                records_processed=records_processed,
                duration_seconds=(completed_at - started_at).total_seconds(),
            )

            return PipelineResult(
                status=ApiStatus.SUCCESS,
                message=f"Daily player stats completed for {date_str}",
                started_at=started_at.isoformat(),
                completed_at=completed_at.isoformat(),
                duration_seconds=(completed_at - started_at).total_seconds(),
                records_processed=records_processed,
            )

        except Exception as e:
            completed_at = datetime.now(central_tz)
            error_msg = f"{type(e).__name__}: {str(e)}"
            run.mark_failed(error_msg)

            log.error(
                "pipeline_failed",
                error=error_msg,
                traceback=traceback.format_exc(),
            )

            return PipelineResult(
                status=ApiStatus.ERROR,
                message="Daily player stats failed",
                started_at=started_at.isoformat(),
                completed_at=completed_at.isoformat(),
                duration_seconds=(completed_at - started_at).total_seconds(),
                error=f"{error_msg}\n{traceback.format_exc()}",
            )

    @staticmethod
    async def run_cumulative_player_stats() -> PipelineResult:
        """
        Update cumulative season stats and rankings for players who played.
        """
        log = get_logger("pipeline").bind(pipeline="cumulative_player_stats")
        central_tz = pytz.timezone("US/Central")
        started_at = datetime.now(central_tz)
        records_processed = 0

        # Start pipeline run tracking
        run = PipelineRun.start_run("cumulative_player_stats")
        run_id = run.id
        log = log.bind(run_id=str(run_id))
        log.info("pipeline_started")

        try:
            yesterday = started_at - timedelta(days=1)
            game_date = yesterday.date()

            log.info("fetching_data", date=str(game_date))

            # Fetch ESPN rostered data (with retries)
            espn_data = _get_espn_player_data(settings.espn_year, settings.espn_league_id)
            log.info("espn_data_fetched", player_count=len(espn_data))

            # Fetch NBA league leaders (with retries)
            api_data = _fetch_nba_league_leaders()
            log.info("nba_data_fetched", player_count=len(api_data))

            # Get latest GP for each player from database
            subquery = CumulativePlayerStats.select(
                CumulativePlayerStats.id,
                fn.MAX(CumulativePlayerStats.date).alias("max_date"),
            ).group_by(CumulativePlayerStats.id)

            latest_records = (
                CumulativePlayerStats.select(
                    CumulativePlayerStats.id, CumulativePlayerStats.gp
                ).join(
                    subquery,
                    on=(
                        (CumulativePlayerStats.id == subquery.c.id)
                        & (CumulativePlayerStats.date == subquery.c.max_date)
                    ),
                )
            )
            db_gp_map = {record.id: record.gp for record in latest_records}

            # Find players who played (GP changed)
            entries = {}
            for player in api_data:
                player_id = player["PLAYER_ID"]
                current_gp = player["GP"]

                # Skip if player hasn't played new games
                if player_id in db_gp_map and current_gp == db_gp_map[player_id]:
                    continue

                player_name = player["PLAYER"]
                normalized_name = _normalize_name(player_name)
                rost_pct = espn_data.get(normalized_name, {}).get("rost_pct", 0)

                player_stats = {
                    "pts": player["PTS"],
                    "reb": player["REB"],
                    "ast": player["AST"],
                    "stl": player["STL"],
                    "blk": player["BLK"],
                    "tov": player["TOV"],
                    "fgm": player["FGM"],
                    "fga": player["FGA"],
                    "fg3m": player["FG3M"],
                    "fg3a": player["FG3A"],
                    "ftm": player["FTM"],
                    "fta": player["FTA"],
                }
                fpts = _calculate_fantasy_points(player_stats)

                if player_id not in entries or current_gp > entries[player_id]['gp']:
                    entries[player_id] = {
                        "id": player_id,
                        "name": player_name,
                        "team": player["TEAM"],
                        "date": game_date,
                        "fpts": fpts,
                        "min": player["MIN"],
                        "gp": current_gp,
                        "rost_pct": rost_pct,
                        "pipeline_run_id": run_id,
                        **player_stats,
                    }

            if entries:
                CumulativePlayerStats.insert_many(list(entries.values())).execute()
                records_processed = len(entries)
                log.info("records_inserted", count=records_processed)

                # Update rankings
                subquery = CumulativePlayerStats.select(
                    CumulativePlayerStats.id,
                    fn.MAX(CumulativePlayerStats.date).alias("max_date"),
                ).group_by(CumulativePlayerStats.id)

                latest_entries = list(
                    CumulativePlayerStats.select()
                    .join(
                        subquery,
                        on=(
                            (CumulativePlayerStats.id == subquery.c.id)
                            & (CumulativePlayerStats.date == subquery.c.max_date)
                        ),
                    )
                    .order_by(CumulativePlayerStats.fpts.desc())
                )

                for i, player in enumerate(latest_entries, start=1):
                    CumulativePlayerStats.update(rank=i).where(
                        (CumulativePlayerStats.id == player.id)
                        & (CumulativePlayerStats.date == player.date)
                    ).execute()

                log.info("rankings_updated", player_count=len(latest_entries))

            completed_at = datetime.now(central_tz)
            run.mark_success(records_processed=records_processed)

            log.info(
                "pipeline_completed",
                records_processed=records_processed,
                duration_seconds=(completed_at - started_at).total_seconds(),
            )

            return PipelineResult(
                status=ApiStatus.SUCCESS,
                message=f"Cumulative stats completed for {game_date}",
                started_at=started_at.isoformat(),
                completed_at=completed_at.isoformat(),
                duration_seconds=(completed_at - started_at).total_seconds(),
                records_processed=records_processed,
            )

        except Exception as e:
            completed_at = datetime.now(central_tz)
            error_msg = f"{type(e).__name__}: {str(e)}"
            run.mark_failed(error_msg)

            log.error(
                "pipeline_failed",
                error=error_msg,
                traceback=traceback.format_exc(),
            )

            return PipelineResult(
                status=ApiStatus.ERROR,
                message="Cumulative player stats failed",
                started_at=started_at.isoformat(),
                completed_at=completed_at.isoformat(),
                duration_seconds=(completed_at - started_at).total_seconds(),
                error=f"{error_msg}\n{traceback.format_exc()}",
            )

    @staticmethod
    async def run_daily_matchup_scores() -> PipelineResult:
        """
        Fetch current matchup scores for all saved teams and record daily snapshots.
        """
        log = get_logger("pipeline").bind(pipeline="daily_matchup_scores")
        central_tz = pytz.timezone("US/Central")
        started_at = datetime.now(central_tz)
        records_processed = 0

        # Start pipeline run tracking
        run = PipelineRun.start_run("daily_matchup_scores")
        run_id = run.id
        log = log.bind(run_id=str(run_id))
        log.info("pipeline_started")

        try:
            today = started_at.date()

            # Get current matchup info
            matchup_info = _get_current_matchup_info(today)
            if not matchup_info:
                log.info("no_active_matchup")
                run.mark_success(records_processed=0)
                return PipelineResult(
                    status=ApiStatus.SUCCESS,
                    message="No active matchup period",
                    started_at=started_at.isoformat(),
                    completed_at=datetime.now(central_tz).isoformat(),
                    records_processed=0,
                )

            log.info(
                "matchup_info",
                matchup_period=matchup_info["matchup_number"],
                day_index=matchup_info["day_index"],
            )

            # Get all saved teams
            teams = list(Team.select())
            log.info("teams_found", count=len(teams))

            for team in teams:
                try:
                    league_info = json.loads(team.league_info)
                    team_name = league_info.get("team_name", "")

                    # Fetch matchup from ESPN
                    espn_data = PipelineService._fetch_matchup_from_espn(
                        league_id=league_info["league_id"],
                        team_name=team_name,
                        espn_s2=league_info.get("espn_s2", ""),
                        swid=league_info.get("swid", ""),
                        year=league_info.get("year", settings.espn_year),
                        matchup_period=matchup_info["matchup_number"],
                    )

                    if espn_data:
                        # Upsert daily score
                        record = {
                            "team_id": team.team_id,
                            "team_name": espn_data["team_name"],
                            "matchup_period": matchup_info["matchup_number"],
                            "opponent_team_name": espn_data["opponent_team_name"],
                            "date": today,
                            "day_of_matchup": matchup_info["day_index"],
                            "current_score": espn_data["current_score"],
                            "opponent_current_score": espn_data["opponent_current_score"],
                            "pipeline_run_id": run_id,
                        }

                        DailyMatchupScore.insert(record).on_conflict(
                            conflict_target=[
                                DailyMatchupScore.team_id,
                                DailyMatchupScore.matchup_period,
                                DailyMatchupScore.date,
                            ],
                            update={
                                "current_score": record["current_score"],
                                "opponent_current_score": record["opponent_current_score"],
                                "team_name": record["team_name"],
                                "opponent_team_name": record["opponent_team_name"],
                                "pipeline_run_id": record["pipeline_run_id"],
                            },
                        ).execute()
                        records_processed += 1

                        log.debug(
                            "team_score_recorded",
                            team=team_name,
                            score=espn_data["current_score"],
                            opponent_score=espn_data["opponent_current_score"],
                        )

                except Exception as e:
                    log.warning(
                        "team_processing_error",
                        team_id=team.team_id,
                        error=str(e),
                    )
                    continue

            completed_at = datetime.now(central_tz)
            run.mark_success(records_processed=records_processed)

            log.info(
                "pipeline_completed",
                records_processed=records_processed,
                duration_seconds=(completed_at - started_at).total_seconds(),
            )

            return PipelineResult(
                status=ApiStatus.SUCCESS,
                message=f"Matchup scores completed for {today}",
                started_at=started_at.isoformat(),
                completed_at=completed_at.isoformat(),
                duration_seconds=(completed_at - started_at).total_seconds(),
                records_processed=records_processed,
            )

        except Exception as e:
            completed_at = datetime.now(central_tz)
            error_msg = f"{type(e).__name__}: {str(e)}"
            run.mark_failed(error_msg)

            log.error(
                "pipeline_failed",
                error=error_msg,
                traceback=traceback.format_exc(),
            )

            return PipelineResult(
                status=ApiStatus.ERROR,
                message="Daily matchup scores failed",
                started_at=started_at.isoformat(),
                completed_at=completed_at.isoformat(),
                duration_seconds=(completed_at - started_at).total_seconds(),
                error=f"{error_msg}\n{traceback.format_exc()}",
            )

    @staticmethod
    def _fetch_matchup_from_espn(
        league_id: int,
        team_name: str,
        espn_s2: str,
        swid: str,
        year: int,
        matchup_period: int,
    ) -> Optional[dict]:
        """Fetch matchup data from ESPN API for a specific team."""
        log = get_logger("espn_api")

        params = {"view": ["mTeam", "mMatchup", "mSchedule"]}
        cookies = {"espn_s2": espn_s2, "SWID": swid}
        endpoint = ESPN_FANTASY_ENDPOINT.format(year, league_id)

        try:
            response = requests.get(
                endpoint,
                params=params,
                cookies=cookies,
                timeout=settings.http_timeout
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            log.warning("espn_matchup_error", error=str(e), team=team_name)
            return None

        # Find our team
        teams = data.get("teams", [])
        our_team_id = None
        our_team_name = None

        for team in teams:
            if team.get("name", "").strip() == team_name.strip():
                our_team_id = team.get("id")
                our_team_name = team.get("name")
                break

        if not our_team_id:
            log.warning("team_not_found", team=team_name)
            return None

        # Find current matchup
        schedule = data.get("schedule", [])
        for matchup in schedule:
            if matchup.get("matchupPeriodId") == matchup_period:
                home_data = matchup.get("home", {})
                away_data = matchup.get("away", {})
                home_id = home_data.get("teamId")
                away_id = away_data.get("teamId")

                if home_id == our_team_id:
                    opponent_id = away_id
                    our_score = home_data.get("totalPoints", 0)
                    opponent_score = away_data.get("totalPoints", 0)
                elif away_id == our_team_id:
                    opponent_id = home_id
                    our_score = away_data.get("totalPoints", 0)
                    opponent_score = home_data.get("totalPoints", 0)
                else:
                    continue

                # Find opponent name
                opponent_name = "Unknown"
                for team in teams:
                    if team.get("id") == opponent_id:
                        opponent_name = team.get("name", "Unknown")
                        break

                return {
                    "team_name": our_team_name,
                    "current_score": our_score,
                    "opponent_team_name": opponent_name,
                    "opponent_current_score": opponent_score,
                }

        return None

    @staticmethod
    async def run_all_pipelines() -> dict[str, PipelineResult]:
        """Run all pipelines in sequence."""
        log = get_logger("pipeline").bind(operation="run_all")
        results = {}

        log.info("all_pipelines_started")

        log.info("running_pipeline", pipeline="daily_player_stats", step="1/3")
        results["daily_player_stats"] = await PipelineService.run_daily_player_stats()

        log.info("running_pipeline", pipeline="cumulative_player_stats", step="2/3")
        results["cumulative_player_stats"] = (
            await PipelineService.run_cumulative_player_stats()
        )

        log.info("running_pipeline", pipeline="daily_matchup_scores", step="3/3")
        results["daily_matchup_scores"] = (
            await PipelineService.run_daily_matchup_scores()
        )

        # Summarize results
        success_count = sum(
            1 for r in results.values() if r.status == ApiStatus.SUCCESS
        )
        log.info(
            "all_pipelines_completed",
            success_count=success_count,
            total_count=len(results),
        )

        return results
