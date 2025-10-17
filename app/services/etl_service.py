from datetime import datetime, timedelta
from app.utils.constants import CRON_TOKEN, LEAGUE_ID, PROXY_STRING
from app.utils.etl_helpers import (
    calculate_fantasy_points, create_daily_entries, create_total_entries,
    restructure_data, get_players_to_update, serialize_fpts_data
)
from app.utils.espn_helpers import remove_diacritics
from app.services.espn_service import EspnService
from app.db.models import TotalStats, DailyStats, FreeAgent, Standing
from app.libs.nba_api.stats.endpoints import leagueleaders
from app.schemas.etl import FPTSPlayer
import pytz

class ETLService:
    
    @staticmethod
    def fetch_nba_fpts_data(rostered_data: dict) -> dict:
        """Fetches and restructures the data from the NBA API"""
        leaders = leagueleaders.LeagueLeaders(
            season='2024-25',
            per_mode48='Totals',
            stat_category_abbreviation='PTS',
            proxy=PROXY_STRING
        )
        updated = leaders.get_normalized_dict()['LeagueLeaders']

        # Create a new dictionary with the id as the key and also filter out the columns
        updated_dict = {}
        for player in updated:
            updated_dict[player['PLAYER_ID']] = {
            'id': player['PLAYER_ID'],
            'name': player['PLAYER'],
            'team': player['TEAM'],
            # add this later (date)
            'min': player['MIN'],
            # add this later (fpts)
            'pts': player['PTS'],
            'reb': player['REB'],
            'ast': player['AST'],
            'stl': player['STL'],
            'blk': player['BLK'],
            'tov': player['TOV'],
            'fgm': player['FGM'],
            'fga': player['FGA'],
            'fg3m': player['FG3M'],
            'fg3a': player['FG3A'],
            'ftm': player['FTM'],
            'fta': player['FTA'],
            'gp': player['GP'],
            'rost_pct': rostered_data.get(remove_diacritics(player['PLAYER']), 0)
            }
        
        return updated_dict

    @staticmethod
    def create_rostered_entries(data: list[dict]) -> list[tuple]:
        """Create the entries for the database"""
        central_tz = pytz.timezone('US/Central')
        today = datetime.now(central_tz)
        date_str = today.strftime("%Y-%m-%d")
        date = datetime.strptime(date_str, "%Y-%m-%d")

        return [(player['espnId'], player['fullName'], player['team'], date, player['rosteredPct']) for player in data]

    @staticmethod
    def serialize_fpts_data(data: list[tuple]) -> list[FPTSPlayer]:
        """Serialize FPTS data for response"""
        return [FPTSPlayer(
            rank=player[0],
            player_id=player[1],
            player_name=player[2],
            total_fpts=player[3],
            avg_fpts=player[4],
            rank_change=player[5]
        ) for player in data]

    @staticmethod
    async def start_etl_update_fpts(cron_token: str):
        """Route to kick-off the ETL process, returning something quick to avoid timeouts on the frontend"""
        if cron_token != CRON_TOKEN:
            print("Invalid token")
            return {"message": "Invalid token"}
        
        central_tz = pytz.timezone('US/Central')
        yesterday = datetime.now(central_tz) - timedelta(days=1)
        date_str = yesterday.strftime("%Y-%m-%d")
        date = datetime.strptime(date_str, "%Y-%m-%d")
        
        # Get the rostered percentages from ESPN
        rostered_data = EspnService.fetch_espn_rostered_data(int(LEAGUE_ID), 2025, for_stats=True)

        # Fetch the data from the NBA API
        new_data = ETLService.fetch_nba_fpts_data(rostered_data)
        
        # Restructure the data from the DB
        data = list(TotalStats.select().dicts())
        old_data = restructure_data([tuple(row.values()) for row in data])

        # Get the players to update
        players_to_update, id_map = get_players_to_update(new_data, old_data)
        print(len(players_to_update), "players to update")

        # Create and insert the daily entries
        daily_entries = create_daily_entries(players_to_update, old_data, date)
        
        # Use Peewee bulk insert
        DailyStats.insert_many([
            {
                'id': entry[0], 'name': entry[1], 'team': entry[2], 'date': entry[3],
                'fpts': entry[4], 'pts': entry[5], 'reb': entry[6], 'ast': entry[7],
                'stl': entry[8], 'blk': entry[9], 'tov': entry[10], 'fgm': entry[11],
                'fga': entry[12], 'fg3m': entry[13], 'fg3a': entry[14], 'ftm': entry[15],
                'fta': entry[16], 'min': entry[17], 'rost_pct': entry[18]
            }
            for entry in daily_entries
        ]).execute()
        print("Inserted new daily entries")
        
        # Create and insert the total entries
        total_entries = create_total_entries(new_data, old_data, id_map, date)
        
        # Use Peewee bulk upsert
        for entry in total_entries:
            TotalStats.replace(
                id=entry[0], name=entry[1], team=entry[2], date=entry[3],
                fpts=entry[4], pts=entry[5], reb=entry[6], ast=entry[7],
                stl=entry[8], blk=entry[9], tov=entry[10], fgm=entry[11],
                fga=entry[12], fg3m=entry[13], fg3a=entry[14], ftm=entry[15],
                fta=entry[16], min=entry[17], gp=entry[18], rost_pct=entry[19]
            ).execute()
        print("Inserted new total entries")

        # Update the previous rank, only for players who played on the date
        players_who_played = TotalStats.select(TotalStats.id).where(TotalStats.date == date)
        TotalStats.update(p_rank=TotalStats.c_rank).where(TotalStats.id.in_(players_who_played)).execute()

        # Recalculate the rank for all players - this is complex in Peewee, so we'll do it manually
        all_players = list(TotalStats.select().order_by(TotalStats.fpts.desc()))
        for i, player in enumerate(all_players):
            TotalStats.update(c_rank=i+1).where(TotalStats.id == player.id).execute()
        print("Updated ranks")
        
        print("ETL process completed")

        return {"message": "ETL process completed"}

    @staticmethod
    async def get_fpts_data(cron_token: str):
        """Queries the view to get the data for the frontend"""
        if not cron_token or cron_token != CRON_TOKEN:
            return {"data": []}
            
        data = list(Standing.select().dicts())
        data_tuples = [tuple(row.values()) for row in data]

        return {"data": ETLService.serialize_fpts_data(data_tuples)}

    @staticmethod
    async def update_rostered(cron_token: str):
        """Actual ETL process for freeagent rostered percentages"""
        if cron_token != CRON_TOKEN:
            print("Invalid token")
            return
        
        # Fetch the nba player data and clean it
        cleaned_data = EspnService.fetch_espn_rostered_data(int(LEAGUE_ID), 2025)
        
        # Create the entries for the rostered percentages
        entries = ETLService.create_rostered_entries(cleaned_data)
        
        # Insert the entries into the DB using Peewee
        FreeAgent.insert_many([
            {
                'espn_id': entry[0], 'name': entry[1], 'team': entry[2], 
                'date': entry[3], 'rostered_pct': entry[4]
            }
            for entry in entries
        ]).execute()
        
        print("ETL process completed")
