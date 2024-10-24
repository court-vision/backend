from .db_helpers.models import UserCreateResp, UserLoginReq, UserLoginResp, TeamGetResp, TeamAddReq, TeamAddResp, TeamRemoveReq, TeamRemoveResp, TeamUpdateReq, TeamUpdateResp, UserUpdateReq, UserUpdateResp, UserDeleteResp, GenerateLineupReq, GenerateLineupResp, SaveLineupReq, SaveLineupResp, GetLineupsResp, DeleteLineupResp, VerifyEmailReq, CheckCodeReq, UserDeleteReq, ETLUpdateFTPSReq, ETLUpdateFTPSResp
from .db_helpers.utils import hash_password, check_password, create_access_token, get_current_user, serialize_league_info, serialize_lineup_info, generate_lineup_hash, deserialize_lineups, generate_verification_code, send_verification_email, get_game_ids, get_game_stats, serialize_fpts_data
from .constants import ACCESS_TOKEN_EXPIRE_DAYS, FEATURES_SERVER_ENDPOINT, DB_CREDENTIALS, SELF_ENDPOINT, CRON_TOKEN, FRONTEND_API_ENDPOINT, LOCAL_API_ENDPOINT
from .data_helpers.utils import check_league
from datetime import datetime, timedelta
from passlib.context import CryptContext
from fastapi import APIRouter, Depends, BackgroundTasks
from contextlib import contextmanager
import pandas as pd
import psycopg2
import requests
import asyncio
import httpx
import time
import json
import os


router = APIRouter()

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Connect to the PostgreSQL database
def connect_to_db() -> psycopg2.connect:

	conn = psycopg2.connect(
		user=DB_CREDENTIALS["user"],
		password=DB_CREDENTIALS["password"],
		host=DB_CREDENTIALS["host"],
		port=DB_CREDENTIALS["port"],
		database=DB_CREDENTIALS["database"]
		)
	return conn

# Get the cursor for the database and close it when done
@contextmanager
def get_cursor():
    cur = conn.cursor()
    try:
        yield cur
    finally:
        cur.close()

conn = connect_to_db()


# ----------------------------------- User Authentication ----------------------------------- #

@router.post('/users/verify/send-email')
async def verify_email(req: VerifyEmailReq):
	email = req.email
	hashed_password = hash_password(req.password)

	# Check if the email is already in use
	with get_cursor() as cur:
		cur.execute("SELECT timestamp FROM verifications WHERE email = %s LIMIT 1", (email,))
		verification_data = cur.fetchone()
		
		if verification_data:
			if time.time() - verification_data[0] < 300:
				return {"success": True, "already_in_use": True}
			else:
				cur.execute("DELETE FROM verifications WHERE email = %s AND type = 'email'", (email,))
				conn.commit()

		cur.execute("SELECT * FROM users WHERE email = %s LIMIT 1", (email,))
		data = cur.fetchone()
		print(data)
		already_exists = bool(data)

		if already_exists:
			print("Already exists in users")
			return {"success": False, "already_in_use": True}
	
		# Generate the verification code
		code = generate_verification_code()
		cur.execute("INSERT INTO verifications (email, code, hashed_password, timestamp, type) VALUES (%s, %s, %s, %s, %s)", (email, code, hashed_password, int(time.time()), "email"))
		conn.commit()

	# Send the verification email
	res = send_verification_email(email, code)
	if res.get("success"):
		return {"success": True, "already_in_use": False}
	else:
		return {"success": False, "already_in_use": False}

@router.post('/users/verify/check-code')
async def check_verification_code(req: CheckCodeReq):
	email = req.email
	code = req.code

	print(email, code)

	with get_cursor() as cur:
		cur.execute("SELECT code, hashed_password, timestamp FROM verifications WHERE email = %s LIMIT 1", (email,))
		verification_data = cur.fetchone()
		if not verification_data:
			return {"success": False, "valid": False}
		
		access_code, hashed_password, timestamp = verification_data
		if access_code != code or time.time() - timestamp > 300:
			return {"success": True, "valid": False}
		
		# Delete the verification data
		cur.execute("DELETE FROM verifications WHERE email = %s", (email,))
		conn.commit()

	resp = create_user(email, hashed_password)
	return resp

