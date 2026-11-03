"""
A stream lane: a standing order to keep one roster spot streaming for the rest of
the matchup (docs/STREAM_LANE_PLAN.md). The lane is the intent, not a list of
pickups; before each day it re-plans the spot with the players available then.
A team's lanes deciding the same day are planned together: they share the adds
and the lineup's seats.

`LaneProjection` is a plan as of one moment: who would hold the spot each day if
nobody else touched the market, what each adds to that day's lineup (with the
other lanes' players in it), and the gain over keeping the spot's player. It can
change; the desk says so, and the email for a move says when the move differs
from the projection shown before it.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import Field

from schemas.common import ApiModel, BaseResponse

LaneStatus = Literal["active", "paused", "ended"]
LaneNotify = Literal["email", "desk"]
LaneAction = Literal["started", "switch", "keep", "blocked", "paused", "resumed", "ended"]


class LanePlayer(ApiModel):
    player_id: int                               # ESPN player id
    name: str
    team: Optional[str] = None                   # NBA tricode
    nba_player_id: Optional[int] = None


class LaneProjectionDay(ApiModel):
    scoring_period_id: int
    nba_date: date
    player: LanePlayer                           # who holds the spot that day
    value: float                                 # what they add to that day's best lineup
    switch: bool = False                         # a new player takes the spot this day


class LaneProjection(ApiModel):
    as_of: datetime
    days: list[LaneProjectionDay] = []
    value: float                                 # the spot's projected points over the days
    hold_value: float                            # the spot's player keeping it every day (the others as planned)
    gain: float
    adds: int                                    # adds the projection makes for this spot
    adds_left: Optional[int] = None              # the budget it planned with, shared; None = no limit
    rehold_after: int                            # days before a dropped player can be added back
    lanes: int = 1                               # lanes planned together, sharing `adds_left`


# ------------------------------- requests ------------------------------- #


class StreamLanePlanReq(ApiModel):
    player_id: int = Field(gt=0)                 # ESPN id of the player whose spot to stream


class StreamLaneStartReq(StreamLanePlanReq):
    notify: LaneNotify = "email"                 # desk: the lane never emails


class StreamLaneUpdateReq(ApiModel):
    status: Optional[Literal["active", "paused"]] = None   # pause, or resume
    notify: Optional[LaneNotify] = None


# ------------------------------- responses ------------------------------- #


class StreamLanePlanOther(ApiModel):
    """An open lane the new one would be planned with, and its projection then."""
    lane_id: int
    holder: LanePlayer
    projection: LaneProjection


class StreamLanePlan(ApiModel):
    """The dry run: what a lane started now on this spot would do."""
    holder: LanePlayer
    start_period: int                            # the first ESPN day a pickup made now counts for
    end_period: int                              # the matchup's last day
    end_date: date
    first_decision_at: datetime                  # when the first day's decision is made
    projection: LaneProjection
    others: list[StreamLanePlanOther] = []       # the open lanes deciding its first day with it


class StreamLanePlanResp(BaseResponse):
    data: Optional[StreamLanePlan] = None


class StreamLaneEvent(ApiModel):
    id: int
    scoring_period_id: Optional[int] = None
    nba_date: Optional[date] = None
    action: LaneAction
    add: Optional[LanePlayer] = None             # switch: who takes the spot
    drop: Optional[LanePlayer] = None            # switch: who leaves it
    projected: Optional[LanePlayer] = None       # who the projection before the decision had on the day
    reason: Optional[str] = None
    detail: Optional[str] = None
    pickup_id: Optional[int] = None              # switch: the scheduled pickup that makes it
    pickup_status: Optional[str] = None          # and what became of it
    created_at: datetime


class StreamLane(ApiModel):
    id: int
    team_id: int
    status: LaneStatus
    notify: LaneNotify
    holder: LanePlayer                           # who holds the spot now
    original: LanePlayer                         # who held it when the lane started
    start_period: int
    end_period: int
    end_date: date
    next_period: Optional[int] = None            # the day the next decision is for (open lanes)
    next_decision_at: Optional[datetime] = None
    reason: Optional[str] = None                 # why it is paused or ended
    detail: Optional[str] = None
    projection: Optional[LaneProjection] = None  # as of the last decision
    events: list[StreamLaneEvent] = []           # newest first
    created_at: datetime
    ended_at: Optional[datetime] = None


class StreamLaneResp(BaseResponse):
    data: Optional[StreamLane] = None


class StreamLaneListData(ApiModel):
    open: list[StreamLane] = []                  # active or paused, oldest first (one per spot)
    recent: list[StreamLane] = []                # ended in the last week, newest first


class StreamLaneListResp(BaseResponse):
    data: Optional[StreamLaneListData] = None
