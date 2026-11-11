"""
Dime's holds on a team (usr.team_holds, migration 0043; services/steering.py), as the
Week desk shows them: each one player over a run of days, what it does, and the
reason Dime set it. A hold is made by applying a `steer` chip from the AI desk
(`ApplyHoldsReq`), and the user clears any from the desk.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import Field

from schemas.common import ApiModel, BaseResponse

HoldKind = Literal["keep", "block", "override", "pin"]


class TeamHoldPlayer(ApiModel):
    espn_id: int                                  # ESPN player id, as the boards key players
    nba_player_id: Optional[int] = None           # nba.players.id when known (Scout)
    name: str
    team: Optional[str] = None                    # NBA tricode


class TeamHold(ApiModel):
    hold_id: int
    kind: HoldKind
    player: TeamHoldPlayer
    share: Optional[float] = Field(None, description="override only: the share of his usual value (0 = he misses)")
    starts_on: date
    ends_on: date = Field(..., description="Its last day; it is read through this day and never after")
    reason: str = Field(..., description="Why Dime set it")
    effect: str = Field(..., description="What it does, in a line")
    applied_by: Literal["user", "dime"]
    proposal_id: Optional[int] = Field(None, description="The steer chip it was made from")
    created_at: datetime


class TeamHoldsData(ApiModel):
    enabled: bool = Field(..., description="Dime can set holds for this account (it is on the AI desk); "
                                           "a hold already set is listed either way")
    holds: list[TeamHold] = []                    # live, by first day


class TeamHoldsResp(BaseResponse):
    data: Optional[TeamHoldsData] = None


class ApplyHoldsReq(ApiModel):
    proposal_id: int = Field(..., gt=0)           # the steer chip to apply (its holds set, its clears done)
