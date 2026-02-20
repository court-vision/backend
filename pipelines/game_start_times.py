"""
Game Start Times Pipeline

Populates game start times from the raw NBA schedule JSON.
This covers future/scheduled games that the GameSchedulePipeline
(which only handles completed games from the NBA API) doesn't include.
"""

import json
import os
from datetime import datetime

from db.models.nba import Game, NBATeam
from pipelines.base import BasePipeline
from pipelines.config import PipelineConfig
from pipelines.context import PipelineContext


SCHEDULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "static",
    "schedule_raw2025-2026.json",
)

# Map gameStatus integers to our status strings
GAME_STATUS_MAP = {
    1: "scheduled",
    2: "in_progress",
    3: "final",
}


class GameStartTimesPipeline(BasePipeline):
    """
    Parse the raw NBA schedule JSON and upsert Game records
    with start_time_et populated for all games.

    For completed games (status 3), scores are also included.
    """

    config = PipelineConfig(
        name="game_start_times",
        display_name="Game Start Times",
        description="Populates game start times from NBA schedule data",
        target_table="nba.games",
    )

    def execute(self, ctx: PipelineContext) -> None:
        """Load schedule JSON and upsert games with start times."""
        ctx.log.info("loading_schedule", path=SCHEDULE_PATH)

        with open(SCHEDULE_PATH, "r") as f:
            schedule_data = json.load(f)

        game_dates = schedule_data["leagueSchedule"]["gameDates"]
        season_year = schedule_data["leagueSchedule"]["seasonYear"]
        season = season_year

        # Cache valid NBA team IDs to skip global/international games
        valid_team_ids = {t.id for t in NBATeam.select(NBATeam.id)}

        ctx.log.info("schedule_loaded", game_dates_count=len(game_dates), season=season, valid_teams=len(valid_team_ids))

        for game_date_entry in game_dates:
            for game in game_date_entry["games"]:
                game_id = game["gameId"]
                game_status = game.get("gameStatus", 1)
                status = GAME_STATUS_MAP.get(game_status, "scheduled")

                # Parse gameDateTimeEst - this IS already ET despite the "Z" suffix
                dt_str = game.get("gameDateTimeEst", "")
                if not dt_str:
                    continue

                try:
                    # Format: "2025-10-22T19:30:00Z"
                    dt = datetime.strptime(dt_str.rstrip("Z"), "%Y-%m-%dT%H:%M:%S")
                except ValueError:
                    ctx.log.warning("invalid_datetime", game_id=game_id, dt_str=dt_str)
                    continue

                game_date = dt.date()
                start_time = dt.time()

                home_tricode = game.get("homeTeam", {}).get("teamTricode")
                away_tricode = game.get("awayTeam", {}).get("teamTricode")

                if not home_tricode or not away_tricode:
                    continue

                # Skip international/global games with non-NBA teams
                if home_tricode not in valid_team_ids or away_tricode not in valid_team_ids:
                    continue

                game_data = {
                    "game_date": game_date,
                    "season": season,
                    "home_team_id": home_tricode,
                    "away_team_id": away_tricode,
                    "start_time_et": start_time,
                    "status": status,
                }

                # Include scores for completed games
                if game_status == 3:
                    home_score = game.get("homeTeam", {}).get("score")
                    away_score = game.get("awayTeam", {}).get("score")
                    if home_score is not None:
                        game_data["home_score"] = home_score
                    if away_score is not None:
                        game_data["away_score"] = away_score

                Game.upsert_game(game_id, game_data)
                ctx.increment_records()

        ctx.log.info("processing_complete", records=ctx.records_processed)
