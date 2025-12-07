from fastapi import APIRouter
from schemas.standings import StandingsResp
from services.standings_service import StandingsService

router = APIRouter(prefix="/standings", tags=["standings"])


@router.get('/', response_model=StandingsResp)
async def get_standings() -> StandingsResp:
    return await StandingsService.get_standings()