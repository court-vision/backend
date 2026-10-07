"""
Service-to-service jobs, authenticated with the shared pipeline bearer token
(the same `PIPELINE_API_TOKEN` cron-runner presents to data-platform). No user
identity: the caller names the user and team, and ownership is re-checked here.
"""

from fastapi import APIRouter, Security

from core.compute import run_cpu
from core.pipeline_auth import verify_pipeline_token
from core.responses import respond
from schemas.common import ApiStatus
from schemas.lineup_editor import LineupEvaluateReq, LineupEvaluationResp
from schemas.scheduled_pickup import PickupExecuteReq, PickupExecuteResp
from schemas.valuation import (
    StandardRankResp,
    StandardValuationData,
    StandardValuationReq,
    StandardValuationResp,
)
from services.lineup_editor_service import LineupEditorService
from services.scheduled_pickup_service import ScheduledPickupService
from services.valuation.engine import playoff_weight_of
from services.valuation.standard import (
    STANDARD_LEAGUE_SIZE,
    STANDARD_ROUNDS,
    ProjectedLine,
    season_calendar,
    standard_playoff_weeks,
    standard_ranks,
)

router = APIRouter(prefix="/jobs", tags=["internal jobs"], dependencies=[Security(verify_pipeline_token)])


@router.post("/lineup/evaluate", response_model=LineupEvaluationResp)
async def evaluate_lineup(req: LineupEvaluateReq):
    """Plan today's fill-only moves for one team; apply them when `apply` is set.

    Called by data-platform's lineup-alerts pipeline for every opted-in team.
    Business outcomes are reported in `data.outcome`, never raised.
    """
    return respond(await LineupEditorService.evaluate(req))


@router.post("/pickups/execute", response_model=PickupExecuteResp)
async def execute_pickups(req: PickupExecuteReq):
    """Attempt every scheduled pickup that is due, up to `limit` rows.

    Called by data-platform's scheduled-pickups pipeline every minute (its route
    gates on whether anything is due). Each row's outcome is reported in
    `data.results`, never raised: executed, skipped (the player is gone — the
    no-op), failed, expired, or deferred (a lock, a waiver period or a writer
    outage; the row waits for `next_attempt_at`). A write is sent at most once:
    one that got no answer is settled by the next attempt from the board alone
    (executed, or failed `interrupted`). With roster writes switched off nothing
    is attempted and due rows are pushed back ten minutes.
    """
    return respond(await ScheduledPickupService.execute_due(req))


@router.post("/valuation/standard", response_model=StandardValuationResp)
async def value_standard_league(req: StandardValuationReq):
    """Rank a set of projections in the standard league, in points and in 9-cat.

    Called by data-platform's projections editor, which holds the projections
    (and the edit being previewed) and has no valuation of its own. The ranks
    are the `cv_rank` a room with no league would show for the same
    projections: ESPN's default points weights or the standard nine categories,
    twelve teams, thirteen rounds, the default fantasy-playoff weeks. Nothing
    is read from or written to the database.
    """
    weight = playoff_weight_of(req.playoff_weight)
    calendar = season_calendar()
    lines = [
        ProjectedLine(
            player_id=p.player_id, line=p.line, games=p.games, team=p.team,
            dd_rate=p.dd_rate, td_rate=p.td_rate,
        )
        for p in req.players
    ]
    ranks = await run_cpu("valuation.standard", standard_ranks, lines, calendar, weight)
    return respond(StandardValuationResp(
        status=ApiStatus.SUCCESS,
        message=f"Valued {len(ranks)} players in the standard league",
        data=StandardValuationData(
            league_size=STANDARD_LEAGUE_SIZE,
            rounds=STANDARD_ROUNDS,
            playoff_weight=weight,
            playoff_weeks=list(standard_playoff_weeks(calendar)),
            players=[StandardRankResp(**vars(rank)) for rank in ranks],
        ),
    ))
