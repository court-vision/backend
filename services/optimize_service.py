"""
Service for lineup optimization via features service.
"""

import httpx

from core.logging import get_logger
from schemas.common import ApiStatus
from schemas.optimize import (
    OptimizeResp,
    OptimizeData,
    OptimizeRequest,
    OptimizedDay,
    RecommendedMove,
    PlayerInput,
)
from utils.constants import FEATURES_SERVER_ENDPOINT


class OptimizeService:
    """Service for lineup optimization."""

    FEATURES_TIMEOUT = 30.0  # seconds

    @staticmethod
    async def optimize_lineup(request: OptimizeRequest) -> OptimizeResp:
        """
        Optimize lineup by calling the features service.

        Args:
            request: Optimization request with roster and parameters

        Returns:
            OptimizeResp with optimized lineup data
        """
        log = get_logger()

        try:
            # Prepare payload for features service
            roster_data = [
                {
                    "id": p.id,
                    "name": p.name,
                    "team": p.team,
                    "position": p.position,
                    "avg_fpts": p.avg_fpts,
                    "injury_status": p.injury_status,
                }
                for p in request.roster
            ]

            free_agent_data = [
                {
                    "id": p.id,
                    "name": p.name,
                    "team": p.team,
                    "position": p.position,
                    "avg_fpts": p.avg_fpts,
                    "injury_status": p.injury_status,
                }
                for p in request.free_agents
            ]

            payload = {
                "roster_data": roster_data,
                "free_agent_data": free_agent_data,
                "week": request.week,
                "threshold": request.threshold,
            }

            log.info(
                "calling_features_service",
                endpoint=f"{FEATURES_SERVER_ENDPOINT}/generate-lineup",
                week=request.week,
                roster_size=len(roster_data),
                free_agents=len(free_agent_data),
            )

            async with httpx.AsyncClient(timeout=OptimizeService.FEATURES_TIMEOUT) as client:
                response = await client.post(
                    f"{FEATURES_SERVER_ENDPOINT}/generate-lineup",
                    json=payload,
                )
                response.raise_for_status()
                result = response.json()

            # Transform features service response to our schema
            optimize_data = OptimizeService._transform_response(result, request.week)

            log.info(
                "optimization_complete",
                week=request.week,
                projected_fpts=optimize_data.projected_total_fpts,
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
            log.error(
                "features_service_error",
                status_code=e.response.status_code,
                detail=str(e),
            )
            return OptimizeResp(
                status=ApiStatus.ERROR,
                message="Failed to optimize lineup",
                data=None,
            )

        except Exception as e:
            log.error("optimization_error", error=str(e))
            return OptimizeResp(
                status=ApiStatus.ERROR,
                message="Failed to optimize lineup",
                data=None,
            )

    @staticmethod
    def _transform_response(result: dict, week: int) -> OptimizeData:
        """Transform features service response to OptimizeData schema."""
        daily_lineups = []
        recommended_moves = []
        notes = []

        # Extract daily lineups from result
        if "daily_lineups" in result:
            for day in result["daily_lineups"]:
                daily_lineups.append(
                    OptimizedDay(
                        date=day.get("date", ""),
                        active_players=day.get("active", []),
                        bench_players=day.get("bench", []),
                        projected_fpts=day.get("projected_fpts", 0.0),
                    )
                )

        # Extract recommended moves
        if "moves" in result:
            for move in result["moves"]:
                player_add = None
                player_drop = None

                if move.get("add"):
                    add_data = move["add"]
                    player_add = PlayerInput(
                        id=add_data.get("id", 0),
                        name=add_data.get("name", ""),
                        team=add_data.get("team", ""),
                        position=add_data.get("position", ""),
                        avg_fpts=add_data.get("avg_fpts", 0.0),
                    )

                if move.get("drop"):
                    drop_data = move["drop"]
                    player_drop = PlayerInput(
                        id=drop_data.get("id", 0),
                        name=drop_data.get("name", ""),
                        team=drop_data.get("team", ""),
                        position=drop_data.get("position", ""),
                        avg_fpts=drop_data.get("avg_fpts", 0.0),
                    )

                recommended_moves.append(
                    RecommendedMove(
                        action=move.get("action", "stream"),
                        player_add=player_add,
                        player_drop=player_drop,
                        reason=move.get("reason", ""),
                        projected_gain=move.get("projected_gain", 0.0),
                    )
                )

        # Extract notes
        if "notes" in result:
            notes = result["notes"]

        return OptimizeData(
            week=week,
            projected_total_fpts=result.get("projected_fpts", 0.0),
            daily_lineups=daily_lineups,
            recommended_moves=recommended_moves,
            optimization_notes=notes,
        )
