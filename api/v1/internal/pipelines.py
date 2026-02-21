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


@router.post("/post-game", response_model=PipelineResponse)
async def trigger_post_game(
    _: str = Security(verify_pipeline_token),
) -> PipelineResponse:
    """
    Post-game pipeline trigger with self-gating.

    Called every 15 minutes by the cron-runner. Self-gates using two checks:
    1. Time window: within [estimated_last_game_end, estimated_last_game_end + window]
    2. Data readiness: all games on the NBA date are Final (live scoreboard check)

    Only triggers once per NBA game date via date-keyed PipelineRun dedup.
    Safe to call frequently — returns immediately if outside window or already triggered.
    """
    import pytz
    from datetime import datetime, timedelta

    from core.settings import settings
    from db.models.nba.games import Game
    from db.models.pipeline_run import PipelineRun
    from pipelines.extractors.nba_api import NBAApiExtractor

    eastern = pytz.timezone("US/Eastern")
    now_et = datetime.now(eastern)

    # NBA date: before 6am ET means we're still on last night's game date
    if now_et.hour < 6:
        nba_date = (now_et - timedelta(days=1)).date()
    else:
        nba_date = now_et.date()

    # Check if there are any scheduled games on the NBA date
    latest_game_time = Game.get_latest_game_time_on_date(nba_date)
    if not latest_game_time:
        log.info("post_game_no_games", nba_date=str(nba_date))
        return PipelineResponse(
            status=ApiStatus.SUCCESS,
            message=f"No games scheduled for NBA date {nba_date}",
        )

    # Gate 1: Time window — only attempt within [estimated_end, estimated_end + window]
    latest_game_dt = datetime.combine(nba_date, latest_game_time)
    estimated_end_dt = latest_game_dt + timedelta(minutes=settings.estimated_game_duration_minutes)
    window_end_dt = estimated_end_dt + timedelta(minutes=settings.post_game_pipeline_window_minutes)
    now_et_naive = now_et.replace(tzinfo=None)

    if not (estimated_end_dt <= now_et_naive <= window_end_dt):
        log.info(
            "post_game_outside_window",
            nba_date=str(nba_date),
            estimated_end=str(estimated_end_dt),
            window_end=str(window_end_dt),
            current_time=str(now_et_naive),
        )
        return PipelineResponse(
            status=ApiStatus.SUCCESS,
            message="Outside post-game window",
        )

    # Gate 2: Data readiness — verify all games are actually Final via live scoreboard
    nba_extractor = NBAApiExtractor()
    try:
        all_final = nba_extractor.check_all_games_final(nba_date)
    except Exception as e:
        log.error("post_game_scoreboard_error", nba_date=str(nba_date), error=str(e))
        return PipelineResponse(
            status=ApiStatus.SUCCESS,
            message="Live scoreboard check failed, will retry",
        )

    if not all_final:
        log.info(
            "post_game_games_not_final",
            nba_date=str(nba_date),
            current_time=str(now_et_naive),
        )
        return PipelineResponse(
            status=ApiStatus.SUCCESS,
            message="Games still in progress, will retry next interval",
        )

    # Dedup: one trigger per NBA date, keyed by date in the pipeline_name
    dedup_key = f"post_game_trigger_{nba_date.isoformat()}"
    already_ran = (
        PipelineRun.select()
        .where(
            (PipelineRun.pipeline_name == dedup_key)
            & (PipelineRun.status == "success")
        )
        .exists()
    )
    if already_ran:
        log.info("post_game_already_triggered", nba_date=str(nba_date), dedup_key=dedup_key)
        return PipelineResponse(
            status=ApiStatus.SUCCESS,
            message=f"Already triggered post-game pipelines for {nba_date}",
        )

    # All gates pass — trigger pipelines and record dedup marker
    job_manager = get_job_manager()
    pipeline_count = len(PIPELINE_REGISTRY)
    job = await job_manager.create_job(pipeline_count)

    dedup_run = PipelineRun.start_run(dedup_key)
    dedup_run.mark_success()

    asyncio.create_task(_run_pipelines_background(job.job_id))

    log.info(
        "post_game_triggered",
        nba_date=str(nba_date),
        job_id=job.job_id,
        pipeline_count=pipeline_count,
    )

    return PipelineResponse(
        status=ApiStatus.SUCCESS,
        message=f"Post-game pipelines triggered for {nba_date}. Job ID: {job.job_id}",
    )


@router.post("/lineup-alerts", response_model=PipelineResponse)
async def trigger_lineup_alerts(
    _: str = Security(verify_pipeline_token),
) -> PipelineResponse:
    """
    Trigger the lineup alerts pipeline.

    Checks all eligible users' lineups and sends notifications if issues
    are found. Self-gates based on game start times - if no games are
    within the notification window, returns immediately.

    Safe to call frequently (every 15 min); deduplication prevents
    repeat notifications.
    """
    from pipelines.lineup_alerts import LineupAlertsPipeline

    pipeline = LineupAlertsPipeline()
    result = await pipeline.run()
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
