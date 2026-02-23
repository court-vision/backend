"""
Daily Matchup Scores Pipeline

Fetches current matchup scores for all saved teams and records daily snapshots.
"""

import json
from typing import Optional

import pytz

from core.settings import settings
from db.models.teams import Team
from db.models.stats.daily_matchup_score import DailyMatchupScore
from pipelines.base import BasePipeline
from pipelines.config import PipelineConfig
from pipelines.context import PipelineContext
from pipelines.extractors import ESPNExtractor, YahooExtractor
from services.schedule_service import get_matchup_dates


class DailyMatchupScoresPipeline(BasePipeline):
    """
    Fetch current matchup scores for all saved teams and record daily snapshots.

    This pipeline:
    1. Determines the current matchup period
    2. Fetches all saved user teams
    3. For each team, fetches matchup data from ESPN or Yahoo based on provider
    4. Records daily score snapshots for visualization
    """

    config = PipelineConfig(
        name="daily_matchup_scores",
        display_name="Daily Matchup Scores",
        description="Fetches current matchup scores for all saved teams",
        target_table="stats_s2.daily_matchup_score",
        # ESPN matchup data isn't ready immediately after games end — it rolls over
        # later in the morning. Run via a dedicated 10am ET cron instead.
        post_game_excluded=True,
    )

    def __init__(self):
        super().__init__()
        self.espn_extractor = ESPNExtractor()
        self.yahoo_extractor = YahooExtractor()

    def execute(self, ctx: PipelineContext) -> None:
        """Execute the daily matchup scores pipeline."""
        central_tz = pytz.timezone("US/Central")
        today = ctx.started_at.date()

        # Get current matchup info
        matchup_info = self._get_current_matchup_info(today)
        if not matchup_info:
            ctx.log.info("no_active_matchup")
            return

        ctx.log.info(
            "matchup_info",
            matchup_period=matchup_info["matchup_number"],
            day_index=matchup_info["day_index"],
        )

        # Get all saved teams
        teams = list(Team.select())
        ctx.log.info("teams_found", count=len(teams))

        for team in teams:
            try:
                league_info = json.loads(team.league_info)
                team_name = league_info.get("team_name", "")
                provider = league_info.get("provider", "espn")  # Default to ESPN for backward compatibility

                if provider == "yahoo":
                    matchup_data = self._fetch_yahoo_matchup(
                        ctx, team, league_info, team_name, matchup_info["matchup_number"]
                    )
                else:
                    matchup_data = self._fetch_espn_matchup(
                        league_info, team_name, matchup_info["matchup_number"]
                    )

                if matchup_data:
                    # Use provider's matchup period if available (Yahoo may differ
                    # from local schedule numbering), otherwise use local schedule's
                    effective_matchup_period = matchup_data.get(
                        "matchup_period", matchup_info["matchup_number"]
                    )

                    # Upsert daily score
                    record = {
                        "team_id": team.team_id,
                        "team_name": matchup_data["team_name"],
                        "matchup_period": effective_matchup_period,
                        "opponent_team_name": matchup_data["opponent_team_name"],
                        "date": today,
                        "day_of_matchup": matchup_info["day_index"],
                        "current_score": matchup_data["current_score"],
                        "opponent_current_score": matchup_data["opponent_current_score"],
                        "pipeline_run_id": ctx.run_id,
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
                    ctx.increment_records()

                    ctx.log.debug(
                        "team_score_recorded",
                        team=team_name,
                        provider=provider,
                        score=matchup_data["current_score"],
                        opponent_score=matchup_data["opponent_current_score"],
                    )

            except Exception as e:
                ctx.log.warning(
                    "team_processing_error",
                    team_id=team.team_id,
                    error=str(e),
                )
                continue

        if teams and ctx.records_processed == 0:
            raise RuntimeError(
                f"0 of {len(teams)} teams processed — ESPN/Yahoo API may be unavailable or returning no matchup data"
            )

    def _fetch_espn_matchup(
        self,
        league_info: dict,
        team_name: str,
        matchup_period: int,
    ) -> Optional[dict]:
        """Fetch matchup data from ESPN."""
        return self.espn_extractor.get_matchup_data(
            league_id=league_info["league_id"],
            team_name=team_name,
            espn_s2=league_info.get("espn_s2", ""),
            swid=league_info.get("swid", ""),
            year=league_info.get("year", settings.espn_year),
            matchup_period=matchup_period,
        )

    def _fetch_yahoo_matchup(
        self,
        ctx: PipelineContext,
        team: Team,
        league_info: dict,
        team_name: str,
        matchup_period: int,
    ) -> Optional[dict]:
        """Fetch matchup data from Yahoo and handle token refresh."""
        yahoo_team_key = league_info.get("yahoo_team_key")
        if not yahoo_team_key:
            ctx.log.warning("yahoo_team_key_missing", team=team_name)
            return None

        matchup_data, new_tokens = self.yahoo_extractor.get_matchup_data(
            team_key=yahoo_team_key,
            team_name=team_name,
            access_token=league_info.get("yahoo_access_token", ""),
            refresh_token=league_info.get("yahoo_refresh_token"),
            token_expiry=league_info.get("yahoo_token_expiry"),
            matchup_period=matchup_period,
        )

        # If tokens were refreshed, persist them back to the database
        if new_tokens:
            self._update_yahoo_tokens(ctx, team, league_info, new_tokens)

        return matchup_data

    def _update_yahoo_tokens(
        self,
        ctx: PipelineContext,
        team: Team,
        league_info: dict,
        new_tokens: dict,
    ) -> None:
        """Persist refreshed Yahoo tokens back to the database."""
        try:
            league_info["yahoo_access_token"] = new_tokens["access_token"]
            league_info["yahoo_refresh_token"] = new_tokens["refresh_token"]
            league_info["yahoo_token_expiry"] = new_tokens["token_expiry"]

            team.league_info = json.dumps(league_info)
            team.save()

            ctx.log.debug(
                "yahoo_tokens_refreshed",
                team_id=team.team_id,
            )
        except Exception as e:
            ctx.log.warning(
                "yahoo_token_update_failed",
                team_id=team.team_id,
                error=str(e),
            )

    def _get_current_matchup_info(self, current_date) -> Optional[dict]:
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
