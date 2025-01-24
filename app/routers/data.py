from .data_helpers.utils import Player, check_league, create_rostered_entries, get_roster, fetch_nba_fpts_data, restructure_data, get_players_to_update, create_daily_entries, create_total_entries, serialize_fpts_data, fetch_espn_rostered_data
from .data_helpers.models import LeagueInfo, TeamDataReq, PlayerResp, ValidateLeagueResp, ETLUpdateFTPSReq
from .constants import ESPN_FANTASY_ENDPOINT, CRON_TOKEN, LEAGUE_ID
from fastapi import APIRouter, BackgroundTasks
from datetime import datetime, timedelta
from .db import get_cursor, commit_connection
import psycopg2.extras
import requests
import pytz
import json


router = APIRouter()

# Configure CORS
origins = [
    "http://localhost",
    "http://localhost:8000",
    "http://localhost:3000",  # Replace with your front-end URL
    "http://0.0.0.0:8000",
    "http://127.0.0.1:8000",
    "https://courtvisionaries.vercel.app",
    "https://www.courtvisionaries.live",
    "www.courtvisionaries.live",
    "courtvisionaries.live",
]

# router.add_middleware(
#     CORSMiddleware,
#     allow_origins=origins,
#     allow_credentials=True,
#     allow_methods=["POST", "GET", "OPTIONS", "DELETE"],
#     allow_headers=["Content-Type"],
# )

@router.get("/")
async def root():
    return {"message": "Hello World"}

# --------------------------------------------------- ESPN League Data -----------------------------------------------------

# Checks if the league and team are valid
@router.post("/validate_league")
async def validate_league(req: LeagueInfo) -> ValidateLeagueResp:
    return check_league(req)



# Returns important data for players on a team
@router.post("/get_roster_data")
async def get_team_data(req: TeamDataReq):

    params = {
            'view': ['mTeam', 'mRoster', 'mMatchup', 'mSettings', 'mStandings']
        }
    
    cookies = {
        'espn_s2': req.league_info.espn_s2,
        'SWID': req.league_info.swid
    }
    
    endpoint = ESPN_FANTASY_ENDPOINT.format(req.league_info.year, req.league_info.league_id)
    data = requests.get(endpoint, params=params, cookies=cookies).json()
    roster = get_roster(req.league_info.team_name, data['teams'])
    players = [Player(player, req.league_info.year) for player in roster]

    team_abbrev_corrections = {"PHL": "PHI", "PHO": "PHX"}
    pos_to_keep = {"PG", "SG", "SF", "PF", "C", "G", "F"}

    return [PlayerResp(name=player.name,
                                avg_points=player.avg_points,
                                team=team_abbrev_corrections.get(player.proTeam, player.proTeam),
                                valid_positions=[pos for pos in player.eligibleSlots if pos in pos_to_keep] + ["UT1", "UT2", "UT3"],
                                injured=player.injured,
                                ) for player in players]
    
    
            


# Returns important data for free agents in a league
@router.post("/get_freeagent_data")
async def get_free_agents(req: TeamDataReq):

    params = {
        'view': 'kona_player_info',
        'scoringPeriodId': 0,
    }

    filters = {"players":{"filterStatus":{"value":["FREEAGENT","WAIVERS"]},"filterSlotIds":{"value":[]},"limit":req.fa_count,"sortPercOwned":{"sortPriority":1,"sortAsc":False},"sortDraftRanks":{"sortPriority":100,"sortAsc":True,"value":"STANDARD"}}}
    headers = {'x-fantasy-filter': json.dumps(filters)}

    cookies = {
        'espn_s2': req.league_info.espn_s2,
        'SWID': req.league_info.swid
    }

    endpoint = ESPN_FANTASY_ENDPOINT.format(req.league_info.year, req.league_info.league_id)
    data = requests.get(endpoint, params=params, headers=headers, cookies=cookies).json()
    players = [Player(player, req.league_info.year) for player in data['players']]

    team_abbrev_corrections = {"PHL": "PHI", "PHO": "PHX"}
    pos_to_keep = {"PG", "SG", "SF", "PF", "C", "G", "F"}

    return [PlayerResp(name=player.name,
                        avg_points=player.avg_points,
                        team=team_abbrev_corrections.get(player.proTeam, player.proTeam),
                        valid_positions=[pos for pos in player.eligibleSlots if pos in pos_to_keep] + ["UT1", "UT2", "UT3"],
                        injured=player.injured,
                        ) for player in players]


# ---------------------------------------------------- ETL Processes -------------------------------------------------------

# Route to kick-off the ETL process, returning something quick to avoid timeouts on the frontend
@router.post('/etl/start-update-fpts')
async def start_ETL_update_fpts(req: ETLUpdateFTPSReq, background_tasks: BackgroundTasks):
	cron_token = req.cron_token
	background_tasks.add_task(trigger_ETL_update_fpts, cron_token)
	return {"message": "ETL process started"}
# Async trigger
async def trigger_ETL_update_fpts(cron_token: str):
	await update_fpts(ETLUpdateFTPSReq(cron_token=cron_token))
