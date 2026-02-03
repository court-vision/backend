"""
Pipeline Job Manager

Manages background pipeline jobs with status tracking.
Uses in-memory storage (suitable for single-instance deployments).
For multi-instance deployments, replace with Redis or database storage.
"""

import asyncio
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel

from core.logging import get_logger


class JobStatus(str, Enum):
    """Status of a pipeline job."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class PipelineJobResult(BaseModel):
    """Result of a single pipeline within a job."""

    pipeline_name: str
    status: str
    message: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    records_processed: Optional[int] = None
    error: Optional[str] = None


class PipelineJob(BaseModel):
    """Represents a background pipeline job."""

    job_id: str
    status: JobStatus
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    pipelines_total: int = 0
    pipelines_completed: int = 0
    pipelines_failed: int = 0
    current_pipeline: Optional[str] = None
    results: dict[str, PipelineJobResult] = {}
    error: Optional[str] = None

    class Config:
        use_enum_values = True


class JobManager:
    """
    Manages pipeline jobs with in-memory storage.

    Thread-safe for async operations within a single process.
    Jobs are stored in memory and will be lost on restart.
    """

    # Maximum number of jobs to keep in memory (prevents unbounded growth)
    MAX_JOBS = 100

    def __init__(self):
        self._jobs: dict[str, PipelineJob] = {}
        self._lock = asyncio.Lock()
        self._log = get_logger("job_manager")

    async def create_job(self, pipeline_count: int) -> PipelineJob:
        """
        Create a new pipeline job.

        Args:
            pipeline_count: Number of pipelines that will be run

        Returns:
            The created PipelineJob
        """
        job_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()

        job = PipelineJob(
            job_id=job_id,
            status=JobStatus.PENDING,
            created_at=now,
            pipelines_total=pipeline_count,
        )

        async with self._lock:
            # Prune old jobs if we're at capacity
            if len(self._jobs) >= self.MAX_JOBS:
                await self._prune_old_jobs()

            self._jobs[job_id] = job

        self._log.info("job_created", job_id=job_id, pipeline_count=pipeline_count)
        return job

    async def get_job(self, job_id: str) -> Optional[PipelineJob]:
        """
        Get a job by ID.

        Args:
            job_id: The job ID

        Returns:
            The PipelineJob or None if not found
        """
        async with self._lock:
            return self._jobs.get(job_id)

    async def update_job_started(self, job_id: str) -> None:
        """Mark a job as started."""
        async with self._lock:
            if job := self._jobs.get(job_id):
                job.status = JobStatus.RUNNING
                job.started_at = datetime.now(timezone.utc).isoformat()

    async def update_current_pipeline(self, job_id: str, pipeline_name: str) -> None:
        """Update the currently running pipeline."""
        async with self._lock:
            if job := self._jobs.get(job_id):
                job.current_pipeline = pipeline_name

    async def add_pipeline_result(
        self,
        job_id: str,
        pipeline_name: str,
        result: PipelineJobResult,
    ) -> None:
        """
        Add a pipeline result to a job.

        Args:
            job_id: The job ID
            pipeline_name: Name of the completed pipeline
            result: The pipeline result
        """
        async with self._lock:
            if job := self._jobs.get(job_id):
                job.results[pipeline_name] = result
                job.pipelines_completed += 1
                if result.status != "success":
                    job.pipelines_failed += 1
                job.current_pipeline = None

    async def complete_job(
        self,
        job_id: str,
        success: bool,
        error: Optional[str] = None,
    ) -> None:
        """
        Mark a job as completed.

        Args:
            job_id: The job ID
            success: Whether all pipelines succeeded
            error: Optional error message if failed
        """
        async with self._lock:
            if job := self._jobs.get(job_id):
                now = datetime.now(timezone.utc)
                job.status = JobStatus.COMPLETED if success else JobStatus.FAILED
                job.completed_at = now.isoformat()
                job.current_pipeline = None
                job.error = error

                if job.started_at:
                    started = datetime.fromisoformat(job.started_at)
                    job.duration_seconds = (now - started).total_seconds()

        status = "completed" if success else "failed"
        self._log.info(f"job_{status}", job_id=job_id, error=error)

    async def list_jobs(self, limit: int = 10) -> list[PipelineJob]:
        """
        List recent jobs.

        Args:
            limit: Maximum number of jobs to return

        Returns:
            List of recent jobs, most recent first
        """
        async with self._lock:
            jobs = sorted(
                self._jobs.values(),
                key=lambda j: j.created_at,
                reverse=True,
            )
            return jobs[:limit]

    async def _prune_old_jobs(self) -> None:
        """Remove oldest completed jobs to make room for new ones."""
        # Sort by created_at and keep only the most recent MAX_JOBS/2 completed jobs
        completed = [
            (k, v)
            for k, v in self._jobs.items()
            if v.status in (JobStatus.COMPLETED, JobStatus.FAILED)
        ]
        completed.sort(key=lambda x: x[1].created_at)

        # Remove oldest half of completed jobs
        to_remove = len(completed) // 2
        for job_id, _ in completed[:to_remove]:
            del self._jobs[job_id]

        self._log.debug("jobs_pruned", removed=to_remove)


# Global job manager instance
_job_manager: Optional[JobManager] = None


def get_job_manager() -> JobManager:
    """Get the global job manager instance."""
    global _job_manager
    if _job_manager is None:
        _job_manager = JobManager()
    return _job_manager
