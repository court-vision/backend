from datetime import datetime
import requests
import json
from schemas.espn import ValidateLeagueResp, PlayerResp, LeagueInfo, TeamDataResp
from utils.constants import ESPN_FANTASY_ENDPOINT
from utils.espn_helpers import POSITION_MAP, PRO_TEAM_MAP, STATS_MAP, STAT_ID_MAP, json_parsing
from schemas.common import ApiStatus

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
        self.news = {}
        expected_return_date = json_parsing(data, 'expectedReturnDate')
        self.expected_return_date = datetime(*expected_return_date).date() if expected_return_date else None

        if pro_team_schedule:
            pro_team_id = json_parsing(data, 'proTeamId')
            pro_team = pro_team_schedule.get(pro_team_id, {})
            for key in pro_team:
                game = pro_team[key][0]
                team = game['awayProTeamId'] if game['awayProTeamId'] != pro_team_id else game['homeProTeamId']
                self.schedule[key] = { 'team': PRO_TEAM_MAP[team], 'date': datetime.fromtimestamp(game['date']/1000.0) }

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
                if split.get('stats'):
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

class EspnService:
    
    @staticmethod
    def check_league(league_info: LeagueInfo) -> ValidateLeagueResp:
        params = {
            'view': ['mTeam', 'mRoster', 'mMatchup', 'mSettings', 'mStandings']
        }

        # Clean the input just in case it is what is making mobile requests fail
        league_info.year = int(league_info.year)
        league_info.league_id = int(league_info.league_id)
        league_info.team_name = league_info.team_name.strip(" \t\n\r")
        league_info.espn_s2 = league_info.espn_s2.strip(" \t\n\r")
        league_info.swid = league_info.swid.strip(" \t\n\r")
        # print(league_info.year, league_info.league_id, league_info.team_name, league_info.espn_s2, league_info.swid)
        # print(len(league_info.espn_s2), len(league_info.swid))

        endpoint = ESPN_FANTASY_ENDPOINT.format(league_info.year, league_info.league_id)
        # print(endpoint)

        try:
            response = requests.get(endpoint, params=params, cookies={'espn_s2': league_info.espn_s2, 'SWID': league_info.swid})
            response.raise_for_status()
            data = response.json()
            teams = [team['name'] for team in data['teams']]
            # print(teams)
            return ValidateLeagueResp(status=ApiStatus.SUCCESS, valid=True, message="Team found") if league_info.team_name in teams else ValidateLeagueResp(status=ApiStatus.SUCCESS, valid=False, message="Team not found in valid league")
        except requests.exceptions.HTTPError as e:
            return ValidateLeagueResp(status=ApiStatus.ERROR, valid=False, message=f"Invalid league information {e}")

    @staticmethod
    def get_roster(team_name, teams):
        for team in teams:
            if team_name.strip() == team['name']:
                return team['roster']['entries']

    @staticmethod
    async def get_team_data(league_info: LeagueInfo, fa_count: int = 0) -> TeamDataResp:
        try:
            params = {
                    'view': ['mTeam', 'mRoster', 'mMatchup', 'mSettings', 'mStandings']
                }
            
            cookies = {
                'espn_s2': league_info.espn_s2,
                'SWID': league_info.swid
            }
            
            endpoint = ESPN_FANTASY_ENDPOINT.format(league_info.year, league_info.league_id)
            data = requests.get(endpoint, params=params, cookies=cookies).json()
            roster = EspnService.get_roster(league_info.team_name, data['teams'])
            players = [Player(player, league_info.year) for player in roster]

            team_abbrev_corrections = {"PHL": "PHI", "PHO": "PHX"}
            pos_to_keep = {"PG", "SG", "SF", "PF", "C", "G", "F"}

            return TeamDataResp(
                status=ApiStatus.SUCCESS, 
                message="Team data fetched successfully",
                data=[
                    PlayerResp(name=player.name,
                        avg_points=player.avg_points,
                        team=team_abbrev_corrections.get(player.proTeam, player.proTeam),
                        valid_positions=[pos for pos in player.eligibleSlots if pos in pos_to_keep] + ["UT1", "UT2", "UT3"],
                        injured=player.injured,
                    ) for player in players
                ]
            )
        except Exception as e:
            print(f"Error in get_team_data: {e}")
            return TeamDataResp(status=ApiStatus.ERROR, message="Internal server error", data=None)

    @staticmethod
    async def get_free_agents(league_info: LeagueInfo, fa_count: int) -> TeamDataResp:
        try:
            params = {
                'view': 'kona_player_info',
                'scoringPeriodId': 0,
            }

            filters = {"players":{"filterStatus":{"value":["FREEAGENT","WAIVERS"]},"filterSlotIds":{"value":[]},"limit":fa_count,"sortPercOwned":{"sortPriority":1,"sortAsc":False},"sortDraftRanks":{"sortPriority":100,"sortAsc":True,"value":"STANDARD"}}}
            headers = {'x-fantasy-filter': json.dumps(filters)}

            cookies = {
                'espn_s2': league_info.espn_s2,
                'SWID': league_info.swid
            }

            endpoint = ESPN_FANTASY_ENDPOINT.format(league_info.year, league_info.league_id)
            data = requests.get(endpoint, params=params, headers=headers, cookies=cookies).json()
            players = [Player(player, league_info.year) for player in data['players']]

            team_abbrev_corrections = {"PHL": "PHI", "PHO": "PHX"}
            pos_to_keep = {"PG", "SG", "SF", "PF", "C", "G", "F"}

            return TeamDataResp(
                status=ApiStatus.SUCCESS,
                message="Free agents fetched successfully",
                data=[PlayerResp(
                        name=player.name,
                        avg_points=player.avg_points,
                        team=team_abbrev_corrections.get(player.proTeam, player.proTeam),
                        valid_positions=[pos for pos in player.eligibleSlots if pos in pos_to_keep] + ["UT1", "UT2", "UT3"],
                        injured=player.injured,
                    ) for player in players
                ]
            )
        except Exception as e:
            print(f"Error in get_free_agents: {e}")
            return TeamDataResp(status=ApiStatus.ERROR, message="Internal server error", data=None)

    @staticmethod
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
