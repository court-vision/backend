from schemas.standings import StandingsResp, StandingsPlayer
from schemas.common import ApiStatus
from db.models.stats.standings import Standings


class StandingsService:

    @staticmethod
    async def get_standings() -> StandingsResp:
        try:
            standings_query = Standings.select().order_by(Standings.curr_rank)
            
            standings_data = [
                StandingsPlayer(
                    id=row.id,
                    rank=row.curr_rank,
                    player_name=row.name,
                    team=row.team,
                    total_fpts=float(row.fpts),
                    avg_fpts=float(row.avg_fpts),
                    rank_change=row.rank_change
                )
                for row in standings_query
            ]

            return StandingsResp(
                status=ApiStatus.SUCCESS,
                message="Standings fetched successfully",
                data=standings_data
            )

        except Exception as e:
            print(f"Error in get_standings: {e}")
            return StandingsResp(status=ApiStatus.ERROR, message="Internal server error", data=[])
