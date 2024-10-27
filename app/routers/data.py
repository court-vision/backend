from .data_helpers.models import LeagueInfo, TeamDataReq, PlayerResp, ValidateLeagueResp
from .data_helpers.utils import Player, check_league, get_roster
from .constants import ESPN_FANTASY_ENDPOINT
from fastapi import APIRouter
import requests
import json


router = APIRouter()

# Configure CORS
origins = [
    "http://localhost",
    "http://localhost:8000",
    "http://localhost:3000",  # Replace with your front-end URL
    "http://0.0.0.0:8000",
    "http://127.0.0.1:8000",
    "https://courtvisionaries.vercel.app",
    "https://www.courtvisionaries.live",
    "www.courtvisionaries.live",
    "courtvisionaries.live",
]

# router.add_middleware(
#     CORSMiddleware,
#     allow_origins=origins,
#     allow_credentials=True,
#     allow_methods=["POST", "GET", "OPTIONS", "DELETE"],
#     allow_headers=["Content-Type"],
# )

@router.get("/")
async def root():
    return {"message": "Hello World"}


# Checks if the league and team are valid
@router.post("/validate_league")
async def validate_league(req: LeagueInfo) -> ValidateLeagueResp:
    return check_league(req)



# Returns important data for players on a team
@router.post("/get_roster_data")
async def get_team_data(req: TeamDataReq):

    params = {
            'view': ['mTeam', 'mRoster', 'mMatchup', 'mSettings', 'mStandings']
        }
    
    cookies = {
        'espn_s2': req.league_info.espn_s2,
        'SWID': req.league_info.swid
    }
    
    endpoint = ESPN_FANTASY_ENDPOINT.format(req.league_info.year, req.league_info.league_id)
    data = requests.get(endpoint, params=params, cookies=cookies).json()
    roster = get_roster(req.league_info.team_name, data['teams'])
    players = [Player(player, req.league_info.year) for player in roster]

    team_abbrev_corrections = {"PHL": "PHI", "PHO": "PHX"}
    pos_to_keep = {"PG", "SG", "SF", "PF", "C", "G", "F"}

    return [PlayerResp(name=player.name,
                                avg_points=player.avg_points,
                                team=team_abbrev_corrections.get(player.proTeam, player.proTeam),
                                valid_positions=[pos for pos in player.eligibleSlots if pos in pos_to_keep] + ["UT1", "UT2", "UT3"],
                                injured=player.injured,
                                ) for player in players]
    
    
            


# Returns important data for free agents in a league
@router.post("/get_freeagent_data")
async def get_free_agents(req: TeamDataReq):

    params = {
        'view': 'kona_player_info',
        'scoringPeriodId': 0,
    }

    filters = {"players":{"filterStatus":{"value":["FREEAGENT","WAIVERS"]},"filterSlotIds":{"value":[]},"limit":req.fa_count,"sortPercOwned":{"sortPriority":1,"sortAsc":False},"sortDraftRanks":{"sortPriority":100,"sortAsc":True,"value":"STANDARD"}}}
    headers = {'x-fantasy-filter': json.dumps(filters)}

    cookies = {
        'espn_s2': req.league_info.espn_s2,
        'SWID': req.league_info.swid
    }

    endpoint = ESPN_FANTASY_ENDPOINT.format(req.league_info.year, req.league_info.league_id)
    data = requests.get(endpoint, params=params, headers=headers, cookies=cookies).json()
    players = [Player(player, req.league_info.year) for player in data['players']]

    team_abbrev_corrections = {"PHL": "PHI", "PHO": "PHX"}
    pos_to_keep = {"PG", "SG", "SF", "PF", "C", "G", "F"}

    return [PlayerResp(name=player.name,
                        avg_points=player.avg_points,
                        team=team_abbrev_corrections.get(player.proTeam, player.proTeam),
                        valid_positions=[pos for pos in player.eligibleSlots if pos in pos_to_keep] + ["UT1", "UT2", "UT3"],
                        injured=player.injured,
                        ) for player in players]



# Returns the most recent FPTS ETL data
@router.get("/etl/get_fpts_data")
async def get_fpts_data():
    with open("file-storage/fpts_data.json", "r") as f:
        data = json.load(f)
    return {"data": data}