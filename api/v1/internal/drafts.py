"""
GET /v1/internal/drafts/board — the Draft Lab board, scored by the caller's league.

v0 is stateless: the client tracks picks and passes them back as query params.
Draft sessions (usr.draft_sessions / usr.draft_picks) take over pick state in
Phase 1; this endpoint exists so the room UI has real board rows to build
against from day one.
"""

from fastapi import APIRouter, Depends, Query

from api.deps import OwnedTeamContext, get_owned_team
from core.responses import respond
from schemas.draft import DraftBoardResp
from services.draft_board_service import DraftBoardService
from services.scoring.resolver import resolve_scoring

router = APIRouter(prefix="/drafts", tags=["Drafts"])


@router.get(
    "/board",
    response_model=DraftBoardResp,
    summary="Get the draft board under a team's league scoring",
    description=(
        "Every draftable player, valued by the scoring settings of the league this team belongs "
        "to (point weights, or the fpts-scale category value for a category league), from ESPN's "
        "published projections where available and last season's per-game baseline otherwise. "
        "Rows carry the latest ESPN market rank/ADP and a `market_rank − cv_rank` delta, and are "
        "flagged `cap_blocked` when the league's hard position caps leave no room for them on the "
        "caller's roster.\n\n"
        "`cv_rank` is computed over the full pool before `picked`/`mine` are removed, so it stays "
        "stable and market-comparable throughout a draft."
    ),
    responses={
        200: {"description": "Board retrieved successfully (empty data with a message before any season data exists)"},
        404: {"description": "No such team, or it does not belong to the caller"},
        422: {"description": "Invalid picked/mine player ids"},
    },
)
async def get_draft_board(
    picked: list[int] = Query(
        default=[],
        description="NBA player ids already drafted by anyone; removed from the board",
    ),
    mine: list[int] = Query(
        default=[],
        description=(
            "NBA player ids drafted by the caller (no need to repeat them in `picked`); "
            "also removed, and counted against the league's position caps"
        ),
    ),
    team: OwnedTeamContext = Depends(get_owned_team),
):
    # `get_owned_team` already loaded the league, so resolving its scoring is
    # pure — no second trip to the database (the rankings pattern).
    scoring = resolve_scoring(team.league)
    return respond(await DraftBoardService.get_board(scoring, picked_ids=picked, my_ids=mine))
