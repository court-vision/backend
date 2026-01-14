from datetime import datetime
from typing import Optional
import requests
import json
from schemas.espn import ValidateLeagueResp, PlayerResp, LeagueInfo, TeamDataResp
from schemas.matchup import MatchupResp, MatchupData, MatchupTeamResp, MatchupPlayerResp
from utils.constants import ESPN_FANTASY_ENDPOINT
from utils.espn_helpers import POSITION_MAP, PRO_TEAM_MAP, STATS_MAP, STAT_ID_MAP, AVG_WINDOW_MAP, json_parsing
from schemas.common import ApiStatus
from services.schedule_service import get_remaining_games

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

        endpoint = ESPN_FANTASY_ENDPOINT.format(league_info.year, league_info.league_id)

        try:
            response = requests.get(endpoint, params=params, cookies={'espn_s2': league_info.espn_s2, 'SWID': league_info.swid})
            response.raise_for_status()
            data = response.json()
            teams = [team['name'] for team in data['teams']]
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
                    PlayerResp(
                        player_id=player.playerId,
                        name=player.name,
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
                        player_id=player.playerId,
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

    @staticmethod
    async def get_matchup_data(league_info: LeagueInfo, avg_window: str = "season") -> MatchupResp:
        """
        Fetches current matchup data from ESPN API.

        Args:
            league_info: League credentials and team info
            avg_window: Averaging window for projections (season, last_7, last_14, last_30)

        Returns:
            MatchupResp with current matchup data including both teams and projections
        """
        try:
            params = {
                'view': ['mTeam', 'mRoster', 'mMatchup', 'mSettings', 'mSchedule']
            }

            cookies = {
                'espn_s2': league_info.espn_s2,
                'SWID': league_info.swid
            }

            endpoint = ESPN_FANTASY_ENDPOINT.format(league_info.year, league_info.league_id)
            response = requests.get(endpoint, params=params, cookies=cookies)
            response.raise_for_status()
            data = response.json()

            # Get current matchup period and scoring period
            status = data.get('status', {})
            current_matchup_period = status.get('currentMatchupPeriod', 1)

            # Get schedule settings for matchup period dates
            settings = data.get('settings', {})
            schedule_settings = settings.get('scheduleSettings', {})
            matchup_periods = schedule_settings.get('matchupPeriods', {})

            # Find our team
            teams = data.get('teams', [])
            our_team = None
            our_team_id = None
            for team in teams:
                if team.get('name', '').strip() == league_info.team_name.strip():
                    our_team = team
                    our_team_id = team.get('id')
                    break

            if not our_team:
                return MatchupResp(
                    status=ApiStatus.NOT_FOUND,
                    message=f"Team '{league_info.team_name}' not found in league",
                    data=None
                )

            # Find current matchup from schedule
            schedule = data.get('schedule', [])
            current_matchup = None
            opponent_team_id = None

            for matchup in schedule:
                if matchup.get('matchupPeriodId') == current_matchup_period:
                    home_team_id = matchup.get('home', {}).get('teamId')
                    away_team_id = matchup.get('away', {}).get('teamId')

                    if home_team_id == our_team_id:
                        current_matchup = matchup
                        opponent_team_id = away_team_id
                        break
                    elif away_team_id == our_team_id:
                        current_matchup = matchup
                        opponent_team_id = home_team_id
                        break

            if not current_matchup:
                return MatchupResp(
                    status=ApiStatus.NOT_FOUND,
                    message="No current matchup found (possibly bye week)",
                    data=None
                )

            # Find opponent team
            opponent_team = None
            for team in teams:
                if team.get('id') == opponent_team_id:
                    opponent_team = team
                    break

            if not opponent_team:
                return MatchupResp(
                    status=ApiStatus.NOT_FOUND,
                    message="Opponent team not found",
                    data=None
                )

            # Get pro team schedule for games remaining calculation
            pro_team_schedule = data.get('settings', {}).get('proTeams', [])
            pro_schedule_map = {}
            for pro_team in pro_team_schedule:
                pro_team_id = pro_team.get('id')
                pro_schedule_map[pro_team_id] = pro_team.get('proGamesByScoringPeriod', {})

            # Get matchup period dates
            matchup_period_scoring_periods = matchup_periods.get(str(current_matchup_period), [])
            matchup_start = None
            matchup_end = None
            if matchup_period_scoring_periods:
                matchup_start = min(matchup_period_scoring_periods)
                matchup_end = max(matchup_period_scoring_periods)

            # Get current scoring period to calculate remaining games
            current_scoring_period = status.get('currentScoringPeriod', matchup_start or 1)

            # Calculate remaining scoring periods in this matchup
            remaining_periods = []
            if matchup_period_scoring_periods:
                remaining_periods = [p for p in matchup_period_scoring_periods if p >= current_scoring_period]

            # Extract current scores
            home_data = current_matchup.get('home', {})
            away_data = current_matchup.get('away', {})

            is_home = home_data.get('teamId') == our_team_id
            our_score = home_data.get('totalPoints', 0) if is_home else away_data.get('totalPoints', 0)
            opponent_score = away_data.get('totalPoints', 0) if is_home else home_data.get('totalPoints', 0)

            # Build roster responses
            stat_key = f"{league_info.year}_{AVG_WINDOW_MAP.get(avg_window, 'total')}"
            projected_key = f"{league_info.year}_projected"

            def build_roster(team_data: dict) -> tuple[list[MatchupPlayerResp], float]:
                """Build roster list and calculate projected score."""
                roster_entries = team_data.get('roster', {}).get('entries', [])
                players = []
                projected_total = 0.0

                team_abbrev_corrections = {"PHL": "PHI", "PHO": "PHX"}

                for entry in roster_entries:
                    player_data = entry.get('playerPoolEntry', {}).get('player', {})
                    if not player_data:
                        player_data = entry.get('player', {})

                    player_id = player_data.get('id', 0)
                    name = player_data.get('fullName', 'Unknown')
                    pro_team_id = player_data.get('proTeamId', 0)
                    team_abbrev = PRO_TEAM_MAP.get(pro_team_id, 'FA')
                    team_abbrev = team_abbrev_corrections.get(team_abbrev, team_abbrev)

                    position = POSITION_MAP.get(player_data.get('defaultPositionId', 0) - 1, '')
                    lineup_slot_id = entry.get('lineupSlotId', 0)
                    lineup_slot = POSITION_MAP.get(lineup_slot_id, '')

                    injured = player_data.get('injured', False)
                    injury_status = player_data.get('injuryStatus')

                    # Get stats for the selected window
                    stats = player_data.get('stats', [])
                    avg_points = 0.0
                    projected_points = 0.0

                    for stat_split in stats:
                        if stat_split.get('seasonId') == league_info.year:
                            stat_id = str(stat_split.get('id', ''))
                            applied_avg = stat_split.get('appliedAverage', 0)

                            # Match stat window
                            if stat_id.startswith('00'):  # Season total
                                if avg_window == "season":
                                    avg_points = round(applied_avg, 2)
                            elif stat_id.startswith('01'):  # Last 7
                                if avg_window == "last_7":
                                    avg_points = round(applied_avg, 2)
                            elif stat_id.startswith('02'):  # Last 15 (we call it last_14)
                                if avg_window == "last_14":
                                    avg_points = round(applied_avg, 2)
                            elif stat_id.startswith('03'):  # Last 30
                                if avg_window == "last_30":
                                    avg_points = round(applied_avg, 2)
                            elif stat_id.startswith('10'):  # Projected
                                projected_points = round(stat_split.get('appliedTotal', 0), 2)
                    

                    # Calculate games remaining for this player using schedule service
                    games_remaining = get_remaining_games(team_abbrev)
                            
                    print(f"Player: {name}, Avg Points: {avg_points}, Projected Points: {projected_points}, Games Remaining: {games_remaining}")

                    # Only add to projection if not on IR
                    if lineup_slot not in ('IR', ''):
                        projected_total += avg_points * games_remaining

                    players.append(MatchupPlayerResp(
                        player_id=player_id,
                        name=name,
                        team=team_abbrev,
                        position=position,
                        lineup_slot=lineup_slot,
                        avg_points=avg_points,
                        projected_points=projected_points,
                        games_remaining=games_remaining,
                        injured=injured,
                        injury_status=injury_status
                    ))

                return players, projected_total

            our_roster, our_projected = build_roster(our_team)
            opponent_roster, opponent_projected = build_roster(opponent_team)

            # Add current scores to projections
            our_final_projection = our_score + our_projected
            opponent_final_projection = opponent_score + opponent_projected

            # Determine winner
            if our_final_projection > opponent_final_projection:
                projected_winner = our_team.get('name', 'Your Team')
            elif opponent_final_projection > our_final_projection:
                projected_winner = opponent_team.get('name', 'Opponent')
            else:
                projected_winner = "Tie"

            projected_margin = round(abs(our_final_projection - opponent_final_projection), 2)

            # Build response
            matchup_data = MatchupData(
                matchup_period=current_matchup_period,
                matchup_period_start=str(matchup_start) if matchup_start else "",
                matchup_period_end=str(matchup_end) if matchup_end else "",
                your_team=MatchupTeamResp(
                    team_name=our_team.get('name', 'Your Team'),
                    team_id=our_team_id,
                    current_score=round(our_score, 2),
                    projected_score=round(our_final_projection, 2),
                    roster=our_roster
                ),
                opponent_team=MatchupTeamResp(
                    team_name=opponent_team.get('name', 'Opponent'),
                    team_id=opponent_team_id,
                    current_score=round(opponent_score, 2),
                    projected_score=round(opponent_final_projection, 2),
                    roster=opponent_roster
                ),
                projected_winner=projected_winner,
                projected_margin=projected_margin
            )

            return MatchupResp(
                status=ApiStatus.SUCCESS,
                message="Matchup data fetched successfully",
                data=matchup_data
            )

        except requests.exceptions.HTTPError as e:
            return MatchupResp(
                status=ApiStatus.ERROR,
                message=f"ESPN API error: {str(e)}",
                data=None
            )
        except Exception as e:
            print(f"Error in get_matchup_data: {e}")
            return MatchupResp(
                status=ApiStatus.ERROR,
                message=f"Internal server error: {str(e)}",
                data=None
            )
