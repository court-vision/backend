from .db_helpers.models import UserCreateResp, UserLoginReq, UserLoginResp, TeamGetResp, TeamAddReq, TeamAddResp, TeamRemoveReq, TeamRemoveResp, TeamUpdateReq, TeamUpdateResp, UserUpdateReq, UserUpdateResp, UserDeleteResp, GenerateLineupReq, GenerateLineupResp, SaveLineupReq, SaveLineupResp, GetLineupsResp, DeleteLineupResp, VerifyEmailReq, CheckCodeReq, UserDeleteReq, VerifyEmailResp, CheckCodeResp
from .base_models import success_response, error_response, ApiStatus, AuthResponse, VerificationResponse, UserResponse
from .db_helpers.utils import hash_password, check_password, create_access_token, get_current_user, serialize_league_info, serialize_lineup_info, generate_lineup_hash, deserialize_lineups, generate_verification_code, send_verification_email
from .constants import ACCESS_TOKEN_EXPIRE_DAYS, FEATURES_SERVER_ENDPOINT, SELF_ENDPOINT
from .data_helpers.utils import check_league
from datetime import datetime, timedelta
from passlib.context import CryptContext
from fastapi import APIRouter, Depends
import requests
import httpx
import time
from app.db.models import User, Verification, Team, Lineup


router = APIRouter()

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Database connection is now handled by Peewee models
# No need for connection pooling with psycopg2

# ----------------------------------- User Authentication ----------------------------------- #

@router.post('/users/verify/send-email', response_model=VerifyEmailResp)
async def verify_email(req: VerifyEmailReq):
	try:
		email = req.email
		hashed_password = hash_password(req.password)

	except Exception as validation_error:
		print(f"Validation error: {validation_error}")
		return VerifyEmailResp(
			status=ApiStatus.VALIDATION_ERROR,
			message="Invalid request data",
			error_code="VALIDATION_ERROR"
		)

	# Check if the email is already in use
	try:
		verification_data = Verification.select().where(Verification.email == email).first()
		
		if verification_data:
			if time.time() - verification_data.timestamp < 300:
				return VerifyEmailResp(
					status=ApiStatus.SUCCESS,
					message="Verification email already sent recently",
					data=VerificationResponse(
						verification_sent=True,
						email=email,
						expires_in_seconds=300 - int(time.time() - verification_data.timestamp)
					)
				)
			else:
				verification_data.delete_instance()

		user_exists = User.select().where(User.email == email).exists()
		
		if user_exists:
			print("Already exists in users")
			return VerifyEmailResp(
				status=ApiStatus.CONFLICT,
				message="Email address is already registered",
				error_code="EMAIL_ALREADY_EXISTS"
			)
		
		# Generate the verification code
		code = generate_verification_code()
		
		# Send the verification email FIRST before creating the database record
		res = send_verification_email(email, code)
		if not res.get("success"):
			print(f"Error in verify_email 1: {res.get('error')}")
			return VerifyEmailResp(
				status=ApiStatus.SERVER_ERROR,
				message="Failed to send verification email",
				error_code="EMAIL_SEND_FAILED"
			)
		
		# Only create the verification record if email sending was successful
		Verification.create(
			email=email,
			code=code,
			hashed_password=hashed_password,
			timestamp=int(time.time()),
			type="email"
		)
		
		return VerifyEmailResp(
			status=ApiStatus.SUCCESS,
			message="Verification email sent successfully",
			data=VerificationResponse(
				verification_sent=True,
				email=email,
				expires_in_seconds=300
			)
		)
			
	except Exception as e:
		print(f"Error in verify_email 2: {e}")
		return VerifyEmailResp(
			status=ApiStatus.SERVER_ERROR,
			message="Internal server error during email verification",
			error_code="INTERNAL_ERROR"
		)

@router.post('/users/verify/check-code', response_model=CheckCodeResp)
async def check_verification_code(req: CheckCodeReq):
	email = req.email
	code = req.code

	try:
		verification_data = Verification.select().where(Verification.email == email).first()
		if not verification_data:
			return CheckCodeResp(
				status=ApiStatus.NOT_FOUND,
				message="No verification request found for this email",
				error_code="VERIFICATION_NOT_FOUND"
			)
		
		if verification_data.code != code or time.time() - verification_data.timestamp > 300:
			return CheckCodeResp(
				status=ApiStatus.ERROR,
				message="Invalid or expired verification code",
				error_code="INVALID_VERIFICATION_CODE"
			)
		
		# Delete the verification data
		verification_data.delete_instance()
		
		resp = create_user(email, verification_data.hashed_password)
		if resp.success:
			return CheckCodeResp(
				status=ApiStatus.SUCCESS,
				message="Account created successfully",
				data=AuthResponse(
					access_token=resp.access_token,
					email=email,
					expires_at=(datetime.now() + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)).isoformat()
				)
			)
		else:
			return CheckCodeResp(
				status=ApiStatus.SERVER_ERROR,
				message="Failed to create account",
				error_code="ACCOUNT_CREATION_FAILED"
			)
		
	except Exception as e:
		print(f"Error in check_verification_code: {e}")
		return CheckCodeResp(
			status=ApiStatus.SERVER_ERROR,
			message="Internal server error during verification",
			error_code="INTERNAL_ERROR"
		)

