"""
An add/drop transaction for a saved ESPN team — the streamers page's "pick him
up" — and what came back: the re-read board plus who was added and dropped.

The request carries the same freshness pair as a lineup write
(`expected_scoring_period_id`, `roster_version`): a board that changed since
the page loaded is a 409 ROSTER_STALE with the fresh board, never a write over
it. At least one of `add_player_id` / `drop_player_id` is required and they
cannot be the same player — the service says so (422 ROSTER_TRANSACTION_INVALID
with `data.reason`) rather than the schema, so every refusal has one shape.
"""

from __future__ import annotations

from typing import Optional

from pydantic import Field

from schemas.common import ApiModel, BaseResponse
from schemas.lineup_editor import LineupState


class RosterTransactionReq(ApiModel):
    add_player_id: Optional[int] = Field(default=None, gt=0)      # ESPN player id to pick up
    drop_player_id: Optional[int] = Field(default=None, gt=0)     # ESPN player id to release
    expected_scoring_period_id: int = Field(gt=0)
    roster_version: str = Field(min_length=1, max_length=64)


class RosterTransactionPlayer(ApiModel):
    player_id: int
    name: str
    team: str                                    # NBA tricode


class RosterTransactionData(ApiModel):
    lineup: LineupState                          # the board re-read after the write
    added: Optional[RosterTransactionPlayer] = None
    dropped: Optional[RosterTransactionPlayer] = None
    verified: bool                               # the re-read shows the add on the board and the drop gone
    audit_id: Optional[int] = None
    scoring_period_id: Optional[int] = None


class RosterTransactionResp(BaseResponse):
    data: Optional[RosterTransactionData] = None
