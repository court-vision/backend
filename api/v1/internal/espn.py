from fastapi import APIRouter, Depends
from services.espn_service import EspnService
from schemas.espn import TeamDataReq, ValidateLeagueResp, TeamDataResp, ValidateLeagueReq
from core.clerk_auth import get_current_user

# Callers supply raw league credentials (pre-team-creation), so there is no
# ownership to check — but every route still requires a signed-in user.
router = APIRouter(prefix="/espn", tags=["ESPN data"], dependencies=[Depends(get_current_user)])

@router.post("/validate_league", response_model=ValidateLeagueResp)
async def validate_league(req: ValidateLeagueReq):
    return await EspnService.check_league(req.league_info)

@router.post("/get_roster_data", response_model=TeamDataResp)
async def get_team_data(req: TeamDataReq):
    return await EspnService.get_team_data(req.league_info, req.fa_count)

@router.post("/get_freeagent_data", response_model=TeamDataResp)
async def get_free_agents(req: TeamDataReq):
    return await EspnService.get_free_agents(req.league_info, req.fa_count)
