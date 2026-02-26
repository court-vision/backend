"""
Pipeline Registry and Exports

Provides a registry of all available pipelines and helper functions
for running them by name.
"""

from datetime import date
from typing import Optional, Type

from core.logging import get_logger
from pipelines.base import BasePipeline
from pipelines.config import PipelineConfig
from pipelines.context import PipelineContext
from pipelines.player_game_stats import PlayerGameStatsPipeline
from pipelines.player_season_stats import PlayerSeasonStatsPipeline
from pipelines.daily_matchup_scores import DailyMatchupScoresPipeline
from pipelines.player_advanced_stats import PlayerAdvancedStatsPipeline
from pipelines.player_ownership import PlayerOwnershipPipeline
from pipelines.player_profiles import PlayerProfilesPipeline
from pipelines.game_schedule import GameSchedulePipeline
from pipelines.game_start_times import GameStartTimesPipeline
from pipelines.injury_report import InjuryReportPipeline
from pipelines.player_rolling_stats import PlayerRollingStatsPipeline
from pipelines.team_stats import TeamStatsPipeline
from schemas.pipeline import PipelineResult
from schemas.common import ApiStatus


# Registry of all available pipelines
# Order matters for run_all_pipelines - dependencies should come first
PIPELINE_REGISTRY: dict[str, Type[BasePipeline]] = {
    # Core daily pipelines
    "player_game_stats": PlayerGameStatsPipeline,
    "player_ownership": PlayerOwnershipPipeline,
    "player_season_stats": PlayerSeasonStatsPipeline,
    "daily_matchup_scores": DailyMatchupScoresPipeline,
    # Rolling averages (depends on player_game_stats)
    "player_rolling_stats": PlayerRollingStatsPipeline,
    # Team stats (independent of player pipelines)
    "team_stats": TeamStatsPipeline,
    # Extended data pipelines
    "player_advanced_stats": PlayerAdvancedStatsPipeline,
    "game_schedule": GameSchedulePipeline,
    "game_start_times": GameStartTimesPipeline,
    # "injury_report": InjuryReportPipeline, -- requires BALLDONTLIE All-Star tier subscription
    # Reference data pipelines (run less frequently)
    "player_profiles": PlayerProfilesPipeline,
}

# Pipelines included in the post-game batch run (excludes post_game_excluded ones)
POST_GAME_PIPELINE_NAMES: list[str] = [
    name for name, cls in PIPELINE_REGISTRY.items()
    if not cls.config.post_game_excluded
]

# Notification pipelines - separate from PIPELINE_REGISTRY so they
# don't run in run_all_pipelines(). Triggered independently.
from pipelines.lineup_alerts import LineupAlertsPipeline

NOTIFICATION_PIPELINE_REGISTRY: dict[str, Type[BasePipeline]] = {
    "lineup_alerts": LineupAlertsPipeline,
}


def get_pipeline(name: str) -> BasePipeline:
    """
    Get a pipeline instance by name.

    Args:
        name: Pipeline name (e.g., "player_game_stats")

    Returns:
        Instantiated pipeline

    Raises:
        KeyError: If pipeline name not found
    """
    if name not in PIPELINE_REGISTRY:
        available = ", ".join(PIPELINE_REGISTRY.keys())
        raise KeyError(f"Unknown pipeline '{name}'. Available: {available}")

    return PIPELINE_REGISTRY[name]()


async def run_pipeline(name: str, date_override: Optional[date] = None) -> PipelineResult:
    """
    Run a pipeline by name.

    Args:
        name: Pipeline name
        date_override: If provided, the pipeline uses this date instead of
                       computing from the current time. Useful for backfills.

    Returns:
        PipelineResult with status and details
    """
    pipeline = get_pipeline(name)
    return await pipeline.run(date_override=date_override)


async def run_all_pipelines(date_override: Optional[date] = None) -> dict[str, PipelineResult]:
    """
    Run all pipelines in sequence.

    Pipelines are run in registration order:
    1. player_game_stats - Per-game box scores
    2. player_season_stats - Season totals
    3. daily_matchup_scores - Fantasy matchup tracking
    4. advanced_stats - Efficiency/usage metrics
    5. game_schedule - NBA game results
    6. injury_report - Player injury status
    7. player_profiles - Biographical data (slow, run weekly)

    Args:
        date_override: If provided, all pipelines use this date instead of
                       computing from the current time. Useful for backfills.

    Returns:
        Dict mapping pipeline name to PipelineResult
    """
    log = get_logger("pipeline").bind(operation="run_all")

    results = {}
    pipeline_names = list(PIPELINE_REGISTRY.keys())

    log.info("all_pipelines_started", count=len(pipeline_names))

    for i, name in enumerate(pipeline_names, 1):
        log.info("running_pipeline", pipeline=name, step=f"{i}/{len(pipeline_names)}")
        results[name] = await run_pipeline(name, date_override=date_override)

    success_count = sum(1 for r in results.values() if r.status == ApiStatus.SUCCESS)
    log.info(
        "all_pipelines_completed",
        success_count=success_count,
        total_count=len(results),
    )

    return results


def list_pipelines() -> list[dict]:
    """
    List all available pipelines with their configurations.

    Returns:
        List of pipeline info dicts
    """
    return [cls.get_info() for cls in PIPELINE_REGISTRY.values()]


__all__ = [
    # Base classes
    "BasePipeline",
    "PipelineConfig",
    "PipelineContext",
    # Core pipelines
    "PlayerGameStatsPipeline",
    "PlayerSeasonStatsPipeline",
    "DailyMatchupScoresPipeline",
    # Rolling averages
    "PlayerRollingStatsPipeline",
    # Team stats
    "TeamStatsPipeline",
    # Extended data pipelines
    "PlayerOwnershipPipeline",
    "AdvancedStatsPipeline",
    "PlayerProfilesPipeline",
    "GameSchedulePipeline",
    "GameStartTimesPipeline",
    "LineupAlertsPipeline",
    # Registry functions
    "PIPELINE_REGISTRY",
    "POST_GAME_PIPELINE_NAMES",
    "NOTIFICATION_PIPELINE_REGISTRY",
    "get_pipeline",
    "run_pipeline",
    "run_all_pipelines",
    "list_pipelines",
]
