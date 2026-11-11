"""
Our availability verdicts for a team's matchup (GET /teams/{id}/availability;
services/availability_service.py). nba.player_availability is data-platform's: one
row per player per game date.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from schemas.common import ApiModel, BaseResponse

VerdictWord = Literal["out", "doubtful", "questionable", "probable", "available"]


class Verdict(ApiModel):
    nba_player_id: int
    game_date: str                               # ISO date of the game
    verdict: VerdictWord
    final: bool = False                          # the box score has spoken
    # nba_report | nba_boxscore | espn_injuries | espn_fantasy | news
    decided_by: Optional[str] = None
    reason: Optional[str] = None                 # "Left Ankle; Sprain"
    detail: Optional[str] = None                 # the source's own words
    expected_return: Optional[str] = None
    previous_verdict: Optional[str] = None
    changed_at: Optional[datetime] = None        # when the verdict last changed


class TeamAvailabilityData(ApiModel):
    start: str
    end: str
    verdicts: list[Verdict] = []                 # by player, then date; a player without one has none


class TeamAvailabilityResp(BaseResponse):
    data: Optional[TeamAvailabilityData] = None
