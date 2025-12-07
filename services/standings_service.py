# from schemas.standings import StandingsResp, StandingsPlayer
# from schemas.common import ApiStatus
# from db.models.stats_s2.cumulative_player_stats import CumulativePlayerStats
# from peewee import fn

# class StandingsService:

#     @staticmethod
#     async def get_standings():
#         try:
#             # Get the latest date
#             max_date = CumulativePlayerStats.select(fn.MAX(CumulativePlayerStats.date)).scalar()
            
#             if not max_date:
#                 return StandingsResp(status=ApiStatus.SUCCESS, message="No standings data found", data=[])

#             query = (CumulativePlayerStats
#                      .select()
#                      .where(CumulativePlayerStats.date == max_date)
#                      .order_by(CumulativePlayerStats.rank))
            
#             data = list(query.dicts())
            
#             standings_list = []
#             for player in data:
#                 # Ensure fpts and gp are numbers
#                 fpts = player['fpts'] if player['fpts'] is not None else 0
#                 gp = player['gp'] if player['gp'] is not None else 0
                
#                 avg_fpts = fpts / gp if gp > 0 else 0.0
                
#                 standings_list.append(StandingsPlayer(
#                     rank=player['rank'] if player['rank'] else 0,
#                     player_name=player['name'],
#                     team=player['team'],
#                     total_fpts=float(fpts),
#                     avg_fpts=round(float(avg_fpts), 1),
#                     rank_change=0 # Handled elsewhere
#                 ))

#             return StandingsResp(status=ApiStatus.SUCCESS, message="Standings fetched successfully", data=standings_list)
#         except Exception as e:
#             print(f"Error in get_standings: {e}")
#             return StandingsResp(status=ApiStatus.ERROR, message="Internal server error", data=[])
