"""
Today's recommended roster actions for a saved team — one row per action, each
carrying exactly what the client needs to stage it: lineup moves in the editor's
own `LineupMoveResult` shape (start a bench player, move someone to or off IR),
or a pickup plus the player to drop for the add/drop dialog. The board the rows
were computed against travels with them, so staging always targets that board.
`GET /teams/{id}/actions` never writes.
"""

from __future__ import annotations

from typing import Literal, Optional

from schemas.common import ApiModel, BaseResponse
from schemas.espn import ValueKind
from schemas.lineup_editor import LineupMoveResult, LineupState, LineupUnfilled, WriteBlockedReason
from schemas.streamer import StreamerPlayerResp

# start: bench player into an active slot (the fill plan); ir_in: injured player to IR;
# ir_out: healthy player off IR; add / add_drop: a free agent playing today, with or
# without a player to release.
DailyActionKind = Literal["start", "ir_in", "ir_out", "add", "add_drop"]
# Row-level only. Whether anything can be written at all is data.can_write /
# data.write_blocked_reason, so a row never repeats a WriteBlockedReason.
DailyActionBlockedReason = Literal["roster_full"]


class DailyActionPlayer(ApiModel):
    player_id: int                               # ESPN player id
    nba_player_id: Optional[int] = None
    name: str
    team: str
    injury_status: Optional[str] = None
    lineup_slot_id: Optional[int] = None         # None for a free agent
    lineup_slot: Optional[str] = None
    avg_points: Optional[float] = None


class DailyActionTransaction(ApiModel):
    pickup: StreamerPlayerResp                   # handed straight to the add/drop dialog
    drop_player_id: Optional[int] = None         # None when a roster seat is open


class DailyAction(ApiModel):
    id: str                                      # stable within a board: "<kind>:<player_id>[:<drop_id>]"
    kind: DailyActionKind
    title: str
    detail: Optional[str] = None
    player: DailyActionPlayer                    # the subject of the row
    counterpart: Optional[DailyActionPlayer] = None   # the player benched or dropped
    moves: list[LineupMoveResult] = []           # lineup kinds only; moves[0] is always the subject's move
    transaction: Optional[DailyActionTransaction] = None
    blocked_reason: Optional[DailyActionBlockedReason] = None
    game_time_et: Optional[str] = None           # the subject's tip-off, "19:30"


class DailyActionsData(ApiModel):
    lineup: Optional[LineupState] = None         # None only for a non-ESPN team
    roster_version: Optional[str] = None
    scoring_period_id: Optional[int] = None
    nba_date: Optional[str] = None
    can_write: bool = False
    write_blocked_reason: Optional[WriteBlockedReason] = None
    value_kind: ValueKind = "fpts"
    unfilled: list[LineupUnfilled] = []
    actions: list[DailyAction] = []
    # The free-agent pool could not be used: the fetch failed (its message), or the pool
    # is for a different day / value scale than the board ("day_mismatch",
    # "value_kind_mismatch"). Lineup rows are unaffected.
    streamers_error: Optional[str] = None


class DailyActionsResp(BaseResponse):
    data: Optional[DailyActionsData] = None
