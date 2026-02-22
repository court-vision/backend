"""
Pipeline Context

Manages pipeline execution context including run tracking, logging, and timing.
"""

from __future__ import annotations

import traceback
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional, Any

import pytz

from core.logging import get_logger
from db.models.pipeline_run import PipelineRun
from schemas.pipeline import PipelineResult
from schemas.common import ApiStatus


@dataclass
class PipelineContext:
    """
    Manages pipeline execution context including:
    - Correlation ID for log tracing
    - PipelineRun database record
    - Timing information
    - Records processed counter

    Usage:
        ctx = PipelineContext("daily_player_stats")
        ctx.start_tracking()
        try:
            # Do work
            ctx.increment_records(10)
            return ctx.mark_success()
        except Exception as e:
            return ctx.mark_failed(e)
    """

    pipeline_name: str
    run_id: uuid.UUID = field(default_factory=uuid.uuid4)
    started_at: datetime = field(
        default_factory=lambda: datetime.now(pytz.timezone("US/Central"))
    )
    records_processed: int = 0
    date_override: Optional[date] = None

    _db_run: Optional[PipelineRun] = field(default=None, repr=False)
    _log: Any = field(default=None, repr=False)

    def __post_init__(self):
        """Initialize the bound logger."""
        self._log = get_logger("pipeline").bind(
            pipeline=self.pipeline_name,
            run_id=str(self.run_id),
        )

    @property
    def log(self):
        """Get the bound logger for this context."""
        return self._log

    def start_tracking(self) -> None:
        """
        Create PipelineRun record in database.

        This creates the audit trail record and updates the run_id
        to match the database record.
        """
        self._db_run = PipelineRun.start_run(self.pipeline_name)
        self.run_id = self._db_run.id
        self._log = self._log.bind(run_id=str(self.run_id))
        self._log.info("pipeline_started")

    def increment_records(self, count: int = 1) -> None:
        """Increment the records processed counter."""
        self.records_processed += count

    def mark_success(self, message: Optional[str] = None) -> PipelineResult:
        """
        Mark pipeline as successful and return result.

        Args:
            message: Optional custom success message

        Returns:
            PipelineResult with success status
        """
        completed_at = datetime.now(pytz.timezone("US/Central"))
        duration = (completed_at - self.started_at).total_seconds()

        if self._db_run:
            self._db_run.mark_success(records_processed=self.records_processed)

        self._log.info(
            "pipeline_completed",
            records_processed=self.records_processed,
            duration_seconds=duration,
        )

        return PipelineResult(
            status=ApiStatus.SUCCESS,
            message=message or f"{self.pipeline_name} completed successfully",
            started_at=self.started_at.isoformat(),
            completed_at=completed_at.isoformat(),
            duration_seconds=duration,
            records_processed=self.records_processed,
        )

    def mark_failed(self, error: Exception) -> PipelineResult:
        """
        Mark pipeline as failed and return error result.

        Args:
            error: The exception that caused the failure

        Returns:
            PipelineResult with error status
        """
        completed_at = datetime.now(pytz.timezone("US/Central"))
        duration = (completed_at - self.started_at).total_seconds()
        error_msg = f"{type(error).__name__}: {str(error)}"
        tb = traceback.format_exc()

        if self._db_run:
            self._db_run.mark_failed(error_msg)

        self._log.error(
            "pipeline_failed",
            error=error_msg,
            traceback=tb,
        )

        return PipelineResult(
            status=ApiStatus.ERROR,
            message=f"{self.pipeline_name} failed",
            started_at=self.started_at.isoformat(),
            completed_at=completed_at.isoformat(),
            duration_seconds=duration,
            error=f"{error_msg}\n{tb}",
        )
