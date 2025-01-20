from ..libs.nba_api.stats.endpoints import leagueleaders
from .models import LeagueInfo, ValidateLeagueResp, FPTSPlayer
from ..constants import ESPN_FANTASY_ENDPOINT
from functools import cached_property
from datetime import datetime
import requests
import json
import pytz

# ------------------------------------------ ESPN Fantasy Data ------------------------------------------
POSITION_MAP = {
    0: 'PG',
    1: 'SG',
    2: 'SF',
    3: 'PF',
    4: 'C',
    5: 'G',
    6: 'F',
    7: 'SG/SF',
    8: 'G/F',
    9: 'PF/C',
    10: 'F/C',
    11: 'UT',
    12: 'BE',
    13: 'IR',
    14: '',
    15: 'Rookie',
    # reverse
    'PG': 0,
    'SG': 1,
    'SF': 2,
    'PF': 3,
    'C': 4,
    'G': 5,
    'F': 6,
    'SG/SF': 7,
    'G/F': 8,
    'PF/C': 9,
    'F/C': 10,
    'UT': 11,
    'BE': 12,
    'IR': 13,
    'Rookie': 15
}

PRO_TEAM_MAP = {
    0: 'FA',
    1: 'ATL',
    2: 'BOS',
    3: 'NOP',
    4: 'CHI',
    5: 'CLE',
    6: 'DAL',
    7: 'DEN',
    8: 'DET',
    9: 'GSW',
    10: 'HOU',
    11: 'IND',
    12: 'LAC',
    13: 'LAL',
    14: 'MIA',
    15: 'MIL',
    16: 'MIN',
    17: 'BKN',
    18: 'NYK',
    19: 'ORL',
    20: 'PHI',
    21: 'PHX',
    22: 'POR',
    23: 'SAC',
    24: 'SAS',
    25: 'OKC',
    26: 'UTA',
    27: 'WAS',
    28: 'TOR',
    29: 'MEM',
    30: 'CHA',
}

STATS_MAP = {
    '0': 'PTS',
    '1': 'BLK',
    '2': 'STL',
    '3': 'AST',
    '4': 'OREB',
    '5': 'DREB',
    '6': 'REB',
    '7': '7',
    '8': '8',
    '9': 'PF',
    '10': '10',
    '11': 'TO',
    '12': '12',
    '13': 'FGM',
    '14': 'FGA',
    '15': 'FTM',
    '16': 'FTA',
    '17': '3PTM',
    '18': '3PTA',
    '19': 'FG%',
    '20': 'FT%',
    '21': '3PT%',
    '22': '22',
    '23': '23',
    '24': '24',
    '25': '25',
    '26': '26',
    '27': '27',
    '28': 'MPG',
    '29': '29',
    '30': '30',
    '31': '31',
    '32': '32',
    '33': '33',
    '34': '34',
    '35': '35',
    '36': '36',
    '37': '37',
    '38': '38',
    '39': '39',
    '40': 'MIN',
    '41': 'GS',
    '42': 'GP',
    '43': '43',
    '44': '44',
    '45': '45',
    }

STAT_ID_MAP = {
    '00': 'total',
    '10': 'projected',
    '01': 'last_7',
    '02': 'last_15',
    '03': 'last_30'
}

ACTIVITY_MAP = {
    178: 'FA ADDED',
    180: 'WAIVER ADDED',
    179: 'DROPPED',
    181: 'DROPPED',
    239: 'DROPPED',
    244: 'TRADED',
    'FA': 178,
    'WAIVER': 180,
    'TRADED': 244
}

NINE_CAT_STATS = {
    '3PTM',
    'AST',
    'BLK',
    'FG%',
    'FT%',
    'PTS',
    'REB',
    'STL',
    'TO'
}

