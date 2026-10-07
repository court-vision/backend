"""
A saved team's lineup as it stood on a finished ESPN day, from the nightly
lineup snapshots (ESPN's per-day history when the row is not there yet).
Clerk-authenticated; ownership via `get_owned_team`. `provider_team_id` reads
another team in the SAME league — the opponent — and nothing else.
"""

from datetime import date as date_type
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Path, Query
from pydantic import AfterValidator

from api.deps import OwnedTeamContext, get_owned_team
from core.responses import respond
from schemas.lineup_snapshots import LineupSnapshotListResp, LineupSnapshotResp
from services.lineup_snapshot_service import LineupSnapshotService

router = APIRouter(prefix="/teams", tags=["lineup snapshots"])

DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"


def _calendar_day(value: Optional[str]) -> Optional[str]:
    """`DATE_PATTERN` also admits days the calendar does not have (2026-02-30).
    They are a 422 like any other malformed date, not a ValueError out of
    `fromisoformat` in the route, which the error handler answers with a 500."""
    if value is not None:
        try:
            date_type.fromisoformat(value)
        except ValueError:
            raise ValueError(f"{value} is not a calendar date") from None
    return value


# Pattern and validator both sit in `Annotated`: with `= Query(...)` / `= Path(...)`
# as the default instead, FastAPI drops one or the other.
CALENDAR_DAY = AfterValidator(_calendar_day)


@router.get("/{team_id}/lineup-snapshots", response_model=LineupSnapshotListResp)
async def list_lineup_snapshots(
    from_date: Annotated[Optional[str], Query(alias="from", pattern=DATE_PATTERN,
                                              description="First day (YYYY-MM-DD). Default: the current matchup week."),
                         CALENDAR_DAY] = None,
    to_date: Annotated[Optional[str], Query(alias="to", pattern=DATE_PATTERN,
                                            description="Last day (YYYY-MM-DD), at most 31 days after `from`."),
                       CALENDAR_DAY] = None,
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
    date: Annotated[str, Path(pattern=DATE_PATTERN, description="A finished day (YYYY-MM-DD)"), CALENDAR_DAY],
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