def create_user(email: str, hashed_password: str):
	try:
		user_exists = User.select().where(User.email == email).exists()
		
		if user_exists:
			return UserCreateResp(access_token=None, already_exists=True, success=True, valid=True)
		
		user = User.create(
			email=email,
			password=hashed_password,
			created_at=datetime.now()
		)
		
		access_token = create_access_token({"uid": user.user_id, "email": email, "exp": datetime.now() + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)})
		
		return UserCreateResp(access_token=access_token, already_exists=False, success=True, valid=True)
		
	except Exception as e:
		print(f"Error in create_user: {e}")
		return UserCreateResp(access_token=None, already_exists=False, success=False, valid=False)


@router.post('/users/login', response_model=UserLoginResp)
async def login_user(user: UserLoginReq):
	email = user.email
	password = user.password
	
	try:
		user_data = User.select().where(User.email == email).first()

		if not user_data or not check_password(password, user_data.password):
			return UserLoginResp(
				status=ApiStatus.AUTHENTICATION_ERROR,
				message="Invalid email or password",
				error_code="INVALID_CREDENTIALS"
			)
			
		access_token = create_access_token({"uid": user_data.user_id, "email": email})

		return UserLoginResp(
			status=ApiStatus.SUCCESS,
			message="Login successful",
			data=AuthResponse(
				access_token=access_token,
				user_id=user_data.user_id,
				email=email,
				expires_at=(datetime.now() + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)).isoformat()
			)
		)
		
	except Exception as e:
		print(f"Error in login_user: {e}")
		return UserLoginResp(
			status=ApiStatus.SERVER_ERROR,
			message="Internal server error during login",
			error_code="INTERNAL_ERROR"
		)

@router.get('/users/verify/auth-check')
async def auth_check(current_user: dict = Depends(get_current_user)):
	return success_response(
		message="Authentication successful",
		data={"user_id": current_user.get("uid"), "email": current_user.get("email")}
	)

# ------------------------------------ Team Management -------------------------------------- #

@router.get('/teams')
async def get_teams(current_user: dict = Depends(get_current_user)):
	user_id = current_user.get("uid")

	try:
		teams_query = Team.select().where(Team.user_id == user_id)
		teams = []
		
		for team in teams_query:
			teams.append({"team_id": team.team_id, "team_info": team.team_info})

		return TeamGetResp(teams=teams)
		
	except Exception as e:
		print(f"Error in get_teams: {e}")
		return TeamGetResp(teams=[])

@router.post('/teams/add')
async def add_team(team_info: TeamAddReq, current_user: dict = Depends(get_current_user)):
	user_id = current_user.get("uid")

	league_info = team_info.league_info
	team_identifier = str(league_info.league_id) + league_info.team_name

	if not check_league(league_info).valid:
		return TeamAddResp(team_id=None, already_exists=False)
	
	try:
		team_exists = Team.select().where(
			(Team.user_id == user_id) & (Team.team_identifier == team_identifier)
		).exists()
		
		if team_exists:
			return TeamAddResp(team_id=None, already_exists=True)
		
		# Create new team
		team = Team.create(
			user_id=user_id,
			team_identifier=team_identifier,
			team_info=serialize_league_info(league_info)
		)

		return TeamAddResp(team_id=team.team_id, already_exists=False)
		
	except Exception as e:
		print(f"Error in add_team: {e}")
		return TeamAddResp(team_id=None, already_exists=False)

@router.delete('/teams/remove')
async def remove_team(team_info: TeamRemoveReq, current_user: dict = Depends(get_current_user)):
	user_id = current_user.get("uid")
	team_id = team_info.team_id

	try:
		Team.delete().where(
			(Team.user_id == user_id) & (Team.team_id == team_id)
		).execute()
		
		return TeamRemoveResp(success=True)
		
	except Exception as e:
		print(f"Error in remove_team: {e}")
		return TeamRemoveResp(success=False)

@router.put('/teams/update')
async def update_team(team_info: TeamUpdateReq, current_user: dict = Depends(get_current_user)):
	user_id = current_user.get("uid")
	
	if not check_league(team_info.league_info).valid:
		return TeamUpdateResp(success=False)

	league_info = team_info.league_info

	try:
		Team.update(team_info=serialize_league_info(league_info)).where(
			(Team.user_id == user_id) & (Team.team_id == team_info.team_id)
		).execute()

		return TeamUpdateResp(success=True)
		
	except Exception as e:
		print(f"Error in update_team: {e}")
		return TeamUpdateResp(success=False)

@router.get('/teams/view')
async def view_team(team_id: int):
	#TODO: Fetch the roster data from the ESPN API, maybe save it to the database, no don't do that, rosters can change

	try:
		team = Team.select().where(Team.team_id == team_id).first()
		if not team:
			return {"error": "Team not found"}

		async with httpx.AsyncClient() as client:
			resp = await client.post(f"{SELF_ENDPOINT}/data/get_roster_data", json={"league_info": team.team_info, "fa_count": 0})
		
		return resp.json()
		
	except Exception as e:
		print(f"Error in view_team: {e}")
		return {"error": "Failed to fetch team data"}

