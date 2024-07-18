from .db_helpers.models import UserCreateReq, UserCreateResp, UserLoginReq, UserLoginResp, TeamGetResp, TeamAddReq, TeamAddResp, TeamRemoveReq, TeamRemoveResp, TeamUpdateReq, TeamUpdateResp, UserUpdateReq, UserUpdateResp, UserDeleteResp, LineupInfo, GenerateLineupReq, GenerateLineupResp, SaveLineupReq, SaveLineupResp, GetLineupsResp
from .db_helpers.utils import hash_password, check_password, create_access_token, get_current_user, serialize_league_info, serialize_lineup_info, generate_lineup_hash, deserialize_lineups
from .constants import ACCESS_TOKEN_EXPIRE_DAYS, FEATURES_SERVER_ENDPOINT
from .data_helpers.utils import check_league
from datetime import datetime, timedelta
from passlib.context import CryptContext
from fastapi import APIRouter, Depends
from contextlib import contextmanager
import psycopg2
import requests


router = APIRouter()

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Connect to the PostgreSQL database
def connect_to_db() -> psycopg2.connect:
    conn = psycopg2.connect(
        user="jameslk3",
        password="REDACTED",
        host="cv-db.postgres.database.azure.com",
        port="5432",
        database="cv-db"
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

@router.post('/users/create')
async def create_user(user: UserCreateReq):
	email = user.email
	password = user.password
	
	with get_cursor() as cur:
			cur.execute("SELECT * FROM users WHERE email = %s LIMIT 1", (email,))
			already_exists = bool(cur.fetchone())
			
			if already_exists:
					return UserCreateResp(access_token=None, already_exists=True)
			
			hashed_password = hash_password(password)
			cur.execute("INSERT INTO users (email, password) VALUES (%s, %s) RETURNING user_id", (email, hashed_password))
			user_id = cur.fetchone()[0]
			conn.commit()

			access_token = create_access_token({"uid": user_id, "email": email, "exp": datetime.now() + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)})
		
	return UserCreateResp(access_token=access_token, already_exists=False)


@router.post('/users/login')
async def login_user(user: UserLoginReq):
	email = user.email
	password = user.password
	
	with get_cursor() as cur:
		cur.execute("SELECT user_id, password FROM users WHERE email = %s LIMIT 1", (email,))
		user_data = cur.fetchone()

		if not user_data or not check_password(password, user_data[1]):
			return UserLoginResp(access_token=None, success=False)
			
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

# ------------------------------------ User Management -------------------------------------- #

@router.post('/users/delete')
async def delete_user(current_user: dict = Depends(get_current_user)):
	user_id = current_user.get("uid")

	with get_cursor() as cur:
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

# ----------------------------------- Squeel Workbench -------------------------------------- #