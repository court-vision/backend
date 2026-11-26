"""Value over a window for one owned team's league (services/league_values.py): every
rostered player and the league's best free agents priced for a run of matchups under
the league's scoring, ranked, with each roster's tiers against the league's."""

from typing import Literal, Optional

from .common import ApiModel, BaseResponse

ValueWindowKey = Literal["this_week", "next_4", "to_playoffs", "playoffs", "rest_of_season"]


class ValueWindow(ApiModel):
    key: Optional[ValueWindowKey] = None        # None: a first and last matchup were asked for
    first_matchup: int                          # the current one when the window is under way
    last_matchup: int
    weeks: list[int]                            # calendar weeks (a two-week playoff round is two)
    start: Optional[str] = None                 # the first day counted: today when under way
    end: Optional[str] = None
    days: int                                   # days counted
    playoffs: bool = False                      # reaches into the league's playoffs
    schedule_incomplete: bool = False           # the calendar is missing games in it
    incomplete_weeks: list[int] = []


class PlayerWindowValue(ApiModel):
    """One player priced over the window and the rest of the season. Points are the
    league's; ranks are among every player priced (rank 1 = most)."""
    name: str
    player_id: Optional[int] = None             # NBA id
    espn_id: Optional[int] = None
    nba_team: Optional[str] = None
    positions: list[str] = []
    league_team_id: Optional[int] = None        # None: a free agent or waiver claim
    team_name: Optional[str] = None
    mine: bool = False
    slot: Optional[str] = None                  # his ESPN lineup slot now, when rostered
    availability: Optional[str] = None          # free_agent | waivers, when not rostered
    injury_status: Optional[str] = None
    per_game: float                             # points per game he plays
    team_games: int                             # his NBA team's games left in the window
    games: float                                # games he's expected to play in them
    value: float                                # per_game x games: if every game he plays counts
    started: Optional[float] = None             # what he adds to his own roster's lineup there
    window_rank: int
    season_games: float
    season_value: float                         # the same over the rest of the fantasy season
    season_rank: int
    projected: bool = True                      # False: valued off stored stats, no live projection


class TierBenchmark(ApiModel):
    """The league's Nth-best players: each roster's Nth-best window value, and yours among them."""
    tier: int
    you: Optional[float] = None
    rank: Optional[int] = None
    average: Optional[float] = None
    median: Optional[float] = None
    best: Optional[float] = None
    worst: Optional[float] = None
    teams: int = 0


class LeagueValuesData(ApiModel):
    league_name: Optional[str] = None
    format: str
    today: str
    projection_as_of: Optional[str] = None
    current_matchup: int
    regular_season_matchups: int
    final_matchup: int
    season_end: Optional[str] = None            # the last day of the fantasy season
    window: ValueWindow
    players: list[PlayerWindowValue]            # rostered, then the best free agents, by window rank
    free_agents_read: Optional[int] = None      # None: ESPN's list didn't load
    ranked_players: int                         # how many players the ranks are among
    tiers: list[TierBenchmark]
    notes: list[str] = []


class LeagueValuesResp(BaseResponse):
    data: Optional[LeagueValuesData] = None
