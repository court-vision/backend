from pydantic import BaseModel
from typing import Optional
from enum import Enum

from .common import ApiStatus


class PipelineResult(BaseModel):
    """Result of a single pipeline execution"""

    status: ApiStatus
    message: str
    started_at: str
    completed_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    records_processed: Optional[int] = None
    error: Optional[str] = None

    class Config:
        use_enum_values = True


class PipelineResponse(BaseModel):
    """Response for a single pipeline trigger"""

    status: ApiStatus
    message: str
    data: Optional[PipelineResult] = None

    class Config:
        use_enum_values = True


class AllPipelinesResponse(BaseModel):
    """Response for triggering all pipelines"""

    status: ApiStatus
    message: str
    data: Optional[dict[str, PipelineResult]] = None

    class Config:
        use_enum_values = True


# ---------------------- Job-based Pipeline Responses ---------------------- #


class JobStatus(str, Enum):
    """Status of a pipeline job."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class PipelineJobInfo(BaseModel):
    """Summary info for a pipeline job."""

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

    class Config:
        use_enum_values = True


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


class PipelineJobDetail(PipelineJobInfo):
    """Full details of a pipeline job including results."""

    results: dict[str, PipelineJobResult] = {}
    error: Optional[str] = None


class JobCreatedResponse(BaseModel):
    """Response when a job is created (fire-and-forget)."""

    status: ApiStatus
    message: str
    data: PipelineJobInfo

    class Config:
        use_enum_values = True


class JobStatusResponse(BaseModel):
    """Response for job status queries."""

    status: ApiStatus
    message: str
    data: PipelineJobDetail

    class Config:
        use_enum_values = True


class JobListResponse(BaseModel):
    """Response for listing jobs."""

    status: ApiStatus
    message: str
    data: list[PipelineJobInfo]

    class Config:
        use_enum_values = True


class LiveStatsData(BaseModel):
    """Data payload for the live stats trigger endpoint."""

    pipeline_name: str
    status: str
    records_processed: int = 0
    games_total: int = 0
    all_games_complete: bool = False
    duration_seconds: Optional[float] = None


class LiveStatsResponse(BaseModel):
    """Response for the live stats trigger endpoint.

    The all_games_complete field is read by the cron-runner's live loop
    to determine when to exit.
    """

    status: ApiStatus
    message: str
    data: LiveStatsData

    class Config:
        use_enum_values = True
