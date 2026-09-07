"""
Today's lineup for a saved team: read it, get the fill-only suggestion, or send
the user's own slot moves to ESPN. Clerk-authenticated; ownership via
`get_owned_team`; credentials hydrated only here, at the provider boundary.
"""

from fastapi import APIRouter, Depends

from api.deps import OwnedTeamContext, get_owned_team, load_owned_league_info
from core.responses import respond
from schemas.lineup_editor import (
    ApplyLineupMovesReq,
    ApplyLineupMovesResp,
    LineupPlanResp,
    LineupStateResp,
)
from services.lineup_editor_service import LineupEditorService

router = APIRouter(prefix="/teams", tags=["lineup editor"])


@router.get("/{team_id}/lineup", response_model=LineupStateResp)
async def get_lineup(team: OwnedTeamContext = Depends(get_owned_team)):
    """Slots, eligibility, locks and game times for the team's current ESPN day."""
    league_info = await load_owned_league_info(team)
    return respond(await LineupEditorService.read_state(team, league_info))


@router.get("/{team_id}/lineup/plan", response_model=LineupPlanResp)
async def get_lineup_plan(team: OwnedTeamContext = Depends(get_owned_team)):
    """The fill-only moves Court Vision would make today. Never writes."""
    league_info = await load_owned_league_info(team)
    return respond(await LineupEditorService.plan_today(team, league_info))


@router.post("/{team_id}/lineup/moves", response_model=ApplyLineupMovesResp)
async def apply_lineup_moves(req: ApplyLineupMovesReq, team: OwnedTeamContext = Depends(get_owned_team)):
    """Send slot moves to ESPN as one transaction, then return the re-read board."""
    league_info = await load_owned_league_info(team)
    return respond(await LineupEditorService.apply_manual(team, league_info, req))