def create_user(email: str, hashed_password: str):
	
	with get_cursor() as cur:
			cur.execute("SELECT * FROM users WHERE email = %s LIMIT 1", (email,))
			already_exists = bool(cur.fetchone())
			
			if already_exists:
					return UserCreateResp(access_token=None, already_exists=True, success=True, valid=True)
			
			cur.execute("INSERT INTO users (email, password) VALUES (%s, %s) RETURNING user_id", (email, hashed_password))
			user_id = cur.fetchone()[0]
			conn.commit()

			access_token = create_access_token({"uid": user_id, "email": email, "exp": datetime.now() + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)})
		
	return UserCreateResp(access_token=access_token, already_exists=False, success=True, valid=True)


@router.post('/users/login')
async def login_user(user: UserLoginReq):
	email = user.email
	password = user.password
	
	with get_cursor() as cur:
		cur.execute("SELECT user_id, password FROM users WHERE email = %s LIMIT 1", (email,))
		user_data = cur.fetchone()

		if not user_data or not check_password(password, user_data[1]):
			return UserLoginResp(access_token="", success=False)
			
		user_id = user_data[0]

		access_token = create_access_token({"uid": user_id, "email": email})

	return UserLoginResp(access_token=access_token, success=True)

@router.get('/users/me')
async def get_me(current_user: dict = Depends(get_current_user)):
	return current_user

# ------------------------------------ Team Management -------------------------------------- #

@router.get('/teams')
async def get_teams(current_user: dict = Depends(get_current_user)):
	user_id = current_user.get("uid")

	with get_cursor() as cur:
		cur.execute("SELECT team_id, team_info FROM teams WHERE user_id = %s", (user_id,))
		data = cur.fetchall()

		teams = []
		for team in data:
			team_id, team_info = team
			teams.append({"team_id": team_id, "team_info": team_info})

	return TeamGetResp(teams=teams)

@router.post('/teams/add')
async def add_team(team_info: TeamAddReq, current_user: dict = Depends(get_current_user)):
	user_id = current_user.get("uid")

	league_info = team_info.league_info
	team_identifier = str(league_info.league_id) + league_info.team_name

	if not check_league(league_info).valid:
		return TeamAddResp(team_id=None, already_exists=False)
	
	with get_cursor() as cur:
		cur.execute("SELECT * FROM teams WHERE user_id = %s AND team_identifier = %s LIMIT 1", (user_id, team_identifier))
		already_exists = bool(cur.fetchone())
		
		if already_exists:
			return TeamAddResp(team_id=None, already_exists=True)
		
		# Handle private league info
		cur.execute("INSERT INTO teams (user_id, team_identifier, team_info) VALUES (%s, %s, %s) RETURNING team_id", (user_id, team_identifier, serialize_league_info(league_info)))
		team_id = cur.fetchone()[0]
		conn.commit()

	return TeamAddResp(team_id=team_id, already_exists=False)

@router.delete('/teams/remove')
async def remove_team(team_info: TeamRemoveReq, current_user: dict = Depends(get_current_user)):
	user_id = current_user.get("uid")

	team_id = team_info.team_id

	with get_cursor() as cur:
		cur.execute("DELETE FROM teams WHERE user_id = %s AND team_id = %s", (user_id, team_id))
		conn.commit()
	
	return TeamRemoveResp(success=True)

