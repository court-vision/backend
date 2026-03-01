"""
Service for lineup optimization via features service.
"""

import httpx

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
from services.lineup_service import LineupService
from utils.constants import FEATURES_SERVER_ENDPOINT


class OptimizeService:
    """Service for lineup optimization."""

    FEATURES_TIMEOUT = 30.0  # seconds

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
            payload = {
                "roster_data": [p.model_dump() for p in roster_players],
                "free_agent_data": [p.model_dump() for p in fa_players],
                "streaming_slots": request.streaming_slots,
                "week": request.week,
            }

            log.info(
                "calling_features_service_from_team",
                endpoint=f"{FEATURES_SERVER_ENDPOINT}/generate-lineup",
                week=request.week,
                team_id=request.team_id,
                roster_size=len(roster_players),
                free_agents=len(fa_players),
                streaming_slots=request.streaming_slots,
                use_recent_stats=request.use_recent_stats,
            )

            async with httpx.AsyncClient(timeout=OptimizeService.FEATURES_TIMEOUT) as client:
                response = await client.post(
                    f"{FEATURES_SERVER_ENDPOINT}/generate-lineup",
                    json=payload,
                )
                response.raise_for_status()
                result = response.json()

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

        except httpx.TimeoutException:
            log.error("features_service_timeout", week=request.week)
            return OptimizeResp(
                status=ApiStatus.ERROR,
                message="Optimization service timed out. Please try again.",
                data=None,
            )

        except httpx.HTTPStatusError as e:
            log.error("features_service_error", status_code=e.response.status_code, detail=str(e))
            return OptimizeResp(
                status=ApiStatus.ERROR,
                message="Failed to optimize lineup",
                data=None,
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
