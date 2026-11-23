"""League projections for one owned team (services/league_projection.py): every roster
projected under the league's scoring, the league's projections as the benchmark, and
playoff odds."""

from typing import Optional

from .common import ApiModel, BaseResponse


class Benchmark(ApiModel):
    """One measure across the league, and where your team stands in it (rank 1 = best)."""
    you: Optional[float] = None
    rank: Optional[int] = None
    median: Optional[float] = None
    low: Optional[float] = None          # 25th percentile
    high: Optional[float] = None         # 75th percentile
    line: Optional[float] = None         # the playoff line: the last team in
    best: Optional[float] = None
    worst: Optional[float] = None
    teams: int = 0


class TeamProjection(ApiModel):
    league_team_id: int
    team_name: str
    mine: bool
    standing: Optional[int] = None
    wins: int
    losses: int
    ties: int
    points_for: Optional[float] = None
    points_against: Optional[float] = None
    regular_season_week: Optional[float] = None   # projected points per week, rest of the regular season
    regular_season_rank: Optional[int] = None
    playoff_week: Optional[float] = None          # projected points per week over the league's playoff weeks
    playoff_rank: Optional[int] = None
    projected_wins: Optional[float] = None        # final wins, a tie counting half
    playoff_odds: Optional[float] = None
    title_odds: Optional[float] = None
    seed_odds: list[float] = []                   # [chance of seed 1, seed 2, ...]
    players_projected: int = 0
    players_unprojected: list[str] = []


class MatchupBenchmark(Benchmark):
    """One matchup across the league: scores when final, ESPN's settled score (through
    yesterday) plus the days left when current, projections when to come."""
    matchup: int
    weeks: list[int]
    start: Optional[str] = None
    end: Optional[str] = None
    status: str                                   # final | current | upcoming
    playoffs: bool = False
    schedule_incomplete: bool = False             # the calendar is missing games that week
    opponent: Optional[str] = None
    opponent_league_team_id: Optional[int] = None
    opponent_score: Optional[float] = None
    win_chance: Optional[float] = None
    result: Optional[str] = None                  # W | L | T, when final


class LeagueBenchmarks(ApiModel):
    regular_season: Benchmark
    playoffs: Benchmark


class LeagueProjectionData(ApiModel):
    league_name: Optional[str] = None
    format: str
    today: str
    projection_as_of: Optional[str] = None
    teams_count: int
    playoff_teams: int
    seed_tiebreaker: Optional[str] = None         # how the league breaks equal records: head_to_head | points_for
    lineup_slots: dict[str, int]
    current_matchup: int
    regular_season_matchups: int
    playoff_matchups: list[int]
    playoff_weeks: list[int]
    incomplete_weeks: list[int]
    typical_week: Optional[float] = None
    simulations: int
    in_playoffs: bool
    benchmarks: LeagueBenchmarks
    teams: list[TeamProjection]
    matchups: list[MatchupBenchmark]
    notes: list[str] = []


class LeagueProjectionResp(BaseResponse):
    data: Optional[LeagueProjectionData] = None
