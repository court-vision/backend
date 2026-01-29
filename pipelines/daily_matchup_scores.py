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
from pipelines.extractors import ESPNExtractor
from services.schedule_service import get_matchup_dates


class DailyMatchupScoresPipeline(BasePipeline):
    """
    Fetch current matchup scores for all saved teams and record daily snapshots.

    This pipeline:
    1. Determines the current matchup period
    2. Fetches all saved user teams
    3. For each team, fetches matchup data from ESPN
    4. Records daily score snapshots for visualization
    """

    config = PipelineConfig(
        name="daily_matchup_scores",
        display_name="Daily Matchup Scores",
        description="Fetches current matchup scores for all saved teams",
        target_table="stats_s2.daily_matchup_score",
    )

    def __init__(self):
        super().__init__()
        self.espn_extractor = ESPNExtractor()

    async def execute(self, ctx: PipelineContext) -> None:
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

                # Fetch matchup from ESPN
                espn_data = self.espn_extractor.get_matchup_data(
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
                        score=espn_data["current_score"],
                        opponent_score=espn_data["opponent_current_score"],
                    )

            except Exception as e:
                ctx.log.warning(
                    "team_processing_error",
                    team_id=team.team_id,
                    error=str(e),
                )
                continue

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
