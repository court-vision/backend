from typing import Optional

from .common import BaseResponse, CategoryDefResp, LeagueSummary


class LeagueDetail(LeagueSummary):
    matchup_periods: dict = {}
    roster_slots: dict[str, int] = {}
    # Hard per-position roster caps, e.g. {"C": 4}. Unlimited positions omitted; 0 = none allowed.
    position_limits: dict[str, int] = {}
    # {type, date, pick_order, time_per_selection, keeper_count, auction_budget}; empty until synced.
    draft_settings: dict = {}
    unsupported: list[str] = []
    warnings: list[str] = []


class LeagueGetResp(BaseResponse):
    data: Optional[LeagueDetail] = None


class LeagueSyncResp(BaseResponse):
    data: Optional[LeagueSummary] = None


__all__ = ["CategoryDefResp", "LeagueSummary", "LeagueDetail", "LeagueGetResp", "LeagueSyncResp"]
