"""
Pipeline Configuration

Immutable configuration dataclass for pipeline metadata.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class PipelineConfig:
    """
    Immutable configuration for a pipeline.

    Attributes:
        name: Internal name used for tracking (e.g., "daily_player_stats")
        display_name: Human-readable name (e.g., "Daily Player Stats")
        description: What this pipeline does
        target_table: Primary table this pipeline writes to
        max_retries: Override retry count (None uses settings default)
        retry_base_delay: Override base delay (None uses settings default)
        retry_max_delay: Override max delay (None uses settings default)
        timeout_seconds: Maximum time for pipeline execution
        allow_concurrent: Whether multiple instances can run simultaneously
        depends_on: Pipeline names that must complete first
    """

    name: str
    display_name: str
    description: str
    target_table: str

    # Retry configuration (override defaults from settings)
    max_retries: Optional[int] = None
    retry_base_delay: Optional[float] = None
    retry_max_delay: Optional[float] = None

    # Execution constraints
    timeout_seconds: int = 600
    allow_concurrent: bool = False

    # Dependencies (other pipeline names that must complete first)
    depends_on: tuple[str, ...] = field(default_factory=tuple)

    # If True, this pipeline is excluded from the post-game batch run.
    # Use for pipelines whose data source (e.g. ESPN matchup rollover) isn't
    # ready immediately after games end and needs its own cron schedule.
    post_game_excluded: bool = False

    def __post_init__(self):
        """Validate configuration."""
        if not self.name:
            raise ValueError("Pipeline name is required")
        if not self.target_table:
            raise ValueError("Pipeline target_table is required")
