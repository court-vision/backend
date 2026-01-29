"""
Pipeline Run Model

Tracks pipeline execution history for auditing and idempotency.
Each pipeline execution creates a record with status, timing, and error info.
"""

import uuid
from datetime import datetime

from peewee import (
    UUIDField,
    CharField,
    DateTimeField,
    IntegerField,
    TextField,
)

from db.base import BaseModel


class PipelineRun(BaseModel):
    """
    Tracks individual pipeline execution runs.

    Attributes:
        id: Unique identifier for the run
        pipeline_name: Name of the pipeline (e.g., "daily_player_stats")
        started_at: When the pipeline started
        completed_at: When the pipeline finished (null if still running)
        status: Current status (running, success, failed)
        records_processed: Number of records processed
        error_message: Error details if failed
    """

    id = UUIDField(primary_key=True, default=uuid.uuid4)
    pipeline_name = CharField(max_length=50, index=True)
    started_at = DateTimeField()
    completed_at = DateTimeField(null=True)
    status = CharField(max_length=20, index=True)  # running, success, failed
    records_processed = IntegerField(default=0)
    error_message = TextField(null=True)

    class Meta:
        table_name = "pipeline_runs"
        schema = "nba"

    def __repr__(self) -> str:
        return (
            f"<PipelineRun("
            f"id={self.id}, "
            f"pipeline={self.pipeline_name}, "
            f"status={self.status})>"
        )

    @classmethod
    def start_run(cls, pipeline_name: str) -> "PipelineRun":
        """
        Create a new pipeline run record with status 'running'.

        Args:
            pipeline_name: Name of the pipeline being run

        Returns:
            The created PipelineRun instance
        """
        return cls.create(
            id=uuid.uuid4(),
            pipeline_name=pipeline_name,
            started_at=datetime.utcnow(),
            status="running",
        )

    def mark_success(self, records_processed: int = 0) -> None:
        """
        Mark the pipeline run as successful.

        Args:
            records_processed: Number of records processed
        """
        self.status = "success"
        self.completed_at = datetime.utcnow()
        self.records_processed = records_processed
        self.save()

    def mark_failed(self, error_message: str) -> None:
        """
        Mark the pipeline run as failed.

        Args:
            error_message: Description of the error
        """
        self.status = "failed"
        self.completed_at = datetime.utcnow()
        self.error_message = error_message
        self.save()

    @property
    def duration_seconds(self) -> float | None:
        """Calculate the duration of the pipeline run in seconds."""
        if self.completed_at and self.started_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None

    @classmethod
    def get_latest_successful(cls, pipeline_name: str) -> "PipelineRun | None":
        """
        Get the most recent successful run for a pipeline.

        Args:
            pipeline_name: Name of the pipeline

        Returns:
            The latest successful run, or None if none found
        """
        return (
            cls.select()
            .where(
                (cls.pipeline_name == pipeline_name) & (cls.status == "success")
            )
            .order_by(cls.completed_at.desc())
            .first()
        )

    @classmethod
    def is_running(cls, pipeline_name: str) -> bool:
        """
        Check if a pipeline is currently running.

        Args:
            pipeline_name: Name of the pipeline

        Returns:
            True if the pipeline has a 'running' status record
        """
        return (
            cls.select()
            .where(
                (cls.pipeline_name == pipeline_name) & (cls.status == "running")
            )
            .exists()
        )
