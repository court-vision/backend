"""
Breakout Streamer Service

Reads breakout candidates from nba.breakout_candidates (populated by the
BreakoutDetectionPipeline) and enriches them with current schedule data.

This is a pure read service — no computation, just DB read + schedule enrichment.
"""

import asyncio
from datetime import date

from db.models.nba import BreakoutCandidate, Player
from schemas.breakout import (
    BreakoutData,
    BreakoutResp,
    BreakoutCandidateResp,
    BreakoutBeneficiary,
    BreakoutInjuredPlayer,
    BreakoutSignals,
)
from schemas.common import ApiStatus
from services.schedule_service import (
    get_remaining_games,
    has_remaining_b2b,
)


class BreakoutService:
    """Service for surfacing breakout streamer candidates."""

    @staticmethod
    async def get_breakout_candidates(
        limit: int = 20,
        team_filter: str | None = None,
    ) -> BreakoutResp:
        """
        Return the latest breakout candidates, enriched with schedule data.

        Args:
            limit: Maximum number of candidates to return (default 20, max 50)
            team_filter: Optional NBA team abbreviation to filter by (e.g. "LAL")

        Returns:
            BreakoutResp with candidates sorted by breakout_score descending
        """
        def _query():
            return BreakoutCandidate.get_latest_candidates(
                limit=min(limit, 50),
                team_id=team_filter,
            )

        rows = await asyncio.to_thread(_query)

        if not rows:
            return BreakoutResp(
                status=ApiStatus.SUCCESS,
                message="No breakout candidates found. Run the breakout-detection pipeline first.",
                data=BreakoutData(
                    as_of_date=date.today(),
                    candidates=[],
                ),
            )

        as_of_date = rows[0].as_of_date

        # Fetch injured player names in bulk (not eager-loaded in get_latest_candidates)
        def _fetch_injured_players(ids: list[int]):
            return {
                p.id: p
                for p in Player.select().where(Player.id.in_(ids))
            }

        injured_ids = list({row.injured_player_id for row in rows})
        injured_players = await asyncio.to_thread(_fetch_injured_players, injured_ids)

        candidates = []
        for row in rows:
            team_abbrev = row.team_id or ""
            beneficiary_player = row.beneficiary  # eager-loaded in get_latest_candidates

            # Schedule enrichment for beneficiary's team
            games_remaining = get_remaining_games(team_abbrev)
            b2b = has_remaining_b2b(team_abbrev)

            injured = injured_players.get(row.injured_player_id)

            candidates.append(
                BreakoutCandidateResp(
                    beneficiary=BreakoutBeneficiary(
                        player_id=beneficiary_player.espn_id or beneficiary_player.id,
                        name=beneficiary_player.name,
                        team=team_abbrev,
                        position=beneficiary_player.position or "",
                        depth_rank=row.depth_rank,
                        avg_min=float(row.beneficiary_avg_min),
                        avg_fpts=float(row.beneficiary_avg_fpts),
                        games_remaining=games_remaining,
                        has_b2b=b2b,
                    ),
                    injured_player=BreakoutInjuredPlayer(
                        player_id=row.injured_player_id,
                        name=injured.name if injured else "Unknown",
                        avg_min=float(row.injured_avg_min),
                        status=row.injury_status,
                        expected_return=row.expected_return,
                    ),
                    signals=BreakoutSignals(
                        depth_rank=row.depth_rank,
                        projected_min_boost=float(row.projected_min_boost),
                        opp_min_avg=(
                            float(row.opp_min_avg)
                            if row.opp_min_avg is not None
                            else None
                        ),
                        opp_fpts_avg=(
                            float(row.opp_fpts_avg)
                            if row.opp_fpts_avg is not None
                            else None
                        ),
                        opp_game_count=row.opp_game_count or 0,
                        breakout_score=float(row.breakout_score),
                    ),
                )
            )

        return BreakoutResp(
            status=ApiStatus.SUCCESS,
            message=f"Found {len(candidates)} breakout candidates",
            data=BreakoutData(
                as_of_date=as_of_date,
                candidates=candidates,
            ),
        )
