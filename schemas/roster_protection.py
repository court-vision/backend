"""
Protected players on a team (usr.roster_protections, migration 0048; the roster lock,
services/roster_protection.py), as the Week desk shows them. A protected player is never
dropped or suggested as a drop by Court Vision until the user unprotects him. Protecting
cancels what would drop him; the answer names what it cancelled, and what it could not
(an attempt already sending).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import Field

from schemas.common import ApiModel, BaseResponse


class ProtectedPlayer(ApiModel):
    espn_id: int                                  # ESPN player id, as the boards key players
    nba_player_id: Optional[int] = None           # nba.players.id when known (Scout)
    name: str
    team: Optional[str] = None                    # NBA tricode
    protected_at: datetime


class ProtectionCancelled(ApiModel):
    """Something protecting a player cancelled, because it would have dropped him."""
    kind: Literal["pickup", "lane"]
    id: int = Field(..., description="The scheduled pickup's id, or the stream lane's")
    summary: str = Field(..., description="What it was, in a few words (\"Thu's pickup of K. Murray\")")


class RosterProtectionsData(ApiModel):
    players: list[ProtectedPlayer] = []           # the team's protected players, by name
    cancelled: list[ProtectionCancelled] = Field(
        default_factory=list, description="Protecting only: what would have dropped him and was cancelled")
    not_cancelled: list[str] = Field(
        default_factory=list,
        description="Protecting only: what would drop him and was already being sent to ESPN, so it could not be "
                    "cancelled; the check before ESPN still refuses it unless it was out already")


class RosterProtectionsResp(BaseResponse):
    data: Optional[RosterProtectionsData] = None
