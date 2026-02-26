from typing import Optional

from schemas.rankings import RankingsResp, RankingsPlayer
from schemas.common import ApiStatus
from db.models.stats.rankings import Rankings
from db.models.nba.player_rolling_stats import PlayerRollingStats

VALID_WINDOWS = {7, 14, 30}


class RankingsService:

    @staticmethod
    async def get_rankings(window: Optional[int] = None) -> RankingsResp:
        try:
            if window is not None and window in VALID_WINDOWS:
                return RankingsService._get_rolling_rankings(window)
            return RankingsService._get_season_rankings()

        except Exception as e:
            print(f"Error in get_rankings: {e}")
            return RankingsResp(status=ApiStatus.ERROR, message="Internal server error", data=[])

    @staticmethod
    def _get_season_rankings() -> RankingsResp:
        """Return full-season rankings from the legacy Rankings model."""
        rankings_query = Rankings.select().order_by(Rankings.curr_rank)

        rankings_data = [
            RankingsPlayer(
                id=row.id,
                rank=row.curr_rank,
                player_name=row.name,
                team=row.team,
                total_fpts=float(row.fpts),
                avg_fpts=float(row.avg_fpts),
                rank_change=row.rank_change,
            )
            for row in rankings_query
        ]

        return RankingsResp(
            status=ApiStatus.SUCCESS,
            message="Rankings fetched successfully",
            data=rankings_data,
        )

    @staticmethod
    def _get_rolling_rankings(window: int) -> RankingsResp:
        """Return rankings from the rolling averages table for a given day window."""
        latest_date, records = PlayerRollingStats.get_latest_for_window(window)

        if not records:
            return RankingsResp(
                status=ApiStatus.SUCCESS,
                message=f"No L{window} data available yet",
                data=[],
            )

        rankings_data = [
            RankingsPlayer(
                id=record.player_id,
                rank=rank,
                player_name=record.player.name,
                team=record.team_id or "",
                total_fpts=round(float(record.fpts) * record.gp, 1),
                avg_fpts=float(record.fpts),
                rank_change=0,
            )
            for rank, record in enumerate(records, start=1)
        ]

        return RankingsResp(
            status=ApiStatus.SUCCESS,
            message=f"L{window} rankings fetched successfully (as of {latest_date})",
            data=rankings_data,
        )
