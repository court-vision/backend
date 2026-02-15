"""
Pipeline API Routes

Endpoints for triggering data pipelines. Uses token-based authentication
(not Clerk) to allow cron jobs and scheduled tasks to trigger pipelines.

The /all endpoint uses a fire-and-forget pattern:
- Returns immediately with a job ID
- Pipelines run in the background
- Use /jobs/{job_id} to check status
"""

import asyncio
from typing import Optional

from fastapi import APIRouter, Security, HTTPException, Query

from core.job_manager import (
    get_job_manager,
    PipelineJobResult as JobResultInternal,
)
from core.logging import get_logger
from core.pipeline_auth import verify_pipeline_token
from pipelines import run_pipeline, run_all_pipelines, list_pipelines, PIPELINE_REGISTRY
from schemas.pipeline import (
    PipelineResponse,
    AllPipelinesResponse,
    JobCreatedResponse,
    JobStatusResponse,
    JobListResponse,
    PipelineJobInfo,
    PipelineJobDetail,
    PipelineJobResult,
)
from schemas.common import ApiStatus

router = APIRouter(prefix="/pipelines", tags=["pipelines"])
log = get_logger("pipeline_api")


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


@router.post("/player-advanced-stats", response_model=PipelineResponse)
async def trigger_player_advanced_stats(
    _: str = Security(verify_pipeline_token),
) -> PipelineResponse:
    """
    Trigger the player advanced stats pipeline.
    """
    result = await run_pipeline("player_advanced_stats")
    return PipelineResponse(
        status=result.status,
        message=result.message,
        data=result,
    )


@router.post("/all", response_model=JobCreatedResponse)
async def trigger_all_pipelines(
    _: str = Security(verify_pipeline_token),
) -> JobCreatedResponse:
    """
    Trigger all pipelines in the background (fire-and-forget).

    Returns immediately with a job ID. Use GET /jobs/{job_id} to check status.

    Runs: daily-player-stats -> cumulative-player-stats -> daily-matchup-scores
          -> advanced-stats -> game-schedule -> player-profiles
    """
    job_manager = get_job_manager()
    pipeline_count = len(PIPELINE_REGISTRY)

    # Create job record
    job = await job_manager.create_job(pipeline_count)

    # Start background task
    asyncio.create_task(_run_pipelines_background(job.job_id))

    log.info("pipeline_job_started", job_id=job.job_id, pipeline_count=pipeline_count)

    return JobCreatedResponse(
        status=ApiStatus.SUCCESS,
        message=f"Pipeline job started. Use GET /jobs/{job.job_id} to check status.",
        data=PipelineJobInfo(
            job_id=job.job_id,
            status=job.status,
            created_at=job.created_at,
            pipelines_total=job.pipelines_total,
        ),
    )


