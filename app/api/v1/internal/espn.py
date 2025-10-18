from fastapi import APIRouter
from app.services.espn_service import EspnService
from app.schemas.espn import LeagueInfo, TeamDataReq, ValidateLeagueResp, TeamDataResp

router = APIRouter(prefix="/espn", tags=["ESPN data"])

@router.post("/validate_league", response_model=ValidateLeagueResp)
async def validate_league(req: LeagueInfo):
    return EspnService.check_league(req)

@router.post("/get_roster_data", response_model=TeamDataResp)
async def get_team_data(req: TeamDataReq):
    return await EspnService.get_team_data(req.league_info, req.fa_count)

@router.post("/get_freeagent_data", response_model=TeamDataResp)
async def get_free_agents(req: TeamDataReq):
    return await EspnService.get_free_agents(req.league_info, req.fa_count)
