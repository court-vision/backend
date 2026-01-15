from fastapi import APIRouter
from schemas.rankings import RankingsResp
from services.rankings_service import RankingsService

router = APIRouter(prefix="/rankings", tags=["rankings"])


@router.get('/', response_model=RankingsResp)
async def get_rankings() -> RankingsResp:
    return await RankingsService.get_rankings()
