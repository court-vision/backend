from ..constants import SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_DAYS, PROXY_STRING
from ..libs.nba_api.stats.endpoints import scoreboardv2, boxscoretraditionalv2, leagueleaders
from .models import LeagueInfo, LineupInfo, SlimPlayer, SlimGene, FPTSPlayer
from fastapi.security import OAuth2PasswordBearer
from fastapi import HTTPException, Depends
from datetime import datetime, timedelta
from sendgrid.helpers.mail import Mail
from sendgrid import SendGridAPIClient
from contextlib import contextmanager
from jose import jwt, JWTError
from typing import Optional
import pandas as pd
import psycopg2
import hashlib
import random
import bcrypt
import pytz
import json
import ssl
import os

# ---------------------- User Authentication ---------------------- #

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

# Generate a verification code
def generate_verification_code() -> str:
	return '{:06d}'.format(random.randint(0, 999999))

# Send the verification email
def send_verification_email(to_email: str, code: str) -> dict:
	message = Mail(
		from_email='Court Vision <mail@courtvision.dev>',
		to_emails=to_email,
		subject='Email Verification',
		html_content=f'<strong>Please verify your email by entering the following code: {code}</strong>')
	try:
		sg = SendGridAPIClient(os.environ.get('SENDGRID_API_KEY'))
		response = sg.send(message)
		print(response.status_code)

		return {"success": True}
	except Exception as e:
		return {"success": False, "error": str(e)}

# Create access token for a user
def create_access_token(data: dict) -> str:
	to_encode = data.copy()
	to_encode.update({"exp": datetime.now() + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)})
	return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

# Verify the access token
def verify_access_token(token: str) -> Optional[dict]:
	try:
		payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
		return payload
	except JWTError:
		return None
	
# Get the data for the user
def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
	payload = verify_access_token(token)
	if payload is None:
		raise HTTPException(status_code=401, detail="Invalid access token")
	return payload

# ---------------------- Database Connection ---------------------- #


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

# --------------------- Encryption/Validation --------------------- #

def hash_password(password: str) -> str:
	return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def check_password(password: str, hashed_password: str) -> bool:
	return bcrypt.checkpw(password.encode('utf-8'), hashed_password.encode('utf-8'))

def generate_lineup_hash(lineup_info: LineupInfo) -> str:
	return hashlib.md5(serialize_lineup_info(lineup_info).encode('utf-8')).hexdigest()

# --------------------------- Testing ----------------------------- #
# ------------------------ Serialization -------------------------- #

def serialize_league_info(league_info: LeagueInfo) -> dict:
	return json.dumps({
		"league_id": league_info.league_id,
		"espn_s2": league_info.espn_s2,
		"swid": league_info.swid,
		"team_name": league_info.team_name,
		"league_name": league_info.league_name if league_info.league_name else "N/A",
		"year": league_info.year
	})

def deserialize_league_info(league_info: str) -> LeagueInfo:
	data = json.loads(league_info)
	return LeagueInfo(league_id=data['league_id'], espn_s2=data['espn_s2'], swid=data['swid'], team_name=data['team_name'], year=data['year'])

def serialize_lineup_info(lineup_info: LineupInfo) -> str:
	return json.dumps({
		"Timestamp": lineup_info.Timestamp,
		"Improvement": lineup_info.Improvement,
		"Week": lineup_info.Week,
		"Threshold": lineup_info.Threshold,
		"Lineup": [{
			"Day": gene.Day,
			"Additions": [{
				"Name": player.Name,
				"AvgPoints": player.AvgPoints,
				"Team": player.Team
			} for player in gene.Additions],
			"Removals": [{
				"Name": player.Name,
				"AvgPoints": player.AvgPoints,
				"Team": player.Team
			} for player in gene.Removals],
			"Roster": {
				player: {
					"Name": gene.Roster[player].Name,
					"AvgPoints": gene.Roster[player].AvgPoints,
					"Team": gene.Roster[player].Team
				} for player in gene.Roster
			}
		} for gene in lineup_info.Lineup]
	})

def deserialize_lineups(lineups: list[tuple]) -> list[LineupInfo]:
	return [LineupInfo(
		Id=lineup[0],
		Timestamp=lineup[1]['Timestamp'],
		Improvement=lineup[1]['Improvement'],
		Week=lineup[1]['Week'],
		Threshold=lineup[1]['Threshold'],
		Lineup=[
			SlimGene(
			Day=gene['Day'],
			Additions=[SlimPlayer(**player) for player in gene['Additions']],
			Removals=[SlimPlayer(**player) for player in gene['Removals']],
			Roster={pos: SlimPlayer(**player) for pos, player in gene['Roster'].items()}
		) for gene in lineup[1]['Lineup']]
	) for lineup in lineups]

def serialize_fpts_data(data: list[tuple]) -> str:
	return [FPTSPlayer(
		rank=player[0],
		player_id=player[1],
		player_name=player[2],
		total_fpts=player[3],
		avg_fpts=player[4],
		rank_change=player[5]
	) for player in data]

# ------------------------ ETL Helpers ------------------------ #


# Formula for the fantasy points of a player
def calculate_fantasy_points(stats):
	points_score = stats['pts']
	rebounds_score = stats['reb']
	assists_score = stats['ast'] * 2
	stocks_score = (stats['stl'] + stats['blk']) * 4
	turnovers_score = stats['tov'] * -2
	three_pointers_score = stats['fg3m']
	fg_eff_score = (stats['fgm'] * 2) - stats['fga']
	ft_eff_score = stats['ftm'] - stats['fta']
	return points_score + rebounds_score + assists_score + stocks_score + turnovers_score + three_pointers_score + fg_eff_score + ft_eff_score