@router.put('/teams/update')
async def update_team(team_info: TeamUpdateReq, current_user: dict = Depends(get_current_user)):
	user_id = current_user.get("uid")
	
	if not check_league(team_info.league_info).valid:
		return TeamUpdateResp(success=False)

	league_info = team_info.league_info

	with get_cursor() as cur:
		cur.execute("UPDATE teams SET team_info = %s WHERE user_id = %s AND team_id = %s", (serialize_league_info(league_info), user_id, team_info.team_id))
		conn.commit()

	return TeamUpdateResp(success=True)

@router.get('/teams/view')
async def view_team(team_id: int, current_user: dict = Depends(get_current_user)):
	#TODO: Fetch the roster data from the ESPN API, maybe save it to the database, no don't do that, rosters can change

	with get_cursor() as cur:
		cur.execute("SELECT team_info FROM teams WHERE team_id = %s", (team_id,))
		team_info = cur.fetchone()[0]

	async with httpx.AsyncClient() as client:
		resp = await client.post(f"{SELF_ENDPOINT}/data/get_roster_data", json={"league_info": team_info, "fa_count": 0})
	
	return resp.json()

# ------------------------------------ User Management -------------------------------------- #

@router.post('/users/delete')
async def delete_user(user: UserDeleteReq, current_user: dict = Depends(get_current_user)):
	user_id = current_user.get("uid")
	password = user.password

	with get_cursor() as cur:
		cur.execute("SELECT password FROM users WHERE uid = %s LIMIT 1", (user_id,))
		user_data = cur.fetchone()

		if not user_data or not check_password(password, user_data[0]):
			return UserDeleteResp(success=False)

		cur.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
		cur.execute("DELETE FROM teams WHERE user_id = %s", (user_id,))
		conn.commit()

	return UserDeleteResp(success=True)

@router.post('/users/update')
async def update_user(user_info: UserUpdateReq, current_user: dict = Depends(get_current_user)):
	user_id = current_user.get("uid")

	email = user_info.email
	password = user_info.password

	with get_cursor() as cur:
		if email:
			cur.execute("UPDATE users SET email = %s WHERE user_id = %s", (email, user_id))
		if password:
			hashed_password = hash_password(password)
			cur.execute("UPDATE users SET password = %s WHERE user_id = %s", (hashed_password, user_id))
		conn.commit()
	
	return UserUpdateResp(success=True)

# ----------------------------------- Lineup Management ------------------------------------- #

# Routes to the features server to generate a lineup
@router.post('/lineups/generate')
def generate_lineup(req: GenerateLineupReq, current_user: dict = Depends(get_current_user)):
	user_id = current_user.get("uid")

	with get_cursor() as cur:
		cur.execute("SELECT team_info FROM teams WHERE user_id = %s AND team_id = %s LIMIT 1", (user_id, req.selected_team))
		league_info = cur.fetchone()[0]
	
	endpoint = FEATURES_SERVER_ENDPOINT + "/generate-lineup"

	body = GenerateLineupResp(
		league_id=league_info['league_id'], 
		team_name=league_info['team_name'], 
		espn_s2=league_info['espn_s2'], 
		swid=league_info['swid'], 
		year=league_info['year'],
		threshold=req.threshold,
		week=req.week
		)
	
	resp = requests.post(endpoint, json=body.dict())
	return resp.json()

@router.get('/lineups')
async def get_lineups(selected_team: int, current_user: dict = Depends(get_current_user)):
	user_id = current_user.get("uid")

	with get_cursor() as cur:
		cur.execute("SELECT lineup_id, lineup_info FROM teams INNER JOIN lineups ON teams.team_id = lineups.team_id WHERE teams.user_id = %s AND teams.team_id = %s", (user_id, selected_team))
		lineups = cur.fetchall()

		if not lineups:
			return GetLineupsResp(lineups=None, no_lineups=True)
	
	return GetLineupsResp(lineups=deserialize_lineups(lineups), no_lineups=False)

