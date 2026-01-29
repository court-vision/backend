"""
Base Pipeline

Abstract base class for all data pipelines.
"""

from abc import ABC, abstractmethod
from typing import ClassVar

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

    Subclasses must implement:
    - config: PipelineConfig class attribute
    - execute(): The actual pipeline logic

    Example:
        class DailyPlayerStatsPipeline(BasePipeline):
            config = PipelineConfig(
                name="daily_player_stats",
                display_name="Daily Player Stats",
                description="Fetches yesterday's game stats from NBA API",
                target_table="nba.player_game_stats",
            )

            async def execute(self, ctx: PipelineContext) -> None:
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
    async def execute(self, ctx: PipelineContext) -> None:
        """
        Execute the pipeline logic.

        This is the main method subclasses implement. The base class
        handles run tracking, error handling, and result creation.

        Args:
            ctx: Pipeline context with logging, tracking, and timing

        Raises:
            Any exception will be caught and converted to a failed result
        """
        pass

    async def run(self) -> PipelineResult:
        """
        Run the pipeline with full lifecycle management.

        This is the public entry point. It:
        1. Creates a PipelineContext
        2. Starts run tracking
        3. Calls execute()
        4. Returns success or failure result

        Returns:
            PipelineResult with status, timing, and records processed
        """
        ctx = PipelineContext(self.config.name)
        ctx.start_tracking()

        try:
            # Pre-run hook
            await self.before_execute(ctx)

            # Main execution
            await self.execute(ctx)

            # Post-run hook
            await self.after_execute(ctx)

            return ctx.mark_success()

        except Exception as e:
            return ctx.mark_failed(e)

    async def before_execute(self, ctx: PipelineContext) -> None:
        """
        Hook called before execute().

        Override for validation or setup tasks.
        """
        pass

    async def after_execute(self, ctx: PipelineContext) -> None:
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
