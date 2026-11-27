"""The streaming line for one owned team (services/streaming_line.py): your players' points per
game against the free agents optimal streaming picks up, and the spots it streams, matchup by
matchup."""

from typing import Optional

from .common import ApiModel, BaseResponse
from .league_values import ValueWindowKey


class LineLane(ApiModel):
    """The kth lane: the player who holds it now, the team's total with k lanes, and what the
    kth lane gains over k - 1."""
    lane: int
    holder: str
    holder_espn_id: int
    total: float
    gain: float
    adds: int                                   # adds the plan with k lanes makes


class LineSpot(ApiModel):
    name: str
    espn_id: int


class MatchupStreamingLine(ApiModel):
    matchup: int
    start: Optional[str] = None                 # the first day planned: today, or tomorrow once today's games start
    end: Optional[str] = None
    days: int
    adds: Optional[int] = None                  # adds the lanes could use (None: no limit)
    playoffs: bool = False
    base: float                                 # your total with no lanes
    lanes: list[LineLane]                       # one per lane count tried
    worth_running: int                          # lanes worth running
    line: Optional[float] = None                # the pickups' points per game; None: no one picked up
    line_low: Optional[float] = None            # the bottom of the band
    games: float = 0.0                          # the pickups' expected games behind the line
    streamed: list[LineSpot] = []               # your players whose spots the plan streams
    proven: bool = True                         # every plan proven best


class LinePlayerValue(ApiModel):
    name: str
    espn_id: int
    player_id: Optional[int] = None             # NBA id
    nba_team: Optional[str] = None
    positions: list[str] = []
    slot: Optional[str] = None
    injury_status: Optional[str] = None
    protected: bool = False                     # locked: never streamed
    on_ir: bool = False
    per_game: float                             # his points per game when he plays
    below_line: Optional[bool] = None           # the wire matches him game for game (None: no line, or on IR)
    streamed_in: list[int] = []                 # the matchups whose plan streams his spot
    worth: float                                # what he adds to your lineup over the rest of the roster, in points
    by_matchup: list[float]                     # the same, matchup by matchup


class StreamingLineData(ApiModel):
    league_name: Optional[str] = None
    today: str
    projection_as_of: Optional[str] = None
    current_matchup: int
    key: Optional[ValueWindowKey] = None        # None: a first and last matchup were asked for
    first_matchup: int
    last_matchup: int
    line: Optional[float] = None                # per game, over the window's pickups
    line_low: Optional[float] = None
    matchups: list[MatchupStreamingLine]
    players: list[LinePlayerValue]              # by points per game, most first
    free_agents_read: Optional[int] = None      # None: ESPN's list didn't load
    notes: list[str] = []


class StreamingLineResp(BaseResponse):
    data: Optional[StreamingLineData] = None