class Player(object):
    '''Player are part of team'''
    def __init__(self, data, year, pro_team_schedule = None):
        self.name = json_parsing(data, 'fullName')
        self.playerId = json_parsing(data, 'id')
        self.year = year
        self.position = POSITION_MAP[json_parsing(data, 'defaultPositionId') - 1]
        self.lineupSlot = POSITION_MAP.get(data.get('lineupSlotId'), '')
        self.eligibleSlots = [POSITION_MAP[pos] for pos in json_parsing(data, 'eligibleSlots')]
        self.acquisitionType = json_parsing(data, 'acquisitionType')
        self.proTeam = PRO_TEAM_MAP[json_parsing(data, 'proTeamId')]
        self.injuryStatus = json_parsing(data, 'injuryStatus')
        self.posRank = json_parsing(data, 'positionalRanking')
        self.stats = {}
        self.schedule = {}

        if pro_team_schedule:
            pro_team_id = json_parsing(data, 'proTeamId')
            pro_team = pro_team_schedule.get(pro_team_id, {})
            for key in pro_team:
                game = pro_team[key][0]
                team = game['awayProTeamId'] if game['awayProTeamId'] != pro_team_id else game['homeProTeamId']
                self.schedule[key] = { 'team': PRO_TEAM_MAP[team], 'date': datetime.fromtimestamp(game['date']/1000.0) }



        # add available stats

        player = data['playerPoolEntry']['player'] if 'playerPoolEntry' in data else data['player']
        self.injuryStatus = player.get('injuryStatus', self.injuryStatus)
        self.injured = player.get('injured', False)

        for split in player.get('stats', []):
            if split['seasonId'] == year:
                id = self._stat_id_pretty(split['id'], split['scoringPeriodId'])
                applied_total = split.get('appliedTotal', 0)
                applied_avg =  round(split.get('appliedAverage', 0), 2)
                game = self.schedule.get(id, {})
                self.stats[id] = dict(applied_total=applied_total, applied_avg=applied_avg, team=game.get('team', None), date=game.get('date', None))
                if 'stats' in split and split['stats']:
                    if 'averageStats' in split.keys():
                        self.stats[id]['avg'] = {STATS_MAP.get(i, i): split['averageStats'][i] for i in split['averageStats'].keys() if STATS_MAP.get(i) != ''}
                        self.stats[id]['total'] = {STATS_MAP.get(i, i): split['stats'][i] for i in split['stats'].keys() if STATS_MAP.get(i) != ''}
                    else:
                        self.stats[id]['avg'] = None
                        self.stats[id]['total'] = {STATS_MAP.get(i, i): split['stats'][i] for i in split['stats'].keys() if STATS_MAP.get(i) != ''}
        self.total_points = self.stats.get(f'{year}_total', {}).get('applied_total', 0)
        self.avg_points = self.stats.get(f'{year}_total', {}).get('applied_avg', 0)
        self.projected_total_points= self.stats.get(f'{year}_projected', {}).get('applied_total', 0)
        self.projected_avg_points = self.stats.get(f'{year}_projected', {}).get('applied_avg', 0)

    def __repr__(self):
        return f'Player({self.name})'

    def _stat_id_pretty(self, id: str, scoring_period):
        id_type = STAT_ID_MAP.get(id[:2])
        return f'{id[2:]}_{id_type}' if id_type else str(scoring_period)

    @cached_property
    def nine_cat_averages(self):
        return {
            k: round(v, (3 if k in {'FG%', 'FT%'} else 1))
            for k, v in self.stats.get(f'{self.year}_total', {}).get("avg", {}).items()
            if k in NINE_CAT_STATS
        }
    
def json_parsing(obj, key):
    """Recursively pull values of specified key from nested JSON."""
    arr = []

    def extract(obj, arr, key):
        """Return all matching values in an object."""
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, (dict)) or (isinstance(v, (list)) and  v and isinstance(v[0], (list, dict))):
                    extract(v, arr, key)
                elif k == key:
                    arr.append(v)
        elif isinstance(obj, list):
            for item in obj:
                extract(item, arr, key)
        return arr

    results = extract(obj, arr, key)
    return results[0] if results else results


