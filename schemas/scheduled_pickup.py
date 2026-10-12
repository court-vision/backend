"""
A free-agent pickup a user schedules for a later ESPN day — the week grid's
"pick him up on Thursday" — and what the executor job made of it.

`SchedulePickupReq` names the player to add (ESPN id), optionally the player to
drop, and the ESPN day D. The row that comes back carries its first-attempt
time (`not_before_at`), its deadline (D's first tip-off), and, once settled,
the outcome: executed (with the audit ids and the seat he was given for D),
skipped (he was gone — the no-op), failed (ESPN refused, the connection
expired), expired (D's games started first) or cancelled.

`PickupExecuteReq` / `PickupResult` are the pipeline route's: one result per
row attempted this run, `deferred` meaning it stays pending and waits. A row
that stopped being the run's under it (cancelled, or claimed by a later run)
has no result. A stream lane's pickup carries `lane` (what its email adds), and
the run's `lanes` are the lane decisions a user hears about: a lane that paused,
a day it missed (schemas.stream_lane).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import AwareDatetime, Field

from schemas.common import ApiModel, BaseResponse

PickupStatus = Literal["pending", "executed", "skipped", "failed", "cancelled", "expired"]
PickupOutcome = Literal["executed", "skipped", "failed", "expired", "deferred"]
LaneNotify = Literal["email", "desk"]


class SchedulePickupReq(ApiModel):
    add_player_id: int = Field(gt=0)                           # ESPN player id to pick up
    drop_player_id: Optional[int] = Field(default=None, gt=0)  # ESPN player id to release; omit for an open seat
    scoring_period_id: int = Field(gt=0)                       # the ESPN day the pickup is for (after today)


class ScheduledPickupPlayer(ApiModel):
    player_id: int                               # ESPN player id
    name: str
    team: Optional[str] = None                   # NBA tricode
    nba_player_id: Optional[int] = None          # nba.players.id when known (for a card / headshot)


class ScheduledPickup(ApiModel):
    id: int
    team_id: int
    scoring_period_id: int
    nba_date: date
    status: PickupStatus
    reason: Optional[str] = None                 # why it settled, or why it waits
    detail: Optional[str] = None                 # ESPN's sentence / the last error
    add: ScheduledPickupPlayer
    drop: Optional[ScheduledPickupPlayer] = None
    not_before_at: datetime                      # first attempt
    deadline_at: Optional[datetime] = None       # D's first tip-off; None = until the day passes
    next_attempt_at: Optional[datetime] = None   # a deferred retry
    attempts: int = 0
    audit_id: Optional[int] = None               # usr.roster_moves row of the add/drop
    lineup_audit_id: Optional[int] = None        # usr.roster_moves row of the seat-on-D move
    seated_slot_id: Optional[int] = None
    seated_slot: Optional[str] = None            # "UT", "PG", ...; None = bench
    created_at: datetime
    executed_at: Optional[datetime] = None
    lane_id: Optional[int] = None                # a stream lane's pickup (the lane decides and cancels it)


class ScheduledPickupResp(BaseResponse):
    data: Optional[ScheduledPickup] = None


class ScheduledPickupListData(ApiModel):
    pending: list[ScheduledPickup] = []          # soonest day first
    recent: list[ScheduledPickup] = []           # settled in the last week, newest first


class ScheduledPickupListResp(BaseResponse):
    data: Optional[ScheduledPickupListData] = None


# ------------------------------- Pipeline route ------------------------------- #


class PickupExecuteReq(ApiModel):
    limit: int = Field(default=4, ge=1, le=50)   # rows per run; each is several ESPN calls
    now: Optional[AwareDatetime] = None          # clock override for dogfooding, with its offset; omit in production


class PickupLane(ApiModel):
    """A stream lane's pickup, for its email: the lane, whether it emails at all, and how the
    move compares with the projection shown before it."""
    lane_id: int
    notify: LaneNotify                            # desk: no email
    projected: Optional[ScheduledPickupPlayer] = None   # who the projection had holding the spot that day
    why: Optional[str] = None                     # as_projected | projected_gone | replanned


class PickupResult(ApiModel):
    pickup_id: int
    team_id: int
    user_id: int
    team_name: str = ""
    outcome: PickupOutcome
    reason: Optional[str] = None
    detail: Optional[str] = None
    add: ScheduledPickupPlayer
    drop: Optional[ScheduledPickupPlayer] = None
    nba_date: date
    scoring_period_id: int
    seated_slot: Optional[str] = None            # executed: where he sits for day D (None = bench)
    verified: Optional[bool] = None              # executed: the re-read showed the add (and the drop gone)
    audit_id: Optional[int] = None
    next_attempt_at: Optional[datetime] = None   # deferred: when it is tried again
    lane: Optional[PickupLane] = None            # a stream lane's pickup


class LaneNotice(ApiModel):
    """A stream lane decision the user hears about (unless the lane is desk-only): it paused
    (`reason`: holder_gone, auth_expired, ...), or a day passed without a decision (blocked, missed)."""
    lane_id: int
    team_id: int
    user_id: int
    team_name: str = ""
    notify: LaneNotify
    action: Literal["paused", "blocked"]
    reason: str
    detail: Optional[str] = None
    holder: ScheduledPickupPlayer                # who holds the lane's spot
    scoring_period_id: Optional[int] = None
    nba_date: Optional[date] = None


class PickupExecuteData(ApiModel):
    due: int                                     # due rows this run took (results may hold fewer)
    results: list[PickupResult] = []
    lanes: list[LaneNotice] = []                 # stream lane decisions to tell (switches come as results)


class PickupExecuteResp(BaseResponse):
    data: Optional[PickupExecuteData] = None
