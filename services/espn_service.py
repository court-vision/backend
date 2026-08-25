from datetime import datetime
import requests
from core.logging import get_logger
from core.settings import settings
from services.player_value_service import PlayerValueService, ValueResult
from services.scoring.models import CategoryTeamScoreData, StatLine
from services.scoring.providers.espn_settings import parse_espn_category_score, statline_from_espn_stats
from services.scoring.resolver import ResolvedScoring, resolve_scoring
from schemas.matchup import CategoryTeamScore, CategoryComparison, CategoryScoreItem
import json
from schemas.espn import ValidateLeagueResp, PlayerResp, LeagueInfo, TeamDataResp
from schemas.matchup import MatchupResp, MatchupData, MatchupTeamResp, MatchupPlayerResp
from utils.constants import ESPN_FANTASY_ENDPOINT
from utils.espn_helpers import TEAM_ABBREV_CORRECTIONS, POSITION_MAP, PRO_TEAM_MAP, STATS_MAP, STAT_ID_MAP, AVG_WINDOW_MAP, json_parsing
from schemas.common import ApiStatus
from services.schedule_service import (
    espn_scoring_periods,
    get_current_matchup,
    get_espn_matchup_dates,
    get_remaining_games,
)

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
                        self.stats[id]['avg_raw'] = split['averageStats']   # stat-id keyed, for scoring fallbacks
                        self.stats[id]['avg'] = {STATS_MAP.get(i, i): split['averageStats'][i] for i in split['averageStats'].keys() if STATS_MAP.get(i) != ''}
                        self.stats[id]['total'] = {STATS_MAP.get(i, i): split['stats'][i] for i in split['stats'].keys() if STATS_MAP.get(i) != ''}
                    else:
                        self.stats[id]['avg'] = None
                        self.stats[id]['total'] = {STATS_MAP.get(i, i): split['stats'][i] for i in split['stats'].keys() if STATS_MAP.get(i) != ''}
        self.total_points = self.stats.get(f'{year}_total', {}).get('applied_total', 0)
        self.avg_points = self.stats.get(f'{year}_total', {}).get('applied_avg', 0)
        if not self.avg_points and self.stats.get(f'{year}_total', {}).get('avg_raw'):
            # Category leagues: ESPN's appliedAverage is 0, so value the player with the default
            # points formula over their raw per-game averages. This is only the last-resort
            # fallback: EspnService._to_player_resps overwrites avg_points from our stored stats.
            from services.scoring.points import DEFAULT_POINTS
            from services.scoring.providers.espn_settings import statline_from_espn_stats
            self.avg_points = round(DEFAULT_POINTS.score(statline_from_espn_stats(self.stats[f'{year}_total']['avg_raw'])), 2)
        self.projected_total_points= self.stats.get(f'{year}_projected', {}).get('applied_total', 0)
        self.projected_avg_points = self.stats.get(f'{year}_projected', {}).get('applied_avg', 0)

    def __repr__(self):
        return f'Player({self.name})'

    def _stat_id_pretty(self, id: str, scoring_period):
        id_type = STAT_ID_MAP.get(id[:2])
        return f'{id[2:]}_{id_type}' if id_type else str(scoring_period)

def espn_scoring_periods_for(matchup_period_map: dict, current_matchup_period: int) -> list[int]:
    """ESPN weekly scoring-period ids covered by a matchup period.

    `scheduleSettings.matchupPeriods` maps matchupPeriodId -> [weekly period ids]
    (e.g. a two-week playoff round "20": [20, 21]). Falls back to the period id
    itself when the key is absent.
    """
    return espn_scoring_periods(matchup_period_map, current_matchup_period)


