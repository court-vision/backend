"""
Every team in the account in one read: what the Home desk shows at a glance.

    GET /v1/internal/teams/overview  ->  services.team_overview_service.overview

Each team gets one state, so the question "do I need to do anything" has one
answer per team:

    needs_you   something that won't happen unless the user acts, with the tip that
                takes the chance away as its deadline
    your_call   optional moves only (the day's stream, IR housekeeping)
    set         nothing to do; an automation may still have a move to make
    autopilot   Dime runs the team (the switch is on and its check-ins run)
    cant_read   the team's board could not be read (or the provider has none)

The items behind the state are the Week desk's daily actions, each judged
against the team's automation levels: `covered` names the automation set to
make the move, so the desk can say what will happen and when.

A block that could not be read this time is None and named in `missing` (`week` is
None too when it is the matchup that couldn't be read); only the board decides
`cant_read`. Never writes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from schemas.common import ApiModel, BaseResponse, FantasyProvider
from schemas.daily_actions import DailyAction
from schemas.lineup_editor import WriteBlockedReason
from schemas.matchup import ScoringFormat

TeamState = Literal["needs_you", "your_call", "set", "autopilot", "cant_read"]
# needs_you: nothing else will make the move. your_call: optional. covered: `covered_by` is set to make it.
ItemStanding = Literal["needs_you", "your_call", "covered"]
# auto_lineup: the day's fill, in its window before the first tip. late_scratch: the swap a ruling
# calls for. daily_actions: Court Vision making the list's moves. dime: Dime, at AI.
CoveredBy = Literal["auto_lineup", "late_scratch", "daily_actions", "dime"]
AutomationLevel = Literal["off", "notify", "act", "ai"]
# A seat tonight. live / final: his game is on or over. later: it is still to come.
# questionable: still to come, and he may not play. out: ruled out, with a game.
# idle: no game today. empty: nobody in the seat.
SeatMark = Literal["live", "later", "final", "questionable", "out", "idle", "empty"]
DayKind = Literal["past", "today", "future"]
NextKind = Literal["auto_lineup", "pickup", "lane", "checkin"]
# ready: `season` is the league as last projected. pending: it is being projected, ask again.
# unavailable: this league has no projection (categories leagues, Yahoo) or its read failed.
SeasonState = Literal["ready", "pending", "unavailable"]
OverviewBlock = Literal["automation", "matchup", "week", "next"]


class OverviewError(ApiModel):
    code: str                                    # PROVIDER_AUTH_EXPIRED, PROVIDER_TIMEOUT, LINEUP_NOT_SUPPORTED, ...
    message: str


class OverviewItem(ApiModel):
    standing: ItemStanding
    covered_by: Optional[CoveredBy] = None       # standing "covered" only
    # The tip that takes the move away: the earliest among the players it moves. Lineup kinds only.
    deadline_et: Optional[str] = None            # "19:30"
    deadline_at: Optional[datetime] = None       # the same moment, UTC
    action: DailyAction                          # the Week desk's own row, ready to stage


class OverviewAutomation(ApiModel):
    autopilot: bool = False                      # the team's autopilot switch
    dime: bool = False                           # Dime's check-ins run here (on, with CV AI access)
    lineup: AutomationLevel = "off"
    daily_actions: AutomationLevel = "off"
    lanes: AutomationLevel = "off"
    late_scratch: AutomationLevel = "off"


class OverviewSeat(ApiModel):
    slot_id: int                                 # ESPN's: 0-11 active, 12 bench, 13 IR
    slot: str
    mark: SeatMark
    player_id: Optional[int] = None              # ESPN player id; None = an empty seat
    nba_player_id: Optional[int] = None
    name: Optional[str] = None
    team: Optional[str] = None
    injury_status: Optional[str] = None
    opponent: Optional[str] = None               # "vs LAL" / "@ BOS"
    game_time_et: Optional[str] = None           # "19:30"
    points: Optional[float] = None               # tonight's fantasy points so far (points leagues)


class OverviewTonight(ApiModel):
    first_tip_et: Optional[str] = None           # the roster's first game today
    next_lock_et: Optional[str] = None           # the next tip that locks one of the roster's players
    next_lock_at: Optional[datetime] = None
    seats: list[OverviewSeat] = []               # the starting seats, in the board's order, empty ones included
    bench: list[OverviewSeat] = []
    ir: list[OverviewSeat] = []
    counts: dict[SeatMark, int] = {}             # the starting seats by mark, every mark present
    points: Optional[float] = None               # the starters' points tonight (points leagues, once a game is on)


class OverviewCategories(ApiModel):
    wins: int
    losses: int
    ties: int


class OverviewMatchup(ApiModel):
    matchup_period: int
    start: str                                   # ISO dates
    end: str
    opponent_name: str
    your_score: float                            # live; a categories league: categories won
    opponent_score: float
    your_projected: float
    opponent_projected: float
    scoring_format: ScoringFormat = "points"
    categories: Optional[OverviewCategories] = None   # categories leagues only
    # From the league projection (`season`): None until it is ready, and in a league that has none.
    win_chance: Optional[float] = None


class OverviewDay(ApiModel):
    date: str
    kind: DayKind
    # Points scored that day (today: so far). None on the days ahead and in a categories league.
    your_points: Optional[float] = None
    opponent_points: Optional[float] = None
    # Players with a game: who started (past), who starts (today), who could (ahead: healthy and
    # off IR, capped at the starting seats).
    your_games: int = 0
    opponent_games: int = 0
    # A past day counted on today's roster, because no lineup is on file for it
    estimated: bool = False


class OverviewSeason(ApiModel):
    wins: int
    losses: int
    ties: int
    standing: Optional[int] = None
    teams: int
    playoff_teams: int
    playoff_odds: Optional[float] = None
    as_of: datetime                              # when the league was projected


class OverviewNext(ApiModel):
    kind: NextKind
    at: datetime
    label: str                                   # "Auto-lineup sets the lineup", "Add A. Player, drop B. Player"


class TeamOverview(ApiModel):
    team_id: int
    team_name: str
    league_name: Optional[str] = None
    provider: FantasyProvider
    scoring_format: Optional[ScoringFormat] = None
    state: TeamState
    error: Optional[OverviewError] = None        # cant_read: why
    items: list[OverviewItem] = []               # needs_you first (soonest deadline), then your_call, then covered
    automation: Optional[OverviewAutomation] = None
    tonight: Optional[OverviewTonight] = None
    matchup: Optional[OverviewMatchup] = None
    # The matchup's days, in order. [] without a matchup now; None when they could not be read
    week: Optional[list[OverviewDay]] = []
    season: Optional[OverviewSeason] = None
    season_state: SeasonState = "unavailable"
    next: Optional[OverviewNext] = None          # the next thing scheduled to happen on the team
    # The board the items were planned on, for staging or sending them
    nba_date: Optional[str] = None
    roster_version: Optional[str] = None
    scoring_period_id: Optional[int] = None
    can_write: bool = False
    write_blocked_reason: Optional[WriteBlockedReason] = None
    missing: list[OverviewBlock] = []            # blocks that could not be read this time


class TeamsOverviewData(ApiModel):
    as_of: datetime
    counts: dict[TeamState, int] = {}            # teams by state, every state present
    next_deadline_at: Optional[datetime] = None  # the soonest needs_you deadline across the teams
    # needs_you first (soonest deadline), then cant_read, your_call, set, autopilot
    teams: list[TeamOverview] = []


class TeamsOverviewResp(BaseResponse):
    data: Optional[TeamsOverviewData] = None
