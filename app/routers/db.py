from .db_helpers.models import UserCreateReq, UserCreateResp, UserLoginReq, UserLoginResp, TeamGetResp, TeamAddReq, TeamAddResp, TeamRemoveReq, TeamRemoveResp, TeamUpdateReq, TeamUpdateResp, UserUpdateReq, UserUpdateResp, UserDeleteResp
from .db_helpers.utils import conn, get_cursor, hash_password, check_password, create_access_token, get_current_user
from .constants import ACCESS_TOKEN_EXPIRE_DAYS
from .data_helpers.utils import check_league
from datetime import datetime, timedelta
from passlib.context import CryptContext
from fastapi import APIRouter, Depends
import json


router = APIRouter()

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

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
def get_me(current_user: dict = Depends(get_current_user)):
	return current_user

# ------------------------------------ Team Management -------------------------------------- #

@router.get('/teams')
def get_teams(current_user: dict = Depends(get_current_user)):
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
def add_team(team_info: TeamAddReq, current_user: dict = Depends(get_current_user)):
	user_id = current_user.get("uid")

	league_info = team_info.league_info
	team_identifier = str(league_info.league_id) + league_info.team_name

	if not check_league(league_info):
		return TeamAddResp(team_id=None, already_exists=False)
	
	with get_cursor() as cur:
		cur.execute("SELECT * FROM teams WHERE user_id = %s AND team_identifier = %s LIMIT 1", (user_id, team_identifier))
		already_exists = bool(cur.fetchone())
		
		if already_exists:
			return TeamAddResp(team_id=None, already_exists=True)
		
		cur.execute("INSERT INTO teams (user_id, team_identifier, team_info) VALUES (%s, %s, %s) RETURNING team_id", (user_id, team_identifier, json.dumps(team_info)))
		team_id = cur.fetchone()[0]
		conn.commit()

	return TeamAddResp(team_id=team_id, already_exists=False)

@router.post('/teams/remove')
def remove_team(team_info: TeamRemoveReq, current_user: dict = Depends(get_current_user)):
	user_id = current_user.get("uid")

	team_id = team_info.team_id

	with get_cursor() as cur:
		cur.execute("DELETE FROM teams WHERE user_id = %s AND team_id = %s", (user_id, team_id))
		conn.commit()
	
	return TeamRemoveResp(success=True)

@router.post('/teams/update')
def update_team(team_info: TeamUpdateReq, current_user: dict = Depends(get_current_user)):
	user_id = current_user.get("uid")

	league_info = team_info.league_info

	with get_cursor() as cur:
		cur.execute("UPDATE teams SET team_info = %s WHERE user_id = %s AND team_id = %s", (json.dumps(league_info), user_id, team_info.team_id))
		conn.commit()

	return TeamUpdateResp(success=True)

# ------------------------------------ User Management -------------------------------------- #

@router.post('/users/delete')
def delete_user(current_user: dict = Depends(get_current_user)):
	user_id = current_user.get("uid")

	with get_cursor() as cur:
		cur.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
		cur.execute("DELETE FROM teams WHERE user_id = %s", (user_id,))
		conn.commit()

	return UserDeleteResp(success=True)

@router.post('/users/update')
def update_user(user_info: UserUpdateReq, current_user: dict = Depends(get_current_user)):
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