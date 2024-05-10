import requests
import json
from fastapi import FastAPI
from pydantic import BaseModel, field_validator
from utils import Player
from fastapi.middleware.cors import CORSMiddleware

def get_roster(team_name, teams):
        
        for team in teams:
            if team_name.strip() == team['name']:
                return team['roster']['entries']
            
# Defines the request body from router
class LeagueCheckerRequest(BaseModel):
    league_id: int
    espn_s2: str | None
    swid: str | None
    team_name: str
    year: int

class LeagueCheckerResponse(BaseModel):
    valid: bool
    message: str

# Defines the request body from streaming-optimization
class TeamDataRequest(BaseModel):
    league_id: int
    espn_s2: str | None
    swid: str | None
    team_name: str
    year: int
    fa_count: int

# Defines the response body
class PlayerModelResponse(BaseModel):
    name: str
    avg_points: float
    team: str
    valid_positions: list[str]
    injured: bool

    @field_validator("team")
    @classmethod
    def correct_team_name(cls, v):
        corretions = {
            "PHL": "PHI",
            "PHO": "PHX",
        }
        return corretions.get(v, v)

    @field_validator("valid_positions", mode="before")
    @classmethod
    def keep_valid_positions(cls, v, values, **kwargs):
        values_to_keep = {"PG", "SG", "SF", "PF", "C", "G", "F"}
        return [pos for pos in v if pos in values_to_keep] + ["UT1", "UT2", "UT3"]
 

app = FastAPI()

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["POST", "GET", "OPTIONS", "DELETE"],
    allow_headers=["Content-Type"],
)

@app.get("/")
async def root():
    return {"message": "Hello World"}


# Checks if the league and team are valid
@app.post("/validate_league/")
async def check_league(req: LeagueCheckerRequest):
    params = {
        'view': ['mTeam', 'mRoster', 'mMatchup', 'mSettings', 'mStandings']
    }

    endpoint = f'https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/{req.year}/segments/0/leagues/{req.league_id}'

    try:
        response = requests.get(endpoint, params=params)
        response.raise_for_status()
        data = response.json()
        teams = [team['name'] for team in data['teams']]
        return LeagueCheckerResponse(valid=True, message="Team found") if req.team_name in teams else LeagueCheckerResponse(valid=False, message="Team not found")
    except requests.exceptions.HTTPError as e:
        return LeagueCheckerResponse(valid=False, message=f"Invalid league information {e}")



# Returns important data for players on a team
@app.post("/get_roster_data/")
async def get_team_data(req: TeamDataRequest):

    params = {
            'view': ['mTeam', 'mRoster', 'mMatchup', 'mSettings', 'mStandings']
        }
    
    endpoint = f'https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/{req.year}/segments/0/leagues/{req.league_id}'
    data = requests.get(endpoint, params=params).json()
    roster = get_roster(req.team_name, data['teams'])
    players = [Player(player, req.year) for player in roster]

    return [PlayerModelResponse(name=player.name,
                                avg_points=player.avg_points,
                                team=player.proTeam,
                                valid_positions=player.eligibleSlots,
                                injured=player.injured,
                                ) for player in players]
    
    
            


# Returns important data for free agents in a league
@app.post("/get_freeagent_data/")
def get_free_agents(req: TeamDataRequest):

    params = {
        'view': 'kona_player_info',
        'scoringPeriodId': 0,
    }

    filters = {"players":{"filterStatus":{"value":["FREEAGENT","WAIVERS"]},"filterSlotIds":{"value":[]},"limit":req.fa_count,"sortPercOwned":{"sortPriority":1,"sortAsc":False},"sortDraftRanks":{"sortPriority":100,"sortAsc":True,"value":"STANDARD"}}}
    headers = {'x-fantasy-filter': json.dumps(filters)}

    endpoint = f'https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/{req.year}/segments/0/leagues/{req.league_id}'
    data = requests.get(endpoint, params=params, headers=headers).json()
    players = [Player(player, req.year) for player in data['players']]

    return [PlayerModelResponse(name=player.name,
                                avg_points=player.avg_points,
                                team=player.proTeam,
                                valid_positions=player.eligibleSlots,
                                injured=player.injured,
                                ) for player in players]

