from schemas.rankings import RankingsResp, RankingsPlayer
from schemas.common import ApiStatus
from db.models.stats.rankings import Rankings


class RankingsService:

    @staticmethod
    async def get_rankings() -> RankingsResp:
        try:
            rankings_query = Rankings.select().order_by(Rankings.curr_rank)

            rankings_data = [
                RankingsPlayer(
                    id=row.id,
                    rank=row.curr_rank,
                    player_name=row.name,
                    team=row.team,
                    total_fpts=float(row.fpts),
                    avg_fpts=float(row.avg_fpts),
                    rank_change=row.rank_change
                )
                for row in rankings_query
            ]

            return RankingsResp(
                status=ApiStatus.SUCCESS,
                message="Rankings fetched successfully",
                data=rankings_data
            )

        except Exception as e:
            print(f"Error in get_rankings: {e}")
            return RankingsResp(status=ApiStatus.ERROR, message="Internal server error", data=[])
