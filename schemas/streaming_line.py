"""The streaming line for one owned team (services/streaming_line.py): each of your players
against what the last stream lane worth running adds to your lineup, matchup by matchup."""

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
    line: Optional[float] = None                # what the last of them adds; None: no lane could add anyone
    line_low: Optional[float] = None            # the bottom of the band
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
    worth: float                                # what he adds to your lineup over the rest of the roster
    by_matchup: list[float]                     # the same, matchup by matchup
    under_line: Optional[bool] = None           # a lane would beat him over the window (None: no line)


class StreamingLineData(ApiModel):
    league_name: Optional[str] = None
    today: str
    projection_as_of: Optional[str] = None
    current_matchup: int
    key: Optional[ValueWindowKey] = None        # None: a first and last matchup were asked for
    first_matchup: int
    last_matchup: int
    line: Optional[float] = None                # summed over the window's matchups with a line
    line_low: Optional[float] = None
    matchups: list[MatchupStreamingLine]
    players: list[LinePlayerValue]              # by worth, most first
    free_agents_read: Optional[int] = None      # None: ESPN's list didn't load
    notes: list[str] = []


class StreamingLineResp(BaseResponse):
    data: Optional[StreamingLineData] = None