# Get all the game IDs for the (previous) day
def get_game_ids() -> list[datetime, list[str]]:
	central_tz = pytz.timezone('US/Central')
	yesterday = datetime.now(central_tz) - timedelta(days=1)
	date_str = yesterday.strftime("%m-%d-%Y")
	date = datetime.strptime(date_str, "%m-%d-%Y")

	scoreboard = scoreboardv2.ScoreboardV2(game_date=date, proxy=PROXY_STRING)
	games = scoreboard.get_dict()['resultSets'][0]['rowSet']
	game_ids = [game[2] for game in games]

	return date, game_ids


# Gets the stats for the players in each game
def get_game_stats(game_id: str) -> pd.DataFrame:
	cols_to_drop = ['COMMENT', 'TEAM_CITY', 'NICKNAME', 'START_POSITION', 'FG_PCT', 'FT_PCT', 'FG3_PCT', 'OREB', 'DREB', 'PF', 'PLUS_MINUS']
	boxscore = boxscoretraditionalv2.BoxScoreTraditionalV2(game_id=game_id, proxy=PROXY_STRING)
	stats = boxscore.get_data_frames()[0]
	stats = stats.dropna()
	stats = stats.drop(columns=cols_to_drop)
	stats.loc[:, "Fantasy Score"] = stats.apply(calculate_fantasy_points, axis=1)

	return stats


# Fetches and restructures the data from the NBA API
def fetch_nba_data() -> dict:
	leaders = leagueleaders.LeagueLeaders(
		season='2024-25',
		per_mode48='Totals',
		stat_category_abbreviation='PTS'
	)
	updated = leaders.get_normalized_dict()['LeagueLeaders']

	# Create a new dictionary with the id as the key and also filter out the columns
	COLS_TO_KEEP = ['id', 'name', 'team', 'date', 'min', 'fpts', 'pts', 'reb', 'ast', 'stl', 'blk', 'tov', 'fgm', 'fga', 'fg3m', 'fg3a', 'ftm', 'fta', 'gp']
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
		'gp': player['GP']
		}
	
	return updated_dict


# Takes in the raw data from the database and returns a restrucuted dict that looks the same as the NBA API data
def restructure_data(data: list[tuple]) -> dict:
	old_dict = {}
	for player in data:
		old_dict[player[0]] = {
			'id': player[0],
			'name': player[1],
			'team': player[2],
			'date': player[3],
			'fpts': player[4],
			'pts': player[5],
			'reb': player[6],
			'ast': player[7],
			'stl': player[8],
			'blk': player[9],
			'tov': player[10],
			'fgm': player[11],
			'fga': player[12],
			'fg3m': player[13],
			'fg3a': player[14],
			'ftm': player[15],
			'fta': player[16],
			'min': player[17],
			'gp': player[18],
			'c_rank': player[19],
			'p_rank': player[20]
		}
		
	return old_dict


# Compare the data from the NBA API and the database to find the players who played
def get_players_to_update(api_data: dict, db_data: dict) -> tuple[list[dict], set]:
	had_game = []
	id_map = set()
	for id, d in api_data.items():
		# If the player is not in the db data, we know they played
		if id not in db_data:
			had_game.append(d)
			id_map.add(id)
			continue
		# If the player is in the old data, but 'gp' is different, we know they played
		if d['gp'] != db_data[id]['gp']:
			had_game.append(d)
			id_map.add(id)
	
	return had_game, id_map


# Creates the formatted entry for insertion into daily_stats
def create_daily_entry(old, new):
	return (old['id'],
				 old['name'],
				 old['team'],
				 new['date'],
				 new['fpts'] - old['fpts'],
				 new['pts'] - old['pts'],
				 new['reb'] - old['reb'],
				 new['ast'] - old['ast'],
				 new['stl'] - old['stl'],
				 new['blk'] - old['blk'],
				 new['tov'] - old['tov'],
				 new['fgm'] - old['fgm'],
				 new['fga'] - old['fga'],
				 new['fg3m'] - old['fg3m'],
				 new['fg3a'] - old['fg3a'],
				 new['ftm'] - old['ftm'],
				 new['fta'] - old['fta'],
				 new['min'] - old['min'],
	)


# Creates the formatted entry for insertion into daily_stats for a player who played but is not in the database
def create_single_daily_entry(new):
	return (new['id'],
				 	new['name'],
				 	new['team'],
				 	new['date'],
					new['fpts'],
					new['pts'],
					new['reb'],
					new['ast'],
					new['stl'],
					new['blk'],
					new['tov'],
					new['fgm'],
					new['fga'],
					new['fg3m'],
					new['fg3a'],
					new['ftm'],
					new['fta'],
					new['min'],
	)


# Creates the formatted entries for insertion into daily_stats
def create_daily_entries(had_game: list[dict], old_dict: dict, date: datetime) -> list[tuple]:
	entries = []

	for d in had_game:
		d['fpts'] = calculate_fantasy_points(d)
		d['date'] = date
		if d['id'] in old_dict:
			entries.append(create_daily_entry(old_dict[d['id']], d))
		else:
			entries.append(create_single_daily_entry(d))
		
	return entries


# Creates the formatted entries for insertion into total_stats
def create_total_entries(updated_dict: dict, old_dict: dict, id_map: set, today: datetime) -> list[tuple]:
	return [
		(
			id,
			d['name'],
			d['team'],
			today if id in id_map else old_dict[id]['date'],
			calculate_fantasy_points(d),
			d['pts'], d['reb'], d['ast'], d['stl'], d['blk'],
			d['tov'], d['fgm'], d['fga'], d['fg3m'], d['fg3a'],
			d['ftm'], d['fta'], d['min'], d['gp']
		)
		for id, d in updated_dict.items()
	]
		

# ------------------------ Networking ------------------------ #