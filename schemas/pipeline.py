from pydantic import BaseModel
from typing import Optional
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
