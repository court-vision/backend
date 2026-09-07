"""
Today's lineup as ESPN sees it — slots, eligibility, per-game locks — and the
moves that change it.

Three consumers share these shapes: the `/teams/{id}/lineup` read behind the
manual editor, the manual `POST .../lineup/moves` write, and the
pipeline-token `POST /jobs/lineup/evaluate` route the data-platform alerts
pipeline calls for every opted-in team. Slot ids are ESPN's
(`utils.espn_helpers.POSITION_MAP`): 0-11 active, 12 bench, 13 IR.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import Field

from schemas.common import ApiModel, BaseResponse, FantasyProvider
from schemas.espn import ValueKind

ScoringPeriodSource = Literal["provider", "calendar", "none"]
WriteBlockedReason = Literal[
    "provider_not_supported",
    "no_credentials",
    "writes_disabled",
    "no_scoring_period",
    "not_team_owner",
    "team_id_unresolved",
]
# start: a bench player enters an active slot; bench: an active player leaves for the
# bench; shift: an active player moves between active slots to make a chain work.
MoveRole = Literal["start", "bench", "shift"]
EvaluationOutcome = Literal["planned", "applied", "noop", "rejected", "failed", "skipped"]


# ------------------------------- Read model ------------------------------- #


class LineupSlotDef(ApiModel):
    slot_id: int
    slot: str
    count: int


class LineupPlayer(ApiModel):
    player_id: int                               # ESPN player id
    nba_player_id: Optional[int] = None
    name: str
    team: str                                    # NBA tricode
    lineup_slot_id: int
    lineup_slot: str
    eligible_slot_ids: list[int]                 # full ESPN list, incl. combo/UT/BE/IR
    eligible_slots: list[str]
    injured: bool = False
    injury_status: Optional[str] = None
    lineup_locked: bool = False                  # ESPN's flag on the roster entry
    has_game_today: bool = False
    opponent: Optional[str] = None               # "vs LAL" / "@ BOS"
    game_time_et: Optional[str] = None           # "19:30"
    game_started: bool = False                   # derived from nba.games + now (ET)
    locked: bool = False                         # lineup_locked or game_started
    playable: bool = False                       # has a game and is not OUT
    avg_points: float = 0.0
    value_kind: ValueKind = "fpts"
    value_source: Optional[str] = None


class LineupState(ApiModel):
    provider: FantasyProvider
    team_name: str
    espn_team_id: Optional[int] = None
    nba_date: Optional[str] = None               # the ESPN fantasy day this board is for
    scoring_period_id: Optional[int] = None
    scoring_period_source: ScoringPeriodSource = "none"
    first_game_time_et: Optional[str] = None
    slot_counts: dict[str, int] = {}             # id-keyed ("11": 3); JSON keys are strings
    slots: list[LineupSlotDef] = []              # ordered rows for rendering
    lock_type: Optional[str] = None              # rosterSettings.lineupLocktimeType
    players: list[LineupPlayer] = []
    can_write: bool = False
    write_blocked_reason: Optional[WriteBlockedReason] = None
    roster_version: str                          # changes whenever any slot assignment changes
    fetched_at: str


class LineupStateResp(BaseResponse):
    data: Optional[LineupState] = None


# ------------------------------- Moves ------------------------------- #


class LineupMoveReq(ApiModel):
    player_id: int = Field(gt=0)
    from_slot_id: int = Field(ge=0, le=15)
    to_slot_id: int = Field(ge=0, le=15)


class ApplyLineupMovesReq(ApiModel):
    moves: list[LineupMoveReq] = Field(min_length=1, max_length=30)
    # Both must match the board the client is looking at; otherwise 409 ROSTER_STALE
    # hands back the fresh board instead of writing over a changed roster.
    expected_scoring_period_id: int = Field(gt=0)
    roster_version: str = Field(min_length=1, max_length=64)


class LineupMoveResult(ApiModel):
    player_id: int
    name: str
    from_slot_id: int
    from_slot: str
    to_slot_id: int
    to_slot: str
    role: MoveRole = "shift"
    note: Optional[str] = None                   # "no game today", "OUT", "vs LAL · 7:30 PM"


class LineupUnfilled(ApiModel):
    """A bench player with a game today the planner could not start."""
    player_id: int
    name: str
    slot: str
    reason: str                                  # no_eligible_slot | slot_holder_locked


class MoveErrorResp(ApiModel):
    player_id: Optional[int] = None
    code: str
    message: str


class LineupPlanData(ApiModel):
    moves: list[LineupMoveResult] = []
    unfilled: list[LineupUnfilled] = []
    summary: str
    scoring_period_id: Optional[int] = None
    nba_date: Optional[str] = None
    roster_version: str


class LineupPlanResp(BaseResponse):
    data: Optional[LineupPlanData] = None


class ApplyLineupMovesData(ApiModel):
    lineup: LineupState                          # the board re-read after the write
    applied_moves: list[LineupMoveResult]
    verified: bool                               # every move confirmed by the re-read
    audit_id: Optional[int] = None


class ApplyLineupMovesResp(BaseResponse):
    data: Optional[ApplyLineupMovesData] = None


# ------------------------------- Pipeline route ------------------------------- #


class LineupEvaluateReq(ApiModel):
    team_id: int = Field(gt=0)
    user_id: int = Field(gt=0)
    nba_date: date
    apply: bool = False


class LineupEvaluationData(ApiModel):
    outcome: EvaluationOutcome
    reason: Optional[str] = None
    moves: list[LineupMoveResult] = []
    unfilled: list[LineupUnfilled] = []
    verified: Optional[bool] = None
    scoring_period_id: Optional[int] = None
    nba_date: Optional[str] = None
    first_game_time_et: Optional[str] = None
    team_name: str = ""
    audit_id: Optional[int] = None


class LineupEvaluationResp(BaseResponse):
    data: Optional[LineupEvaluationData] = None
