"""
Service for lineup optimization via features service.
"""

from core.logging import get_logger
from db.models import Team
from schemas.common import ApiStatus
from schemas.optimize import (
    OptimizeResp,
    OptimizeData,
    GenerateLineupRequest,
    OptimizedDay,
    RecommendedMove,
    PlayerInput,
)
from services import features_client
from services.features_client import FeaturesRejected, FeaturesUnavailable
from services.lineup_service import LineupService


class OptimizeService:
    """Service for lineup optimization."""

    @staticmethod
    async def optimize_from_team(api_key, request: GenerateLineupRequest) -> OptimizeResp:
        """
        Generate an optimized lineup by auto-fetching roster and free agents from
        stored ESPN/Yahoo credentials, then calling the v2 lineup generation service.

        Looks up the team by team_id, verifies ownership via the API key's user, fetches
        roster and free agents from the provider, then calls the v2 lineup generation service.
        """
        log = get_logger()

        try:
            roster_players, fa_players = await LineupService.fetch_roster_and_fas(
                api_key.user_id, request.team_id, request.use_recent_stats
            )
        except Team.DoesNotExist:
            return OptimizeResp(
                status=ApiStatus.ERROR,
                message="Team not found or does not belong to this API key",
                data=None,
            )
        except ValueError as e:
            return OptimizeResp(status=ApiStatus.ERROR, message=str(e), data=None)

        try:
            payload = LineupService.build_features_payload(
                roster_players, fa_players, request.streaming_slots, request.week
            )
            result = await features_client.request_lineup(
                payload, caller="optimize_service", team_id=request.team_id,
                use_recent_stats=request.use_recent_stats,
            )

            optimize_data = OptimizeService._transform_v2_response(result, request.week)

            log.info(
                "optimization_from_team_complete",
                week=request.week,
                team_id=request.team_id,
                improvement=result.get("Improvement", 0),
            )

            return OptimizeResp(
                status=ApiStatus.SUCCESS,
                message=f"Lineup optimized for week {request.week}",
                data=optimize_data,
            )

        except FeaturesRejected as e:
            return OptimizeResp(
                status=ApiStatus.ERROR,
                message=e.message,
                data=None,
                error_code="LINEUP_SERVICE_REJECTED",
            )

        except FeaturesUnavailable as e:
            return OptimizeResp(
                status=ApiStatus.ERROR,
                message=e.message,
                data=None,
                error_code="LINEUP_SERVICE_UNAVAILABLE",
            )

        except Exception as e:
            log.error("optimization_from_team_error", error=str(e))
            return OptimizeResp(
                status=ApiStatus.ERROR,
                message="Failed to optimize lineup",
                data=None,
            )

    @staticmethod
    def _transform_v2_response(result: dict, week: int) -> OptimizeData:
        """Transform v2 Go service response to OptimizeData schema.

        v2 response shape:
          { Lineup: [{Day, Additions, Removals, Roster: {pos: {Name, AvgPoints, Team}}}],
            Improvement: int, Week: int, StreamingSlots: int, Timestamp: str }
        """
        daily_lineups = []
        recommended_moves = []

        lineup = result.get("Lineup", [])
        total_moves = sum(len(gene.get("Additions", [])) for gene in lineup)
        gain_per_move = result.get("Improvement", 0) / max(total_moves, 1)

        for gene in lineup:
            day = gene.get("Day", 0)
            roster = gene.get("Roster", {})

            daily_lineups.append(
                OptimizedDay(
                    date=f"Week {week}, Day {day}",
                    active_players=[p["Name"] for p in roster.values()],
                    bench_players=[],
                    projected_fpts=sum(p.get("AvgPoints", 0.0) for p in roster.values()),
                )
            )

            additions = gene.get("Additions", [])
            removals = gene.get("Removals", [])
            for i, add in enumerate(additions):
                drop = removals[i] if i < len(removals) else None
                recommended_moves.append(
                    RecommendedMove(
                        action="stream",
                        player_add=PlayerInput(
                            id=0,
                            name=add["Name"],
                            team=add["Team"],
                            position="",
                            avg_fpts=add.get("AvgPoints", 0.0),
                        ),
                        player_drop=PlayerInput(
                            id=0,
                            name=drop["Name"],
                            team=drop["Team"],
                            position="",
                            avg_fpts=drop.get("AvgPoints", 0.0),
                        ) if drop else None,
                        reason=f"Day {day} streaming move",
                        projected_gain=round(gain_per_move, 1),
                    )
                )

        notes = [
            f"Week {week}: +{result.get('Improvement', 0)} projected fpts "
            f"from {result.get('StreamingSlots', 0)} streaming slot(s)"
        ]

        return OptimizeData(
            week=week,
            projected_total_fpts=float(result.get("Improvement", 0)),
            daily_lineups=daily_lineups,
            recommended_moves=recommended_moves,
            optimization_notes=notes,
        )
