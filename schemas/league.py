from typing import Optional

from .common import BaseResponse, CategoryDefResp, LeagueSummary


class LeagueDetail(LeagueSummary):
    matchup_periods: dict = {}
    roster_slots: dict[str, int] = {}
    unsupported: list[str] = []
    warnings: list[str] = []


class LeagueGetResp(BaseResponse):
    data: Optional[LeagueDetail] = None


class LeagueSyncResp(BaseResponse):
    data: Optional[LeagueSummary] = None


__all__ = ["CategoryDefResp", "LeagueSummary", "LeagueDetail", "LeagueGetResp", "LeagueSyncResp"]