def check_league(req: LeagueInfo):
    params = {
        'view': ['mTeam', 'mRoster', 'mMatchup', 'mSettings', 'mStandings']
    }


    # Clean the input just in case it is what is making mobile requests fail
    req.year = int(req.year)
    req.league_id = int(req.league_id)
    req.team_name = req.team_name.strip(" \t\n\r")
    req.espn_s2 = req.espn_s2.strip(" \t\n\r")
    req.swid = req.swid.strip(" \t\n\r")
    
    print(req.year, req.league_id, req.team_name, req.espn_s2, req.swid)
    print(len(req.espn_s2), len(req.swid))

    endpoint = ESPN_FANTASY_ENDPOINT.format(req.year, req.league_id)
    print(endpoint)

    try:
        response = requests.get(endpoint, params=params, cookies={'espn_s2': req.espn_s2, 'SWID': req.swid})
        response.raise_for_status()
        data = response.json()
        teams = [team['name'] for team in data['teams']]
        return ValidateLeagueResp(valid=True, message="Team found") if req.team_name in teams else ValidateLeagueResp(valid=False, message="Team not found in valid league")
    except requests.exceptions.HTTPError as e:
        return ValidateLeagueResp(valid=False, message=f"Invalid league information {e}")
    
def get_roster(team_name, teams):
        
        for team in teams:
            if team_name.strip() == team['name']:
                return team['roster']['entries']
            
# ------------------------------------------------ ETL Helpers ------------------------------------------------


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


# Fetches and restructures the data from the NBA API
def fetch_nba_fpts_data(rostered_data: dict) -> dict:
	leaders = leagueleaders.LeagueLeaders(
		season='2024-25',
		per_mode48='Totals',
		stat_category_abbreviation='PTS'
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
		'rost_pct': rostered_data.get(player['PLAYER'], 0)
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
				 new['rost_pct']
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
					new['rost_pct']
	)


# Creates the formatted entries for insertion into daily_stats
def create_daily_entries(had_game: list[dict], old_dict: dict, date: datetime, rostered_data: dict) -> list[tuple]:
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
			d['ftm'], d['fta'], d['min'], d['gp'], d['rost_pct']
		)
		for id, d in updated_dict.items()
	]


def serialize_fpts_data(data: list[tuple]) -> str:
	return [FPTSPlayer(
		rank=player[0],
		player_id=player[1],
		player_name=player[2],
		total_fpts=player[3],
		avg_fpts=player[4],
		rank_change=player[5]
	) for player in data]

def fetch_espn_rostered_data(league_id: int, year: int, for_stats: bool = False) -> dict:
    params = {
        'view': 'kona_player_info',
        'scoringPeriodId': 0,
    }
    endpoint = ESPN_FANTASY_ENDPOINT.format(year, league_id)
    filters = {"players":{"filterSlotIds":{"value":[]},"limit": 750, "sortPercOwned":{"sortPriority":1,"sortAsc":False},"sortDraftRanks":{"sortPriority":2,"sortAsc":True,"value":"STANDARD"}}}
    headers = {'x-fantasy-filter': json.dumps(filters)}

    data = requests.get(endpoint, params=params, headers=headers).json()
    data = data['players']
    data = [x.get('player', x) for x in data]

    cleaned_data = []

    # When inserting into the freeagents, we only want players with ownership between 0.33 and 85
    if not for_stats:
        for player in data:
            if player:
                if 0.33 <= player["ownership"]["percentOwned"] <= 85:
                    cleaned_data.append({
                        'espnId': player['id'],
                        'fullName': player['fullName'],
                        'team': PRO_TEAM_MAP[player['proTeamId']],
                        'rosteredPct': player['ownership']['percentOwned'],
                    })
    # When getting the data to incorporate into the daily stats, we want all players and mapping from full name to data
    else:
        cleaned_data = {player['fullName']: player['ownership']['percentOwned'] for player in data if player}

    return cleaned_data

# Create the entries for the database
def create_rostered_entries(data: list[dict]) -> None:
    central_tz = pytz.timezone('US/Central')
    today = datetime.now(central_tz)
    date_str = today.strftime("%Y-%m-%d")
    date = datetime.strptime(date_str, "%Y-%m-%d")

    return [(player['espnId'], player['fullName'], player['team'], date, player['rosteredPct']) for player in data]