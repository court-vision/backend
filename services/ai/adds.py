"""
Dime's read of the league's add limit (James, 2026-11-15): how many adds the team's league
allows this matchup and how many the team has made, straight from ESPN.

ESPN's counter (`transactionCounter.matchupAcquisitionTotals`) counts every add the team
makes, wherever it is made: ESPN's own app and site, the Week desk, a stream lane, a
scheduled pickup or Dime. ESPN sends no total: a per-game-day limit's total is counted on
ESPN's own schedule (services/acquisitions.py), so this is the board's read, made fresh for
every call. Pickups still waiting to be made take adds the counter doesn't show yet, so they
are counted beside it.

The tool definition is appended to `AGENT_TOOLS` after the draft recap's and never
reordered: the list is a cached prefix.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from core.errors import AppError
from schemas.common import FantasyProvider, LeagueInfo
from services.acquisitions import in_current_matchup

GET_ADD_LIMITS_NAME = "get_add_limits"


class GetAddLimitsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team_id: int = Field(..., gt=0, lt=2**31)


GET_ADD_LIMITS: dict[str, Any] = {
    "name": GET_ADD_LIMITS_NAME,
    "description": (
        "How many adds the team's league allows this matchup and how many the team has made, read from "
        "ESPN now. ESPN counts every add the team made this matchup wherever it was made: in ESPN's own "
        "app or site, in Court Vision, by a stream lane, a scheduled pickup or you. this_matchup is the "
        "matchup's total (per game_day: adds_per_game_day for each of the matchup's "
        "days_with_games_this_matchup, usable on any day of it), team_adds_this_matchup ESPN's count so "
        "far, left_this_matchup the rest, scheduled_this_matchup the pickups still waiting to be made this "
        "matchup (each takes one when it is made; null when the matchup's dates can't be read just now), "
        "left_after_scheduled what is left after them, and "
        "season_left the room under a season cap when the league has one. this_matchup null: the league "
        "sets no matchup limit, or its total can't be counted just now; ESPN still refuses an add over "
        "its limit. Read it before proposing or making an add, and pace the matchup's adds."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"team_id": {"type": "integer", "description": "One of the user's team_ids"}},
        "required": ["team_id"],
        "additionalProperties": False,
    },
}


async def get_add_limits(team: Any, league_info: LeagueInfo) -> dict[str, Any]:
    """The league's add limit and the team's use of it this matchup, off a fresh ESPN board."""
    from services.ai import acting
    from services.lineup_editor_service import _roster_slots
    from services.providers import get_provider_adapter

    if league_info.provider != FantasyProvider.ESPN:
        raise AppError("AI_PROVIDER_UNSUPPORTED",
                       "Add limits are read from ESPN, and this team's league isn't on ESPN")
    # The pickups before the board, as the acting path reads them: one made between the two reads
    # is then counted twice (too few left), never missed (too many)
    pending = await acting._pending_days(team.team_id)
    board = await get_provider_adapter(league_info.provider).read_lineup(
        team.team_id, league_info, fallback_slot_counts=_roster_slots(team))
    acq = board.acquisitions
    if acq is None:
        return {"team_id": team.team_id, **acting.LIMITS_UNREAD}
    out: dict[str, Any] = {"team_id": team.team_id}
    dated = acq.matchup_start is not None and acq.matchup_end is not None
    if dated:
        out["matchup"] = {"period": acq.matchup_period_id, "start": acq.matchup_start.isoformat(),
                          "end": acq.matchup_end.isoformat()}
    out.update(acting.league_limits(acq))
    out.setdefault("team_adds_this_matchup", acq.matchup_used)
    # Without the matchup's dates no pickup can be placed in it: unknown, not none
    waiting = sum(1 for d in pending if in_current_matchup(acq, d)) if dated else None
    out["scheduled_this_matchup"] = waiting
    left = out.get("left_this_matchup")
    if left is not None:
        out["left_after_scheduled"] = None if waiting is None else max(0, left - waiting)
    return out
