from datetime import datetime
from functools import cached_property

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

        for split in  player.get('stats', []):
            if split['seasonId'] == year:
                id = self._stat_id_pretty(split['id'], split['scoringPeriodId'])
                applied_total = split.get('appliedTotal', 0)
                applied_avg =  round(split.get('appliedAverage', 0), 2)
                game = self.schedule.get(id, {})
                self.stats[id] = dict(applied_total=applied_total, applied_avg=applied_avg, team=game.get('team', None), date=game.get('date', None))
                if split['stats']:
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