@router.post("/all/sync", response_model=AllPipelinesResponse)
async def trigger_all_pipelines_sync(
    _: str = Security(verify_pipeline_token),
) -> AllPipelinesResponse:
    """
    Trigger all pipelines synchronously (blocks until complete).

    WARNING: This can take several minutes. Use POST /all for fire-and-forget.
    Only use this endpoint if you need the results immediately and can wait.
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


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    _: str = Security(verify_pipeline_token),
    limit: int = Query(default=10, ge=1, le=50, description="Max jobs to return"),
) -> JobListResponse:
    """
    List recent pipeline jobs.

    Returns most recent jobs first.
    """
    job_manager = get_job_manager()
    jobs = await job_manager.list_jobs(limit=limit)

    return JobListResponse(
        status=ApiStatus.SUCCESS,
        message=f"Found {len(jobs)} jobs",
        data=[
            PipelineJobInfo(
                job_id=j.job_id,
                status=j.status,
                created_at=j.created_at,
                started_at=j.started_at,
                completed_at=j.completed_at,
                duration_seconds=j.duration_seconds,
                pipelines_total=j.pipelines_total,
                pipelines_completed=j.pipelines_completed,
                pipelines_failed=j.pipelines_failed,
                current_pipeline=j.current_pipeline,
            )
            for j in jobs
        ],
    )


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(
    job_id: str,
    _: str = Security(verify_pipeline_token),
) -> JobStatusResponse:
    """
    Get the status of a pipeline job.

    Returns current status, progress, and results for completed pipelines.
    """
    job_manager = get_job_manager()
    job = await job_manager.get_job(job_id)

    if job is None:
        raise HTTPException(
            status_code=404,
            detail=f"Job {job_id} not found. Jobs are kept in memory and may be lost on restart.",
        )

    # Convert internal results to API results
    results = {
        name: PipelineJobResult(
            pipeline_name=r.pipeline_name,
            status=r.status,
            message=r.message,
            started_at=r.started_at,
            completed_at=r.completed_at,
            duration_seconds=r.duration_seconds,
            records_processed=r.records_processed,
            error=r.error,
        )
        for name, r in job.results.items()
    }

    return JobStatusResponse(
        status=ApiStatus.SUCCESS,
        message=f"Job is {job.status.value}",
        data=PipelineJobDetail(
            job_id=job.job_id,
            status=job.status,
            created_at=job.created_at,
            started_at=job.started_at,
            completed_at=job.completed_at,
            duration_seconds=job.duration_seconds,
            pipelines_total=job.pipelines_total,
            pipelines_completed=job.pipelines_completed,
            pipelines_failed=job.pipelines_failed,
            current_pipeline=job.current_pipeline,
            results=results,
            error=job.error,
        ),
    )


async def _run_pipelines_background(job_id: str) -> None:
    """
    Run all pipelines in the background and update job status.

    This function is spawned as a background task and runs independently.
    """
    job_manager = get_job_manager()
    pipeline_names = list(PIPELINE_REGISTRY.keys())

    log.info("background_job_starting", job_id=job_id, pipelines=pipeline_names)

    await job_manager.update_job_started(job_id)

    try:
        for i, name in enumerate(pipeline_names, 1):
            log.info(
                "background_pipeline_starting",
                job_id=job_id,
                pipeline=name,
                step=f"{i}/{len(pipeline_names)}",
            )

            await job_manager.update_current_pipeline(job_id, name)

            try:
                result = await run_pipeline(name)

                # Convert to job result format
                # Note: result.status is already a string due to use_enum_values=True
                job_result = JobResultInternal(
                    pipeline_name=name,
                    status=result.status,
                    message=result.message,
                    started_at=result.started_at,
                    completed_at=result.completed_at,
                    duration_seconds=result.duration_seconds,
                    records_processed=result.records_processed,
                    error=result.error,
                )

                await job_manager.add_pipeline_result(job_id, name, job_result)

                log.info(
                    "background_pipeline_completed",
                    job_id=job_id,
                    pipeline=name,
                    status=result.status,
                )

            except Exception as e:
                log.error(
                    "background_pipeline_error",
                    job_id=job_id,
                    pipeline=name,
                    error=str(e),
                )

                job_result = JobResultInternal(
                    pipeline_name=name,
                    status="error",
                    message=f"Pipeline failed with exception: {e}",
                    error=str(e),
                )
                await job_manager.add_pipeline_result(job_id, name, job_result)

        # Check if all succeeded
        job = await job_manager.get_job(job_id)
        all_success = job.pipelines_failed == 0 if job else False

        await job_manager.complete_job(job_id, success=all_success)

        log.info(
            "background_job_completed",
            job_id=job_id,
            success=all_success,
            completed=job.pipelines_completed if job else 0,
            failed=job.pipelines_failed if job else 0,
        )

    except Exception as e:
        log.error("background_job_failed", job_id=job_id, error=str(e))
        await job_manager.complete_job(job_id, success=False, error=str(e))
