"""
Lineup snapshots: a team's roster and lineup slots as they stood on one finished
ESPN day (usr.lineup_snapshots, written nightly by data-platform), or the same
shape read live from ESPN's per-day history when the row is not there yet.
"""

from typing import Literal, Optional

from schemas.common import ApiModel, BaseResponse, FantasyProvider

LineupSnapshotSource = Literal["snapshot", "provider_history"]


class LineupSnapshotPlayer(ApiModel):
    player_id: int                               # provider (ESPN) player id
    nba_player_id: Optional[int] = None          # nba.players.id when resolvable
    name: str
    team: str                                    # NBA tricode; FA when unsigned
    position: str                                # default position, e.g. "PG"
    lineup_slot_id: int                          # ESPN slot id: 0 PG ... 11 UT, 12 BE, 13 IR
    lineup_slot: str
    eligible_slots: list[str] = []
    injured: bool = False
    injury_status: Optional[str] = None          # as ESPN reported it that day
    applied_total: Optional[float] = None        # ESPN's points for him that day; None = no game


class LineupSnapshot(ApiModel):
    provider: FantasyProvider
    provider_league_id: str
    season: int                                  # provider season id (ESPN 2027 = 2026-27)
    provider_team_id: int
    team_name: str
    scoring_period_id: int                       # ESPN day (1 = opening night)
    nba_date: str                                # that day as YYYY-MM-DD
    matchup_period_id: Optional[int] = None
    opponent_provider_team_id: Optional[int] = None
    applied_stat_total: Optional[float] = None   # the day's points over the active slots
    captured_at: Optional[str] = None            # None when read live from the provider
    source: LineupSnapshotSource
    players: list[LineupSnapshotPlayer]          # starters in slot order, then bench, then IR


class LineupSnapshotResp(BaseResponse):
    data: Optional[LineupSnapshot] = None


class LineupSnapshotListData(ApiModel):
    team_id: int
    provider_team_id: Optional[int] = None       # None when the team's ESPN id could not be learned yet
    from_date: str
    to_date: str
    snapshots: list[LineupSnapshot]
    missing_dates: list[str]                     # finished days in the range with no stored snapshot


class LineupSnapshotListResp(BaseResponse):
    data: Optional[LineupSnapshotListData] = None
