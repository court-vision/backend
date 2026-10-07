"""
A saved team's lineup as it stood on a finished ESPN day, from the nightly
lineup snapshots (ESPN's per-day history when the row is not there yet).
Clerk-authenticated; ownership via `get_owned_team`. `provider_team_id` reads
another team in the SAME league — the opponent — and nothing else.
"""

from datetime import date as date_type
from typing import Optional

from fastapi import APIRouter, Depends, Path, Query

from api.deps import OwnedTeamContext, get_owned_team
from core.responses import respond
from schemas.lineup_snapshots import LineupSnapshotListResp, LineupSnapshotResp
from services.lineup_snapshot_service import LineupSnapshotService

router = APIRouter(prefix="/teams", tags=["lineup snapshots"])

DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"


@router.get("/{team_id}/lineup-snapshots", response_model=LineupSnapshotListResp)
async def list_lineup_snapshots(
    from_date: Optional[str] = Query(None, alias="from", pattern=DATE_PATTERN,
                                     description="First day (YYYY-MM-DD). Default: the current matchup week."),
    to_date: Optional[str] = Query(None, alias="to", pattern=DATE_PATTERN,
                                   description="Last day (YYYY-MM-DD), at most 31 days after `from`."),
    team: OwnedTeamContext = Depends(get_owned_team),
) -> LineupSnapshotListResp:
    """The team's stored lineups over a range of finished days, with the days that have none."""
    return respond(await LineupSnapshotService.list_range(
        team,
        date_type.fromisoformat(from_date) if from_date else None,
        date_type.fromisoformat(to_date) if to_date else None,
    ))


@router.get("/{team_id}/lineup-snapshots/{date}", response_model=LineupSnapshotResp)
async def get_lineup_snapshot(
    date: str = Path(..., pattern=DATE_PATTERN, description="A finished day (YYYY-MM-DD)"),
    provider_team_id: Optional[int] = Query(
        None, gt=0,
        description="Another team in the same league (the opponent's ESPN team id). Default: this team.",
    ),
    team: OwnedTeamContext = Depends(get_owned_team),
) -> LineupSnapshotResp:
    """One team's roster and lineup slots as they stood on that day.

    The stored snapshot when there is one; otherwise ESPN's own history of the
    day, read live (`source: provider_history`). 404 LINEUP_SNAPSHOT_NOT_FOUND
    when neither has it; 400 DATE_NOT_PAST for today or a later day.
    """
    return respond(await LineupSnapshotService.get_day(team, date_type.fromisoformat(date), provider_team_id))
