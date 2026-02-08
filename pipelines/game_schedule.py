"""
Game Schedule Pipeline

Fetches NBA game schedule and results from NBA API.
"""

from datetime import datetime

import pytz

from core.settings import settings
from db.models.nba import Game
from pipelines.base import BasePipeline
from pipelines.config import PipelineConfig
from pipelines.context import PipelineContext
from pipelines.extractors import NBAApiExtractor


class GameSchedulePipeline(BasePipeline):
    """
    Fetch game schedule and results, insert into games table.

    This pipeline:
    1. Fetches LeagueGameLog for the current season
    2. Processes each game record
    3. Inserts/updates game records with scores and status

    The game log returns one row per team per game, so we need to
    deduplicate and combine home/away data.
    """

    config = PipelineConfig(
        name="game_schedule",
        display_name="Game Schedule",
        description="Fetches NBA game schedule and results",
        target_table="nba.games",
    )

    def __init__(self):
        super().__init__()
        self.nba_extractor = NBAApiExtractor()

    def execute(self, ctx: PipelineContext) -> None:
        """Execute the game schedule pipeline."""
        central_tz = pytz.timezone("US/Central")

        # Determine season string
        now = ctx.started_at
        season = f"{now.year}-{str(now.year + 1)[-2:]}"
        if now.month < 8:
            season = f"{now.year - 1}-{str(now.year)[-2:]}"

        ctx.log.info("fetching_game_log", season=season)

        # Fetch game log from NBA API
        api_data = self.nba_extractor.get_league_game_log(season)

        if not api_data:
            ctx.log.info("no_games_found")
            return

        ctx.log.info("data_fetched", record_count=len(api_data))

        # Group games by GAME_ID (each game appears twice - once per team)
        games_by_id: dict[str, dict] = {}

        for record in api_data:
            game_id = record["GAME_ID"]
            team_abbrev = record["TEAM_ABBREVIATION"]
            matchup = record.get("MATCHUP", "")

            # Determine if this is home or away
            # Matchup format: "LAL vs. BOS" (home) or "LAL @ BOS" (away)
            is_home = " vs. " in matchup

            if game_id not in games_by_id:
                games_by_id[game_id] = {
                    "game_id": game_id,
                    "game_date": self._parse_date(record.get("GAME_DATE")),
                    "season": season,
                    "status": "final",  # Game log only has completed games
                }

            game = games_by_id[game_id]

            if is_home:
                game["home_team_id"] = team_abbrev
                game["home_score"] = record.get("PTS")
            else:
                game["away_team_id"] = team_abbrev
                game["away_score"] = record.get("PTS")

        ctx.log.info("games_processed", unique_games=len(games_by_id))

        # Insert/update games
        for game_data in games_by_id.values():
            # Only process if we have both teams
            if not game_data.get("home_team_id") or not game_data.get("away_team_id"):
                continue

            Game.upsert_game(game_data["game_id"], game_data)
            ctx.increment_records()

        ctx.log.info("processing_complete", records=ctx.records_processed)

    def _parse_date(self, date_str: str | None):
        """Parse date string from NBA API."""
        if not date_str:
            return None

        try:
            # NBA API returns "OCT 22, 2024" or "2024-10-22" format
            if "," in date_str:
                return datetime.strptime(date_str, "%b %d, %Y").date()
            else:
                return datetime.strptime(date_str.split("T")[0], "%Y-%m-%d").date()
        except ValueError:
            return None
