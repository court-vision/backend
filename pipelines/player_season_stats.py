"""
Cumulative Player Stats Pipeline

Updates cumulative season stats and rankings for players who played.
"""

from datetime import timedelta

import pytz
from peewee import fn

from core.settings import settings
from db.models.nba import Player, PlayerSeasonStats
from pipelines.base import BasePipeline
from pipelines.config import PipelineConfig
from pipelines.context import PipelineContext
from pipelines.extractors import ESPNExtractor, NBAApiExtractor
from pipelines.transformers import normalize_name, calculate_fantasy_points


class PlayerSeasonStatsPipeline(BasePipeline):
    """
    Update cumulative season stats and rankings for players who played.

    This pipeline:
    1. Fetches ESPN player data for roster percentages
    2. Fetches NBA league leaders with season totals
    3. Compares with previous records to find players who played
    4. Inserts new season stats records
    5. Updates rankings based on total fantasy points
    """

    config = PipelineConfig(
        name="player_season_stats",
        display_name="Player Season Stats",
        description="Updates season totals and rankings for players who played yesterday",
        target_table="nba.player_season_stats",
        depends_on=("player_game_stats",),
    )

    def __init__(self):
        super().__init__()
        self.espn_extractor = ESPNExtractor()
        self.nba_extractor = NBAApiExtractor()

    def execute(self, ctx: PipelineContext) -> None:
        """Execute the cumulative player stats pipeline."""
        central_tz = pytz.timezone("US/Central")

        # Determine the game date. Use an explicit override for backfills;
        # otherwise use CST with a 6am cutoff (before 6am = previous night's games).
        if ctx.date_override:
            game_date = ctx.date_override
        else:
            now_cst = ctx.started_at  # already in CST from PipelineContext
            if now_cst.hour < 6:
                game_date = (now_cst - timedelta(days=1)).date()
            else:
                game_date = now_cst.date()

        # Determine season string
        season = f"{game_date.year}-{str(game_date.year + 1)[-2:]}"
        if game_date.month < 8:
            season = f"{game_date.year - 1}-{str(game_date.year)[-2:]}"

        ctx.log.info("fetching_data", date=str(game_date), season=season)

        # Fetch ESPN data for roster percentages
        espn_data = self.espn_extractor.get_player_data()
        ctx.log.info("espn_data_fetched", player_count=len(espn_data))

        # Fetch NBA league leaders
        api_data = self.nba_extractor.get_league_leaders(season)
        ctx.log.info("nba_data_fetched", player_count=len(api_data))

        # Get latest GP for each player from database
        subquery = PlayerSeasonStats.select(
            PlayerSeasonStats.player_id,
            fn.MAX(PlayerSeasonStats.as_of_date).alias("max_date"),
        ).group_by(PlayerSeasonStats.player_id)

        latest_records = (
            PlayerSeasonStats.select(PlayerSeasonStats.player_id, PlayerSeasonStats.gp)
            .join(
                subquery,
                on=(
                    (PlayerSeasonStats.player_id == subquery.c.player_id)
                    & (PlayerSeasonStats.as_of_date == subquery.c.max_date)
                ),
            )
        )
        db_gp_map = {record.player_id: record.gp for record in latest_records}

        # Find players who played (GP changed) and prepare entries
        entries = {}
        for player in api_data:
            player_id = player["PLAYER_ID"]
            current_gp = player["GP"]

            # Skip if player hasn't played new games
            if player_id in db_gp_map and current_gp == db_gp_map[player_id]:
                continue

            player_name = player["PLAYER"]
            normalized_name = normalize_name(player_name)
            rost_pct = espn_data.get(normalized_name, {}).get("rost_pct", 0)
            team_abbrev = player["TEAM"]

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
            fpts = calculate_fantasy_points(player_stats)

            # Keep only the entry with highest GP for each player
            if player_id not in entries or current_gp > entries[player_id]["gp"]:
                # Ensure player exists in dimension table
                Player.upsert_player(player_id=player_id, name=player_name)

                entries[player_id] = {
                    "player_id": player_id,
                    "team_id": team_abbrev,
                    "as_of_date": game_date,
                    "season": season,
                    "gp": current_gp,
                    "fpts": fpts,
                    "min": player["MIN"],
                    "rost_pct": rost_pct,
                    "pipeline_run_id": ctx.run_id,
                    **player_stats,
                }

        if entries:
            # Insert new records
            for entry_data in entries.values():
                PlayerSeasonStats.upsert_season_stats(
                    player_id=entry_data["player_id"],
                    as_of_date=entry_data["as_of_date"],
                    season=entry_data["season"],
                    stats={
                        "gp": entry_data["gp"],
                        "fpts": entry_data["fpts"],
                        "pts": entry_data["pts"],
                        "reb": entry_data["reb"],
                        "ast": entry_data["ast"],
                        "stl": entry_data["stl"],
                        "blk": entry_data["blk"],
                        "tov": entry_data["tov"],
                        "min": entry_data["min"],
                        "fgm": entry_data["fgm"],
                        "fga": entry_data["fga"],
                        "fg3m": entry_data["fg3m"],
                        "fg3a": entry_data["fg3a"],
                        "ftm": entry_data["ftm"],
                        "fta": entry_data["fta"],
                        "rost_pct": entry_data["rost_pct"],
                    },
                    team_id=entry_data["team_id"],
                    pipeline_run_id=ctx.run_id,
                )
                ctx.increment_records()

            ctx.log.info("records_inserted", count=len(entries))

            # Update rankings for today's records
            self._update_rankings(game_date, season, ctx)

    def _update_rankings(
        self, as_of_date, season: str, ctx: PipelineContext
    ) -> None:
        """Update rankings for all players with records on this date."""
        # Get all records for this date ordered by fpts descending
        records = list(
            PlayerSeasonStats.select()
            .where(
                (PlayerSeasonStats.as_of_date == as_of_date)
                & (PlayerSeasonStats.season == season)
            )
            .order_by(PlayerSeasonStats.fpts.desc())
        )

        for rank, record in enumerate(records, start=1):
            PlayerSeasonStats.update(rank=rank).where(
                PlayerSeasonStats.id == record.id
            ).execute()

        ctx.log.info("rankings_updated", player_count=len(records))
