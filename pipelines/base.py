"""
Base Pipeline

Abstract base class for all data pipelines.
"""

import asyncio
from abc import ABC, abstractmethod
from datetime import date
from typing import ClassVar, Optional

from db.base import db
from pipelines.config import PipelineConfig
from pipelines.context import PipelineContext
from schemas.pipeline import PipelineResult


class BasePipeline(ABC):
    """
    Abstract base class for all data pipelines.

    Provides:
    - Automatic run tracking via PipelineContext
    - Structured logging with correlation IDs
    - Standardized error handling
    - Template method pattern for run lifecycle
    - Thread-based execution to avoid blocking the async event loop

    Subclasses must implement:
    - config: PipelineConfig class attribute
    - execute(): The actual pipeline logic (synchronous)

    Example:
        class DailyPlayerStatsPipeline(BasePipeline):
            config = PipelineConfig(
                name="daily_player_stats",
                display_name="Daily Player Stats",
                description="Fetches yesterday's game stats from NBA API",
                target_table="nba.player_game_stats",
            )

            def execute(self, ctx: PipelineContext) -> None:
                # Pipeline implementation
                data = self.espn_extractor.get_player_data()
                ctx.increment_records(len(data))
    """

    # Class-level configuration - must be overridden by subclasses
    config: ClassVar[PipelineConfig]

    def __init__(self):
        """Initialize pipeline and validate configuration."""
        self._validate_config()

    def _validate_config(self) -> None:
        """Validate that config is properly defined."""
        if not hasattr(self.__class__, "config") or self.__class__.config is None:
            raise ValueError(
                f"{self.__class__.__name__} must define a 'config' class attribute"
            )

    @abstractmethod
    def execute(self, ctx: PipelineContext) -> None:
        """
        Execute the pipeline logic.

        This is the main method subclasses implement. It runs in a separate
        thread to avoid blocking the async event loop. All synchronous I/O
        (HTTP requests, database calls) is safe to call directly here.

        Args:
            ctx: Pipeline context with logging, tracking, and timing

        Raises:
            Any exception will be caught and converted to a failed result
        """
        pass

    def _run_sync(self, date_override: Optional[date] = None) -> PipelineResult:
        """
        Run the full pipeline lifecycle synchronously.

        Called via asyncio.to_thread() from run() so that all blocking I/O
        (Peewee DB calls, HTTP requests) executes in a thread pool worker
        instead of on the async event loop.

        Manages its own DB connection since it runs in a separate thread
        from the request handler (Peewee uses thread-local connections).
        """
        if db.is_closed():
            db.connect()

        try:
            ctx = PipelineContext(self.config.name, date_override=date_override)
            ctx.start_tracking()

            try:
                self.before_execute(ctx)
                self.execute(ctx)
                self.after_execute(ctx)
                return ctx.mark_success()
            except Exception as e:
                return ctx.mark_failed(e)
        finally:
            if not db.is_closed():
                db.close()

    async def run(self, date_override: Optional[date] = None) -> PipelineResult:
        """
        Run the pipeline with full lifecycle management.

        This is the public entry point. The entire pipeline execution
        (including DB and HTTP I/O) runs in a thread pool worker via
        asyncio.to_thread() to avoid blocking the event loop.

        Args:
            date_override: If provided, pipelines use this date instead of
                           computing from the current time. Useful for backfills.

        Returns:
            PipelineResult with status, timing, and records processed
        """
        return await asyncio.to_thread(self._run_sync, date_override)

    def before_execute(self, ctx: PipelineContext) -> None:
        """
        Hook called before execute().

        Override for validation or setup tasks.
        """
        pass

    def after_execute(self, ctx: PipelineContext) -> None:
        """
        Hook called after successful execute().

        Override for cleanup tasks.
        """
        pass

    @classmethod
    def get_name(cls) -> str:
        """Get the pipeline name from config."""
        return cls.config.name

    @classmethod
    def get_info(cls) -> dict:
        """Get pipeline information for listing."""
        return {
            "name": cls.config.name,
            "display_name": cls.config.display_name,
            "description": cls.config.description,
            "target_table": cls.config.target_table,
        }

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}(name={self.config.name})>"