# Actual ETL process
async def update_fpts(req: ETLUpdateFTPSReq):
	cron_token = req.cron_token
	if cron_token != CRON_TOKEN:
		print("Invalid token")
		return
	
	central_tz = pytz.timezone('US/Central')
	yesterday = datetime.now(central_tz) - timedelta(days=1)
	date_str = yesterday.strftime("%Y-%m-%d")
	date = datetime.strptime(date_str, "%Y-%m-%d")
	
	# Get the rostered percentages from ESPN
	rostered_data = fetch_espn_rostered_data(int(LEAGUE_ID), 2025, for_stats=True)

	# Fetch the data from the NBA API
	new_data = fetch_nba_fpts_data(rostered_data)
	
	# Restructure the data from the DB
	with get_cursor() as cur:
		cur.execute('SELECT * FROM total_stats;')
		data = cur.fetchall()
	old_data = restructure_data(data)

	# Get the players to update
	players_to_update, id_map = get_players_to_update(new_data, old_data)

	# Create and insert the daily entries
	daily_entries = create_daily_entries(players_to_update, old_data, date)
	with get_cursor() as cur:
		query = '''
			INSERT INTO daily_stats (
				id, name, team, date, fpts, pts, reb, ast, stl, blk, tov, fgm, fga, fg3m, fg3a, ftm, fta, min, rost_pct
			) VALUES %s
			'''
		psycopg2.extras.execute_values(cur, query, daily_entries)
		commit_connection()
	
	# Create and insert the total entries
	total_entries = create_total_entries(new_data, old_data, id_map, date)
	with get_cursor() as cur:
		query = '''
    INSERT INTO total_stats (
        id, name, team, date, fpts, pts, reb, ast, stl, blk, tov, fgm, fga, fg3m, fg3a, ftm, fta, min, gp, rost_pct
    ) VALUES %s
    ON CONFLICT (id) DO UPDATE SET
        name = EXCLUDED.name,
        team = EXCLUDED.team,
        date = EXCLUDED.date,
        fpts = EXCLUDED.fpts,
        pts = EXCLUDED.pts,
        reb = EXCLUDED.reb,
        ast = EXCLUDED.ast,
        stl = EXCLUDED.stl,
        blk = EXCLUDED.blk,
        tov = EXCLUDED.tov,
        fgm = EXCLUDED.fgm,
        fga = EXCLUDED.fga,
        fg3m = EXCLUDED.fg3m,
        fg3a = EXCLUDED.fg3a,
        ftm = EXCLUDED.ftm,
        fta = EXCLUDED.fta,
        min = EXCLUDED.min,
        gp = EXCLUDED.gp,
				rost_pct = EXCLUDED.rost_pct;
    '''
		psycopg2.extras.execute_values(cur, query, total_entries)

		# Update the previous rank, only for players who played on the date
		cur.execute('''
			UPDATE total_stats
			SET p_rank = c_rank
			WHERE id IN (
				SELECT id
				FROM total_stats
				WHERE date = %s
			);
			''', (date,))

		# Recalculate the rank for all players
		cur.execute('''
			UPDATE total_stats
			SET c_rank = new_rank
			FROM (
				SELECT id, ROW_NUMBER() OVER (ORDER BY fpts DESC) as new_rank
				FROM total_stats
			) AS subquery
			WHERE total_stats.id = subquery.id;
			''')
	
	commit_connection()
	print("ETL process completed")


# Queries the view to get the data for the frontend
@router.get("/etl/get_fpts_data")
async def get_fpts_data(cron_token: str):
	if not cron_token or cron_token != CRON_TOKEN:
		return {"data": []}
		
	with get_cursor() as cur:
		cur.execute('SELECT * FROM standings;')
		data = cur.fetchall()

	return {"data": serialize_fpts_data(data)}


# Route to trigger the ETL process for freeagent rostered percentages
@router.post('/etl/start-update-rostered')
async def start_ETL_update_rostered(req: ETLUpdateFTPSReq, background_tasks: BackgroundTasks):
	cron_token = req.cron_token
	background_tasks.add_task(trigger_ETL_update_rostered, cron_token)
	return {"message": "ETL process started"}
# Async trigger
async def trigger_ETL_update_rostered(cron_token: str):
	await update_rostered(ETLUpdateFTPSReq(cron_token=cron_token))
# Actual ETL process
async def update_rostered(req: ETLUpdateFTPSReq):
	cron_token = req.cron_token
	if cron_token != CRON_TOKEN:
		print("Invalid token")
		return
	
	# Fetch the nba player data and clean it
	cleaned_data = fetch_espn_rostered_data(int(LEAGUE_ID), 2025)
	
  # Create the entries for the rostered percentages
	entries = create_rostered_entries(cleaned_data)
	
    # Insert the entries into the DB
	query = '''
        INSERT INTO freeagents (espn_id, name, team, date, rostered_pct) VALUES %s
    '''
	with get_cursor() as cur:
		psycopg2.extras.execute_values(cur, query, entries)
		commit_connection()
	
	print("ETL process completed")