"""
Pipeline API Routes

Endpoints for triggering data pipelines. Uses token-based authentication
(not Clerk) to allow cron jobs and scheduled tasks to trigger pipelines.
"""

from fastapi import APIRouter, Security

from core.pipeline_auth import verify_pipeline_token
from pipelines import run_pipeline, run_all_pipelines, list_pipelines
from schemas.pipeline import PipelineResponse, AllPipelinesResponse
from schemas.common import ApiStatus

router = APIRouter(prefix="/pipelines", tags=["pipelines"])


@router.get("/")
async def get_available_pipelines(
    _: str = Security(verify_pipeline_token),
) -> dict:
    """
    List all available pipelines.

    Returns pipeline names, descriptions, and target tables.
    """
    return {"pipelines": list_pipelines()}


@router.post("/daily-player-stats", response_model=PipelineResponse)
async def trigger_daily_player_stats(
    _: str = Security(verify_pipeline_token),
) -> PipelineResponse:
    """
    Trigger the daily player stats pipeline.

    Fetches yesterday's game stats from NBA API and ESPN ownership data,
    then inserts into nba.player_game_stats table.
    """
    result = await run_pipeline("daily_player_stats")
    return PipelineResponse(
        status=result.status,
        message=result.message,
        data=result,
    )


@router.post("/cumulative-player-stats", response_model=PipelineResponse)
async def trigger_cumulative_player_stats(
    _: str = Security(verify_pipeline_token),
) -> PipelineResponse:
    """
    Trigger the cumulative player stats pipeline.

    Updates season totals and rankings for players who played yesterday.
    """
    result = await run_pipeline("cumulative_player_stats")
    return PipelineResponse(
        status=result.status,
        message=result.message,
        data=result,
    )


@router.post("/daily-matchup-scores", response_model=PipelineResponse)
async def trigger_daily_matchup_scores(
    _: str = Security(verify_pipeline_token),
) -> PipelineResponse:
    """
    Trigger the daily matchup scores pipeline.

    Fetches current matchup scores for all saved teams and records
    daily snapshots for visualization.
    """
    result = await run_pipeline("daily_matchup_scores")
    return PipelineResponse(
        status=result.status,
        message=result.message,
        data=result,
    )


@router.post("/all", response_model=AllPipelinesResponse)
async def trigger_all_pipelines(
    _: str = Security(verify_pipeline_token),
) -> AllPipelinesResponse:
    """
    Trigger all pipelines in sequence.

    Runs: daily-player-stats -> cumulative-player-stats -> daily-matchup-scores
    """
    results = await run_all_pipelines()

    # Determine overall status
    all_success = all(r.status == ApiStatus.SUCCESS for r in results.values())
    overall_status = ApiStatus.SUCCESS if all_success else ApiStatus.ERROR
    message = (
        "All pipelines completed successfully"
        if all_success
        else "Some pipelines failed"
    )

    return AllPipelinesResponse(
        status=overall_status,
        message=message,
        data=results,
    )
