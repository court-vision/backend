"""
Team Stats Pipeline

Fetches season-to-date statistics for all 30 NBA teams from NBA API,
combining base per-game counting stats and advanced efficiency metrics.

Makes two API calls per run (Base + Advanced measure types) and merges
them by team abbreviation before upserting to nba.team_stats.
"""

from datetime import timedelta

from db.models.nba.team_stats import TeamStats
from pipelines.base import BasePipeline
from pipelines.config import PipelineConfig
from pipelines.context import PipelineContext
from pipelines.extractors import NBAApiExtractor


class TeamStatsPipeline(BasePipeline):
    """
    Fetch and store season-to-date stats for all 30 NBA teams.

    This pipeline:
    1. Fetches LeagueDashTeamStats with MeasureType="Advanced" (ratings, pace)
    2. Fetches LeagueDashTeamStats with MeasureType="Base" (per-game counting stats)
    3. Merges both result sets by TEAM_ABBREVIATION
    4. Upserts one record per team to nba.team_stats

    Key metrics collected:
    - Efficiency: OFF_RATING, DEF_RATING, NET_RATING
    - Pace: PACE (possessions per 48 min)
    - Shooting: TS_PCT, EFG_PCT, FG_PCT, FG3_PCT, FT_PCT
    - Per-game: PTS, REB, AST, STL, BLK, TOV
    - Record: W, L, W_PCT
    """

    config = PipelineConfig(
        name="team_stats",
        display_name="Team Stats",
        description="Daily team pace, ratings, and per-game stats for all 30 NBA teams",
        target_table="nba.team_stats",
    )

    def __init__(self):
        super().__init__()
        self.nba_extractor = NBAApiExtractor()

    def execute(self, ctx: PipelineContext) -> None:
        """Execute the team stats pipeline."""

        # Determine the as_of_date using the same CST 6am cutoff as all other pipelines
        if ctx.date_override:
            as_of_date = ctx.date_override
        else:
            now_cst = ctx.started_at  # already in CST from PipelineContext
            if now_cst.hour < 6:
                as_of_date = (now_cst - timedelta(days=1)).date()
            else:
                as_of_date = now_cst.date()

        # Determine season string (NBA season spans two calendar years)
        season = f"{as_of_date.year}-{str(as_of_date.year + 1)[-2:]}"
        if as_of_date.month < 8:
            season = f"{as_of_date.year - 1}-{str(as_of_date.year)[-2:]}"

        ctx.log.info("fetching_team_stats", season=season, as_of_date=str(as_of_date))

        # Fetch merged advanced + base stats (two API calls internally)
        api_data = self.nba_extractor.get_team_stats(season)

        if not api_data:
            ctx.log.info("no_data_returned")
            return

        ctx.log.info("data_fetched", team_count=len(api_data))

        for team in api_data:
            abbr = team.get("TEAM_ABBREVIATION")
            if not abbr:
                ctx.log.warning("missing_team_abbreviation", team=team.get("TEAM_NAME"))
                continue

            stats = {
                "gp": team.get("GP"),
                "w": team.get("W"),
                "l": team.get("L"),
                "w_pct": team.get("W_PCT"),
                # Per-game counting stats (from Base measure)
                "pts": team.get("PTS"),
                "reb": team.get("REB"),
                "ast": team.get("AST"),
                "stl": team.get("STL"),
                "blk": team.get("BLK"),
                "tov": team.get("TOV"),
                "fg_pct": team.get("FG_PCT"),
                "fg3_pct": team.get("FG3_PCT"),
                "ft_pct": team.get("FT_PCT"),
                # Advanced efficiency metrics (from Advanced measure)
                "off_rating": team.get("OFF_RATING"),
                "def_rating": team.get("DEF_RATING"),
                "net_rating": team.get("NET_RATING"),
                "pace": team.get("PACE"),
                "ts_pct": team.get("TS_PCT"),
                "efg_pct": team.get("EFG_PCT"),
                "ast_pct": team.get("AST_PCT"),
                "oreb_pct": team.get("OREB_PCT"),
                "dreb_pct": team.get("DREB_PCT"),
                "reb_pct": team.get("REB_PCT"),
                "tov_pct": team.get("TM_TOV_PCT"),
                "pie": team.get("PIE"),
            }

            TeamStats.upsert_team_stats(
                team_id=abbr,
                as_of_date=as_of_date,
                season=season,
                stats=stats,
                pipeline_run_id=ctx.run_id,
            )
            ctx.increment_records()

        ctx.log.info("processing_complete", records=ctx.records_processed)
