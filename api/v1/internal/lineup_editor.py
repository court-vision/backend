"""
A saved team's lineup: read today's (or a later day's), get the fill-only suggestion, list
today's recommended actions, read our availability verdicts on the matchup's players, or send
the user's own slot moves to ESPN. Clerk-authenticated; ownership via `get_owned_team`;
credentials hydrated only here, at the provider boundary.
"""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query

from core.errors import AppError
from schemas.availability import TeamAvailabilityResp
from services.availability_service import MAX_DAYS, MAX_PLAYERS, AvailabilityService

from api.deps import OwnedTeamContext, get_owned_team, load_owned_league_info
from core.responses import respond
from schemas.daily_actions import DailyActionsResp
from schemas.lineup_editor import (
    ApplyLineupMovesReq,
    ApplyLineupMovesResp,
    LineupPlanResp,
    LineupStateResp,
)
from services.daily_actions_service import DailyActionsService
from services.lineup_editor_service import LineupEditorService

router = APIRouter(prefix="/teams", tags=["lineup editor"])


@router.get("/{team_id}/lineup", response_model=LineupStateResp)
async def get_lineup(
    scoring_period_id: Optional[int] = Query(
        None,
        gt=0,
        description="A later ESPN day to read; its board is writable like today's (POST .../moves with "
                    "expected_scoring_period_id = this day). Omit for today. "
                    "400 SCORING_PERIOD_OUT_OF_RANGE before today or past the season's last day.",
    ),
    team: OwnedTeamContext = Depends(get_owned_team),
):
    """Slots, eligibility, locks and game times for the team's current ESPN day, or a later one."""
    league_info = await load_owned_league_info(team)
    return respond(await LineupEditorService.read_state(team, league_info, scoring_period_id))


@router.get("/{team_id}/lineup/plan", response_model=LineupPlanResp)
async def get_lineup_plan(team: OwnedTeamContext = Depends(get_owned_team)):
    """The fill-only moves Court Vision would make today. Never writes."""
    league_info = await load_owned_league_info(team)
    return respond(await LineupEditorService.plan_today(team, league_info))


@router.post("/{team_id}/lineup/moves", response_model=ApplyLineupMovesResp)
async def apply_lineup_moves(req: ApplyLineupMovesReq, team: OwnedTeamContext = Depends(get_owned_team)):
    """Send slot moves to ESPN as one transaction for the board's day (`expected_scoring_period_id`,
    today or later), then return the re-read board."""
    league_info = await load_owned_league_info(team)
    return respond(await LineupEditorService.apply_manual(team, league_info, req))


@router.get("/{team_id}/actions", response_model=DailyActionsResp)
async def get_daily_actions(team: OwnedTeamContext = Depends(get_owned_team)):
    """Today's recommended roster actions (start / IR / pickup), each ready to stage. Never writes."""
    league_info = await load_owned_league_info(team)
    return respond(await DailyActionsService.read(team, league_info))


@router.get("/{team_id}/availability", response_model=TeamAvailabilityResp)
async def get_team_availability(
    start: date = Query(..., description="First game date (ISO)"),
    end: date = Query(..., description=f"Last game date (ISO); start to end spans at most {MAX_DAYS} days"),
    nba_ids: list[int] = Query(
        default=[], description=f"NBA player ids, the matchup's two rosters (at most {MAX_PLAYERS})",
    ),
    team: OwnedTeamContext = Depends(get_owned_team),
):
    """Our verdict on each player for each game date in the range (out, doubtful, questionable,
    probable, available), the source that decided it and when it changed. A player with no
    verdict that day has no row: ESPN's own status stands for him. Never reads ESPN."""
    if end < start or (end - start).days >= MAX_DAYS:
        raise AppError("AVAILABILITY_RANGE_INVALID",
                       f"end must be on or after start, and start to end may span at most {MAX_DAYS} days",
                       status_code=422)
    if len(nba_ids) > MAX_PLAYERS:
        raise AppError("AVAILABILITY_TOO_MANY_PLAYERS", f"At most {MAX_PLAYERS} players", status_code=422)
    return respond(await AvailabilityService.for_team(nba_ids, start, end))
