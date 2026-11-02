"""
The move ledger (usr.move_outcomes): every add and drop a league's teams made and
what each earned afterwards, scored with the league's own scoring. Read by the AI
desk's "How moves went" and by Dime's `get_move_outcomes`; rebuilt nightly through
POST /v1/internal/jobs/ledger/run.
"""

from typing import Any, Literal, Optional

from pydantic import Field

from schemas.common import ApiModel, BaseResponse

MoveSource = Literal["you", "scheduled", "lane", "dime", "outside"]
MoveScope = Literal["team", "league"]


class MoveOutcomePlayer(ApiModel):
    player_id: Optional[int] = None              # NBA id; None when nba.players has no match
    name: Optional[str] = None


class MoveOutcome(ApiModel):
    move_id: int
    date: str                                    # the first day the move counted (YYYY-MM-DD)
    team_name: str
    mine: bool                                   # the asking team's move (else a rival's)
    source: MoveSource
    add: Optional[MoveOutcomePlayer] = None
    drop: Optional[MoveOutcomePlayer] = None
    paired: bool
    # {"day" | "matchup" | "two_weeks": {start, end, complete, add, drop, net | edge}, "held_through"}
    windows: dict[str, Any]
    forecast: Optional[dict[str, Any]] = None    # a Dime proposal's projection, beside the result
    reason: Optional[str] = None


class MoveSourceSummary(ApiModel):
    source: MoveSource
    moves: int
    settled: int                                 # moves whose two weeks are complete
    net_points: Optional[float] = None           # points leagues: the settled moves' summed net
    ahead: Optional[int] = None
    behind: Optional[int] = None
    categories_won: Optional[int] = None         # category leagues: over the settled moves
    categories_lost: Optional[int] = None


class MoveOutcomeListData(ApiModel):
    team_id: int
    scope: MoveScope
    format: Literal["points", "categories"]
    total: int                                   # moves matched (moves holds at most `limit`)
    moves: list[MoveOutcome]
    summary: list[MoveSourceSummary]


class MoveOutcomeListResp(BaseResponse):
    data: Optional[MoveOutcomeListData] = None


class LedgerRunReq(ApiModel):
    provider_league_id: Optional[str] = Field(default=None, max_length=64)   # one league; default every active one
    season: Optional[int] = None                 # with provider_league_id; default its newest stored season


class LedgerLeagueResult(ApiModel):
    provider_league_id: str
    season: int
    ok: bool
    days: int = 0
    moves: int = 0
    by_source: dict[str, int] = {}
    error: Optional[str] = None


class LedgerRunData(ApiModel):
    leagues: list[LedgerLeagueResult]
    rebuilt: int
    failed: int


class LedgerRunResp(BaseResponse):
    data: Optional[LedgerRunData] = None