# ------------------------------------ User Management -------------------------------------- #

@router.post('/users/delete')
async def delete_user(user: UserDeleteReq, current_user: dict = Depends(get_current_user)):
	user_id = current_user.get("uid")
	password = user.password

	try:
		user_data = User.select().where(User.user_id == user_id).first()

		if not user_data or not check_password(password, user_data.password):
			return UserDeleteResp(success=False)

		# Delete user (teams will be deleted automatically due to CASCADE)
		User.delete().where(User.user_id == user_id).execute()

		return UserDeleteResp(success=True)
		
	except Exception as e:
		print(f"Error in delete_user: {e}")
		return UserDeleteResp(success=False)

@router.post('/users/update')
async def update_user(user_info: UserUpdateReq, current_user: dict = Depends(get_current_user)):
	user_id = current_user.get("uid")

	email = user_info.email
	password = user_info.password

	try:
		update_data = {}
		if email:
			update_data['email'] = email
		if password:
			update_data['password'] = hash_password(password)
		
		if update_data:
			User.update(**update_data).where(User.user_id == user_id).execute()
	
		return UserUpdateResp(success=True)
		
	except Exception as e:
		print(f"Error in update_user: {e}")
		return UserUpdateResp(success=False)

# ----------------------------------- Lineup Management ------------------------------------- #

# Routes to the features server to generate a lineup
@router.post('/lineups/generate')
def generate_lineup(req: GenerateLineupReq, current_user: dict = Depends(get_current_user)):
	user_id = current_user.get("uid")

	try:
		team = Team.select().where(
			(Team.user_id == user_id) & (Team.team_id == req.selected_team)
		).first()
		
		if not team:
			return {"error": "Team not found"}
		
		endpoint = FEATURES_SERVER_ENDPOINT + "/generate-lineup"

		body = GenerateLineupResp(
			league_id=team.team_info['league_id'], 
			team_name=team.team_info['team_name'], 
			espn_s2=team.team_info['espn_s2'], 
			swid=team.team_info['swid'], 
			year=team.team_info['year'],
			threshold=req.threshold,
			week=req.week
		)
		
		resp = requests.post(endpoint, json=body.dict())
		return resp.json()
		
	except Exception as e:
		print(f"Error in generate_lineup: {e}")
		return {"error": "Failed to generate lineup"}

@router.get('/lineups')
async def get_lineups(selected_team: int, current_user: dict = Depends(get_current_user)):
	user_id = current_user.get("uid")

	try:
		# Join teams and lineups tables
		lineups_query = (Lineup
			.select(Lineup.lineup_id, Lineup.lineup_info)
			.join(Team, on=(Lineup.team_id == Team.team_id))
			.where((Team.user_id == user_id) & (Team.team_id == selected_team)))
		
		lineups = list(lineups_query)

		if not lineups:
			return GetLineupsResp(lineups=None, no_lineups=True)
		
		# Convert to the format expected by deserialize_lineups
		lineup_data = [(lineup.lineup_id, lineup.lineup_info) for lineup in lineups]
	
		return GetLineupsResp(lineups=deserialize_lineups(lineup_data), no_lineups=False)
		
	except Exception as e:
		print(f"Error in get_lineups: {e}")
		return GetLineupsResp(lineups=None, no_lineups=True)

@router.put('/lineups/save')
async def save_lineup(req: SaveLineupReq, current_user: dict = Depends(get_current_user)):
	user_id = current_user.get("uid")

	try:
		# Check if the lineup already exists
		lineup_hash = generate_lineup_hash(req.lineup_info)

		lineup_exists = (Lineup
			.select()
			.join(Team, on=(Lineup.team_id == Team.team_id))
			.where((Team.user_id == user_id) & (Lineup.lineup_hash == lineup_hash))
			.exists())
			
		if lineup_exists:
			return SaveLineupResp(success=False, already_exists=True)

		# Save the lineup
		Lineup.create(
			team_id=req.selected_team,
			lineup_info=serialize_lineup_info(req.lineup_info),
			lineup_hash=lineup_hash
		)

		return SaveLineupResp(success=True, already_exists=False)

	except Exception as e:
		print(f"Error in save_lineup: {e}")
		return SaveLineupResp(success=False, already_exists=False)

@router.delete('/lineups/remove')
async def remove_lineup(lineup_id: int, current_user: dict = Depends(get_current_user)):

	try:
		lineup = Lineup.select().where(Lineup.lineup_id == lineup_id).first()
		if not lineup:
			return DeleteLineupResp(success=False)
		
		lineup.delete_instance()
		return DeleteLineupResp(success=True)
		
	except Exception as e:
		print(f"Error in remove_lineup: {e}")
		return DeleteLineupResp(success=False)


# ------------------------------------------ ETL -------------------------------------------- #

# ----------------------------------- Squeel Workbench -------------------------------------- #