@router.put('/lineups/save')
async def save_lineup(req: SaveLineupReq, current_user: dict = Depends(get_current_user)):
	user_id = current_user.get("uid")

	try:
		with get_cursor() as cur:
			# Check if the lineup already exists
			lineup_hash = generate_lineup_hash(req.lineup_info)

			cur.execute("SELECT * FROM teams INNER JOIN lineups ON teams.team_id = lineups.team_id WHERE teams.user_id = %s AND lineup_hash = %s", (user_id, lineup_hash))
			already_exists = bool(cur.fetchone())
			if already_exists:
				return SaveLineupResp(success=False, already_exists=True)

			# Else, save the lineup
			cur.execute("INSERT INTO lineups (team_id, lineup_info, lineup_hash) VALUES (%s, %s, %s)", (req.selected_team, serialize_lineup_info(req.lineup_info), lineup_hash))
			conn.commit()

	except Exception as _:
		return SaveLineupResp(success=False, already_exists=False)

	return SaveLineupResp(success=True, already_exists=False)

@router.delete('/lineups/remove')
async def remove_lineup(lineup_id: int, current_user: dict = Depends(get_current_user)):

	with get_cursor() as cur:
		cur.execute("DELETE FROM lineups WHERE lineup_id = %s RETURNING lineup_hash", (lineup_id,))
		lineup_hash = cur.fetchone()[0]
		if not lineup_hash:
			return DeleteLineupResp(success=False)
		conn.commit()

	return DeleteLineupResp(success=True)


# ------------------------------------------ ETL -------------------------------------------- #

# Route to kick-off the ETL process, returning something quick to avoid timeouts on the frontend
@router.post('/etl/start-update-fpts')
async def start_ETL_update_fpts(req: ETLUpdateFTPSReq, background_tasks: BackgroundTasks):
	cron_token = req.cron_token
	background_tasks.add_task(trigger_ETL_update_fpts, cron_token)
	return {"message": "ETL process started"}

# Async trigger
async def trigger_ETL_update_fpts(cron_token: str):
	await update_fpts(ETLUpdateFTPSReq(cron_token=cron_token))

# Route to handle the actual ETL process
async def update_fpts(req: ETLUpdateFTPSReq):
	cron_token = req.cron_token
	if cron_token != CRON_TOKEN:
		print("Invalid token")
		return
	game_date, game_ids = get_game_ids()

	# Insert the new daily data into the database
	for game_id in game_ids:
		print(game_id)
		game_stats = get_game_stats(game_id)
		if game_stats is None:
			continue

		# Update the database with the new stats
		with get_cursor() as cur:
			for index, row in game_stats.iterrows():
				cur.execute('INSERT INTO daily_fantasy_points (player_id, player_name, date, fantasy_points) VALUES (%s, %s, %s, %s)', (row['PLAYER_ID'], row['PLAYER_NAME'], game_date, row['Fantasy Score']))
			conn.commit()
		
		# Sleep for a bit so we don't get rate limited
		await asyncio.sleep(10)
	
	# Now update the total data in the database
	with get_cursor() as cur:
		cur.execute('''
    INSERT INTO player_total_points (player_id, player_name, total_points, avg_points)
    SELECT player_id, MAX(player_name), SUM(fantasy_points), AVG(fantasy_points)
    FROM daily_fantasy_points
    GROUP BY player_id
    ON CONFLICT (player_id) DO UPDATE
    SET total_points = EXCLUDED.total_points, avg_points = EXCLUDED.avg_points, player_name = EXCLUDED.player_name
		''')
		conn.commit()

	# Now select the updated data using the view
	with get_cursor() as cur:
		cur.execute('SELECT * FROM player_standings ORDER BY rank')
		data = cur.fetchall()
	
	with open('file-storage/fpts_data.json', 'w') as f:
		json.dump([{
			"rank": player[0],
			"player_id": player[1],
			"player_name": player[2],
			"total_points": float(player[3]),
			"avg_points": round(float(player[4]), 1)
		} for player in data], f, indent=2)

# ----------------------------------- Squeel Workbench -------------------------------------- #