class EspnService:
    
    @staticmethod
    def check_league(league_info: LeagueInfo) -> ValidateLeagueResp:
        # Team names + league settings are all validation (and league sync) need.
        params = {
            'view': ['mTeam', 'mSettings']
        }

        # Clean the input just in case it is what is making mobile requests fail
        league_info.year = int(league_info.year)
        league_info.league_id = int(league_info.league_id)
        league_info.team_name = league_info.team_name.strip(" \t\n\r")
        league_info.espn_s2 = league_info.espn_s2.strip(" \t\n\r")
        league_info.swid = league_info.swid.strip(" \t\n\r")

        endpoint = ESPN_FANTASY_ENDPOINT.format(league_info.year, league_info.league_id)

        try:
            response = requests.get(endpoint, params=params, cookies={'espn_s2': league_info.espn_s2, 'SWID': league_info.swid}, timeout=settings.http_timeout)
            response.raise_for_status()
            data = response.json()
            teams = [team['name'] for team in data['teams']]
            if league_info.team_name in teams:
                # league_payload is excluded from serialization; LeagueService reuses it for settings sync
                return ValidateLeagueResp(status=ApiStatus.SUCCESS, valid=True, message="Team found", league_payload=data)
            return ValidateLeagueResp(status=ApiStatus.SUCCESS, valid=False, message="Team not found in valid league")
        except requests.exceptions.HTTPError as e:
            return ValidateLeagueResp(status=ApiStatus.ERROR, valid=False, message=f"Invalid league information {e}")

    @staticmethod
    def get_roster(team_name, teams):
        for team in teams:
            if team_name.strip() == team['name']:
                return team['roster']['entries']

    @staticmethod
    def _scoring_for(league_info: LeagueInfo) -> ResolvedScoring:
        """The league's resolved scoring (honouring the team's scoring_preview); the
        default points scoring if the lookup fails, so ESPN data is still served."""
        try:
            return PlayerValueService.scoring_for(league_info)
        except Exception as exc:
            get_logger().warning("espn_scoring_resolve_failed", league_id=league_info.league_id, error=str(exc))
            return resolve_scoring(None, getattr(league_info, "scoring_preview", None))

    @staticmethod
    def _values_for(scoring: ResolvedScoring, espn_ids: list[int], **kwargs) -> dict[int, ValueResult]:
        """avg_points from our stored stats for a set of ESPN ids; {} if the lookup fails."""
        try:
            return PlayerValueService.avg_points_for(scoring, espn_ids=espn_ids, **kwargs)
        except Exception as exc:
            get_logger().warning("espn_avg_points_lookup_failed", error=str(exc))
            return {}

    @staticmethod
    def _to_player_resps(players: list["Player"], league_info: LeagueInfo) -> list[PlayerResp]:
        """PlayerResps with `avg_points` computed the same way for every provider:
        fantasy points under the league's weights, or the category value proxy for
        H2H-category leagues, from our stored stats (rolling window, then last
        season's baseline). ESPN's own number -- appliedAverage, or the default
        formula over its raw averages -- is only the last resort for a player we
        know nothing about.
        """
        scoring = EspnService._scoring_for(league_info)
        value_kind = PlayerValueService.value_kind_for(scoring)
        values = EspnService._values_for(scoring, [p.playerId for p in players])

        team_abbrev_corrections = TEAM_ABBREV_CORRECTIONS
        pos_to_keep = {"PG", "SG", "SF", "PF", "C", "G", "F"}
        out: list[PlayerResp] = []
        for player in players:
            valued = values.get(player.playerId)
            if valued is not None and valued.value is not None:
                avg_points, source = valued.value, valued.source
            else:
                avg_points, source = player.avg_points, "provider"
            out.append(PlayerResp(
                player_id=player.playerId,
                name=player.name,
                avg_points=avg_points,
                team=team_abbrev_corrections.get(player.proTeam, player.proTeam),
                valid_positions=[pos for pos in player.eligibleSlots if pos in pos_to_keep] + ["UT1", "UT2", "UT3"],
                injured=player.injured,
                injury_status=player.injuryStatus if player.injuryStatus and player.injuryStatus != "ACTIVE" else None,
                value_kind=value_kind,
                value_source=source,
            ))
        return out

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
            data = requests.get(endpoint, params=params, cookies=cookies, timeout=settings.http_timeout).json()
            roster = EspnService.get_roster(league_info.team_name, data['teams'])
            players = [Player(player, league_info.year) for player in roster]

            return TeamDataResp(
                status=ApiStatus.SUCCESS,
                message="Team data fetched successfully",
                data=EspnService._to_player_resps(players, league_info),
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
            data = requests.get(endpoint, params=params, headers=headers, cookies=cookies, timeout=settings.http_timeout).json()
            players = [Player(player, league_info.year) for player in data['players']]

            return TeamDataResp(
                status=ApiStatus.SUCCESS,
                message="Free agents fetched successfully",
                data=EspnService._to_player_resps(players, league_info),
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

        data = requests.get(endpoint, params=params, headers=headers, timeout=settings.http_timeout).json()
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
    def _comparison_schema(cmp) -> CategoryComparison:
        return CategoryComparison(
            items=[CategoryScoreItem(key=i.key, label=i.label, you=i.you, opp=i.opp, winner=i.winner,
                                     higher_is_better=i.higher_is_better, is_rate=i.is_rate) for i in cmp.items],
            wins=cmp.wins, losses=cmp.losses, ties=cmp.ties,
        )

    @staticmethod
    async def get_matchup_data(
        league_info: LeagueInfo,
        avg_window: str = "season",
        scoring: ResolvedScoring | None = None,
    ) -> MatchupResp:
        """
        Fetches current matchup data from ESPN API.

        Args:
            league_info: League credentials and team info
            avg_window: Averaging window for projections (season, last_7, last_14, last_30)

        Returns:
            MatchupResp with current matchup data including both teams and projections
        """
        scoring = scoring or resolve_scoring(None)
        try:
            params = {
                'view': ['mTeam', 'mRoster', 'mMatchup', 'mSettings', 'mSchedule']
            }

            cookies = {
                'espn_s2': league_info.espn_s2,
                'SWID': league_info.swid
            }

            endpoint = ESPN_FANTASY_ENDPOINT.format(league_info.year, league_info.league_id)
            response = requests.get(endpoint, params=params, cookies=cookies, timeout=settings.http_timeout)
            response.raise_for_status()
            data = response.json()

            # Get current matchup period from ESPN
            status = data.get('status', {})
            current_matchup_period = status.get('currentMatchupPeriod', 1)
            latest_scoring_period = status.get('latestScoringPeriod')

            # Resolve matchup period dates via ESPN's scoring period map (handles playoffs).
            # During playoffs, one matchup period spans multiple scoring periods (e.g., [21, 22]).
            league_settings = data.get('settings', {})
            matchup_period_map = league_settings.get('scheduleSettings', {}).get('matchupPeriods', {})
            # matchupPeriods values are WEEK ids; status.latestScoringPeriod is a
            # DAY index. The resolver indexes the weekly ids into our calendar and
            # falls back to the calendar week containing the day if they disagree.
            matchup_dates = get_espn_matchup_dates(
                matchup_period_map, current_matchup_period, latest_scoring_period
            )
            matchup_start_date = matchup_dates[0] if matchup_dates else None
            matchup_end_date = matchup_dates[1] if matchup_dates else None

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

            # Extract current scores
            home_data = current_matchup.get('home', {})
            away_data = current_matchup.get('away', {})

            is_home = home_data.get('teamId') == our_team_id
            our_score = home_data.get('totalPoints', 0) if is_home else away_data.get('totalPoints', 0)
            opponent_score = away_data.get('totalPoints', 0) if is_home else home_data.get('totalPoints', 0)

            your_cat = opp_cat = None
            if scoring.is_categories:
                your_cat = parse_espn_category_score(home_data if is_home else away_data) or CategoryTeamScoreData(totals={})
                opp_cat = parse_espn_category_score(away_data if is_home else home_data) or CategoryTeamScoreData(totals={})

            # Build roster responses
            stat_key = f"{league_info.year}_{AVG_WINDOW_MAP.get(avg_window, 'total')}"
            projected_key = f"{league_info.year}_projected"

            window_prefix = {'season': '00', 'last_7': '01', 'last_14': '02', 'last_30': '03'}.get(avg_window, '00')

            def _entry_player(entry: dict) -> dict:
                return entry.get('playerPoolEntry', {}).get('player', {}) or entry.get('player', {})

            # Our own per-player values for both rosters in one lookup: the category value
            # for category leagues (so matchup figures agree with the streamer finder and the
            # optimizer), and the rolling/baseline fpts that stands in when ESPN has no
            # average yet (opening week) for points leagues.
            our_values = EspnService._values_for(scoring, [
                _entry_player(entry).get('id', 0)
                for team_data in (our_team, opponent_team)
                for entry in team_data.get('roster', {}).get('entries', [])
            ])

            def build_roster(team_data: dict) -> tuple[list[MatchupPlayerResp], float, list]:
                """Build roster list and calculate projected score."""
                roster_entries = team_data.get('roster', {}).get('entries', [])
                players = []
                projected_total = 0.0
                proj_inputs = []

                team_abbrev_corrections = TEAM_ABBREV_CORRECTIONS

                for entry in roster_entries:
                    player_data = _entry_player(entry)

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

                    avg_line = StatLine()
                    for stat_split in stats:
                        if stat_split.get('seasonId') == league_info.year:
                            stat_id = str(stat_split.get('id', ''))
                            if stat_id.startswith(window_prefix):
                                avg_points = round(stat_split.get('appliedAverage', 0) or 0, 2)
                                avg_line = statline_from_espn_stats(stat_split.get('averageStats'))
                            elif stat_id.startswith('10'):  # Projected
                                projected_points = round(stat_split.get('appliedTotal', 0), 2)
                    ours = our_values.get(player_id)
                    if ours is not None and ours.value is not None and (scoring.is_categories or not avg_points):
                        avg_points = ours.value
                    elif not avg_points and any(getattr(avg_line, k) for k in StatLine.ROW_KEYS):
                        # Last resort (no stored stats for this player): value the player with the
                        # league's point weights (default formula) over ESPN's raw averages.
                        avg_points = round(scoring.points.score(avg_line), 2)
                    
                    # Calculate games remaining for this player using schedule service
                    games_remaining = get_remaining_games(team_abbrev)

                    # Only add to projection if not on IR and not injured
                    counts_toward_projection = lineup_slot not in ('IR', '') and not injured
                    if counts_toward_projection:
                        projected_total += avg_points * games_remaining
                    proj_inputs.append((avg_line, games_remaining, counts_toward_projection))

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

                return players, projected_total, proj_inputs

            our_roster, our_projected, our_inputs = build_roster(our_team)
            opponent_roster, opponent_projected, opp_inputs = build_roster(opponent_team)

            category_comparison = projected_category_comparison = None
            your_cat_schema = opp_cat_schema = None
            if scoring.is_categories and scoring.categories is not None:
                cats = scoring.categories
                current_cmp = cats.compare(your_cat.totals, opp_cat.totals)
                if (your_cat.wins + your_cat.losses + your_cat.ties) == 0:
                    your_cat.wins, your_cat.losses, your_cat.ties = current_cmp.wins, current_cmp.losses, current_cmp.ties
                    opp_cat.wins, opp_cat.losses, opp_cat.ties = current_cmp.losses, current_cmp.wins, current_cmp.ties
                proj_cmp = cats.compare(cats.project(your_cat, our_inputs), cats.project(opp_cat, opp_inputs))
                category_comparison = EspnService._comparison_schema(current_cmp)
                projected_category_comparison = EspnService._comparison_schema(proj_cmp)
                your_cat_schema = CategoryTeamScore(**your_cat.__dict__)
                opp_cat_schema = CategoryTeamScore(**opp_cat.__dict__)
                # Scalars keep the frontend contract: categories won now / projected
                our_score, opponent_score = float(your_cat.wins), float(your_cat.losses)
                our_final_projection, opponent_final_projection = float(proj_cmp.wins), float(proj_cmp.losses)
                projected_margin = float(proj_cmp.wins - proj_cmp.losses)
            else:
                # Add current scores to projections
                our_final_projection = our_score + our_projected
                opponent_final_projection = opponent_score + opponent_projected
                projected_margin = round(abs(our_final_projection - opponent_final_projection), 2)

            # Determine winner
            if our_final_projection > opponent_final_projection:
                projected_winner = our_team.get('name', 'Your Team')
            elif opponent_final_projection > our_final_projection:
                projected_winner = opponent_team.get('name', 'Opponent')
            else:
                projected_winner = "Tie"

            # Our calendar week containing the period start (what the optimizer indexes by)
            _week = get_current_matchup(matchup_start_date) if matchup_start_date else None
            schedule_week = _week["matchup_number"] if _week else None

            # Build response
            matchup_data = MatchupData(
                matchup_period=current_matchup_period,
                schedule_week=schedule_week,
                matchup_period_start=matchup_start_date.isoformat() if matchup_start_date else "",
                matchup_period_end=matchup_end_date.isoformat() if matchup_end_date else "",
                your_team=MatchupTeamResp(
                    team_name=our_team.get('name', 'Your Team'),
                    team_id=our_team_id,
                    current_score=round(our_score, 2),
                    projected_score=round(our_final_projection, 2),
                    roster=our_roster,
                    categories=your_cat_schema,
                ),
                opponent_team=MatchupTeamResp(
                    team_name=opponent_team.get('name', 'Opponent'),
                    team_id=opponent_team_id,
                    current_score=round(opponent_score, 2),
                    projected_score=round(opponent_final_projection, 2),
                    roster=opponent_roster,
                    categories=opp_cat_schema,
                ),
                projected_winner=projected_winner,
                projected_margin=projected_margin,
                scoring_period_id=latest_scoring_period,
                scoring_format=scoring.format,
                settings_synced=scoring.settings_synced,
                category_comparison=category_comparison,
                projected_category_comparison=projected_category_comparison,
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
