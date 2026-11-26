"""
What the AI spend report and the caps editor send and take (services/ai/spend_report.py).

The data platform's dashboard reads `GET /v1/internal/jobs/ai/spend` and writes
`PUT /v1/internal/jobs/ai/caps`, both with the pipeline token. Money is in
millionths of a dollar (micros) everywhere except a cap's `monthly_usd`, which is
the dollars typed into the dashboard. Days are UTC, the calendar the monthly caps
reset on.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import Field

from schemas.common import ApiModel, BaseRequest, BaseResponse

Line = Literal["desk", "autopilot", "analyst"]
# What a cap can be set on: a line, or `app`, the whole app's spend on every line
# together (migration 0056). `app` is a cap only: no ledger row is on it.
CapLine = Literal["desk", "autopilot", "analyst", "app"]

# The ledger's rows, by what spent them:
#   chat           a message to Dime on the AI desk
#   draft          a note in Dime's draft room (the desk's budget)
#   checkin_reply  the user's reply inside a check-in's conversation (the desk's budget)
#   checkin        a check-in Dime started itself (the autopilot budget); `detail` is its kind
#   analyst        one of the live rankings' analyst reads
#   unlinked       desk spend whose turn is gone (a deleted chat) or was never written
Feature = Literal["chat", "draft", "checkin_reply", "checkin", "analyst", "unlinked"]


class AiCapView(ApiModel):
    """One cap as it stands: the number in force, what applies with no row, and the row."""

    cap_micros: Optional[int] = Field(None, description="In force; null: no cap of the user's own on this line")
    default_cap_micros: Optional[int] = Field(None, description="What applies with no row; null: none")
    cap_set: bool = Field(False, description="A row from the dashboard sets it")
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None


class AiSpendLine(AiCapView):
    """One line's month so far."""

    line: Line
    spent_micros: int = Field(..., description="This month, settled turns and reads")
    open_micros: int = Field(..., description="This month, reservations not settled: running now, or died "
                                              "mid-call (counted at their ceiling)")


class AiSpendApp(AiCapView):
    """The whole app's month so far against its cap: every line together."""

    spent_micros: int = Field(..., description="This month on every line, settled")
    open_micros: int = Field(..., description="This month on every line, reservations not settled")


class AiSpendRow(ApiModel):
    """The ledger over the window, summed per UTC day, line, feature, user and team."""

    day: date
    line: Line
    feature: Feature
    detail: Optional[str] = Field(None, description="A check-in's kind: morning, weekly, pre_lock, night, event")
    user_id: Optional[int] = None
    team_id: Optional[int] = None
    cost_micros: int = Field(..., description="Settled and open together, as the caps count it")
    open_micros: int
    entries: int


class AiUsageRow(ApiModel):
    """What each call's own record says it used, per UTC day, feature and model: the AI
    desk's turns and the analyst's reads. Spend whose record is gone (a deleted chat) or
    never written (an open reservation) is in the ledger only."""

    day: date
    feature: Feature
    model: str
    records: int = Field(..., description="Turns, or analyst reads")
    model_calls: int
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    cost_micros: int


class AiUserLine(AiCapView):
    spent_micros: int = Field(..., description="This month, settled and open")


class AiSpendUser(ApiModel):
    """A user on the AI desk's allowlist, or with spend or a cap: their month on each line."""

    user_id: int
    email: str
    allowed: bool = Field(..., description="On AI_AGENT_USER_IDS")
    desk: AiUserLine
    autopilot: AiUserLine


class AiSpendTeam(ApiModel):
    team_id: int
    user_id: int
    name: str


class AiSpendReport(ApiModel):
    as_of: datetime
    month_start: date
    since: date = Field(..., description="First UTC day of the window")
    days: int
    turn_max_micros: int = Field(..., description="What one desk turn or check-in reserves before it starts")
    read_max_micros: int = Field(..., description="What one analyst read reserves before it starts")
    lines: list[AiSpendLine]
    app: AiSpendApp = Field(..., description="The cap on the whole app, over every line")
    rows: list[AiSpendRow]
    usage: list[AiUsageRow]
    users: list[AiSpendUser]
    teams: list[AiSpendTeam]


class AiSpendResp(BaseResponse):
    data: Optional[AiSpendReport] = None


class AiCapReq(BaseRequest):
    """Set a line's monthly cap (no `user_id`), the whole app's (`line` app), or one
    user's on a line; `monthly_usd` null deletes the row, so the environment's number
    applies again."""

    line: CapLine
    user_id: Optional[int] = Field(None, ge=1)
    monthly_usd: Optional[Decimal] = Field(None, ge=0, le=1000, max_digits=6, decimal_places=2)
    author: Optional[str] = Field(None, max_length=80)


class AiCapData(AiCapView):
    line: CapLine
    user_id: Optional[int] = None


class AiCapResp(BaseResponse):
    data: Optional[AiCapData] = None

