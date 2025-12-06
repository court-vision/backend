from fastapi import APIRouter
from app.schemas.standings import StandingsResp
from app.services.standings_service import StandingsService

router = APIRouter(prefix="/standings", tags=["standings"])

@router.get('/', response_model=StandingsResp)
async def get_standings():
    return await StandingsService.get_standings()