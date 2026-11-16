"""
The live rankings' analyst: one read of one player (the Live rankings plan,
step 4; `POST /v1/internal/jobs/analyst/read`, see `services.ai.analyst`).

The data platform builds the request from its own tables (the morning's live
projection, the stored news and availability reports, the notes) and decides
what to do with the answer: it checks every proposal against the stored items
and the bounds before anything is written. This side asks the model and counts
what it cost.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal, Optional

from pydantic import Field

from schemas.common import ApiModel, BaseRequest, BaseResponse

Decision = Literal["write", "keep", "retire", "none"]
Confidence = Literal["confirmed", "likely", "possible"]
NoteKind = Literal["injury_current", "injury_return", "role", "trade", "other"]


class AnalystItem(BaseRequest):
    """A stored item about the player: a news item or an availability report."""

    ref: str = Field(min_length=3, max_length=40, description="news:<id> or report:<id>: what a note cites")
    kind: str = Field(max_length=16, description="injury, availability, lineup, transaction, performance, rumor, other")
    citable: bool = Field(description="Whether a note may rest on it: never a rumor or a performance item")
    source: str = Field(max_length=40)
    at: Optional[str] = Field(default=None, max_length=40, description="When the source published it, Eastern")
    text: str = Field(min_length=1, max_length=2400, description="The headline and the blurb")


class AnalystTeammate(BaseRequest):
    """A rotation teammate: who the player's role may depend on."""

    player_id: int = Field(ge=1)
    name: str = Field(max_length=80)
    minutes: Optional[float] = Field(default=None, description="His usual minutes a game")
    out: bool = Field(default=False, description="Whether the projection has him out now")
    status: Optional[str] = Field(default=None, max_length=24)
    expected_return: Optional[date] = None


class AnalystReadReq(BaseRequest):
    season: str = Field(min_length=7, max_length=7)
    as_of: date = Field(description="The morning the read is for")
    player_id: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=80)
    team: Optional[str] = Field(default=None, max_length=8)
    reasons: list[str] = Field(default_factory=list, max_length=12, description="Why the screen picked him, in words")
    projection: dict[str, Any] = Field(
        default_factory=dict, description="What the live projection assumes about him this morning (see the prompt)"
    )
    recent_games: list[dict[str, Any]] = Field(default_factory=list, max_length=12)
    availability: Optional[dict[str, Any]] = Field(default=None, description="The availability feed's latest verdict")
    teammates: list[AnalystTeammate] = Field(default_factory=list, max_length=20)
    items: list[AnalystItem] = Field(default_factory=list, max_length=30)
    own_note: Optional[dict[str, Any]] = Field(default=None, description="The analyst's own live note, if it has one")
    owner_note: Optional[dict[str, Any]] = Field(default=None, description="The note written by hand, if there is one")
    bounds: dict[str, Any] = Field(default_factory=dict, description="The limits a note is held to")


class AnalystProposal(ApiModel):
    """What the analyst answered. Inputs only: a date, minutes, a share of the offense."""

    decision: Decision
    reason: str = Field(description="One or two plain sentences: what the items say and what follows")
    kind: NoteKind = "other"
    return_date: Optional[date] = None
    return_earliest: Optional[date] = None
    return_latest: Optional[date] = None
    out_for_season: bool = False
    minutes: Optional[float] = None
    usage: Optional[float] = None
    until_date: Optional[date] = None
    until_player_id: Optional[int] = None
    confidence: Confidence = "possible"
    review_date: Optional[date] = None
    cites: list[str] = Field(default_factory=list)
    against_owner_note: Optional[str] = None


class AnalystUsage(ApiModel):
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cost_micros: int = 0
    fallback: bool = False


class AnalystReadData(ApiModel):
    """How one read went.

    `outcome`: `read` (a proposal came back), `declined` (the model declined
    the request), `malformed` (the answer could not be read as a proposal),
    `budget` (this month's budget for reads is used up: nothing was asked) or
    `off` (the analyst is switched off: nothing was asked).
    """

    outcome: Literal["read", "declined", "malformed", "budget", "off"]
    proposal: Optional[AnalystProposal] = None
    detail: Optional[str] = None
    usage: Optional[AnalystUsage] = None
    prompt_version: str
    month_spent_micros: int = 0
    month_cap_micros: int = 0


class AnalystReadResp(BaseResponse):
    data: Optional[AnalystReadData] = None


class AnalystSpendData(ApiModel):
    """This month's spend on reads and the line's cap, in millionths of a dollar."""

    enabled: bool
    month_spent_micros: int
    month_cap_micros: int
    read_max_micros: int
    model: str
    prompt_version: str


class AnalystSpendResp(BaseResponse):
    data: Optional[AnalystSpendData] = None
