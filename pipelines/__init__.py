"""
Pipeline Registry and Exports

Provides a registry of all available pipelines and helper functions
for running them by name.
"""

from typing import Type

from core.logging import get_logger
from pipelines.base import BasePipeline
from pipelines.config import PipelineConfig
from pipelines.context import PipelineContext
from pipelines.daily_player_stats import DailyPlayerStatsPipeline
from pipelines.cumulative_player_stats import CumulativePlayerStatsPipeline
from pipelines.daily_matchup_scores import DailyMatchupScoresPipeline
from pipelines.advanced_stats import AdvancedStatsPipeline
from pipelines.player_profiles import PlayerProfilesPipeline
from pipelines.game_schedule import GameSchedulePipeline
from pipelines.injury_report import InjuryReportPipeline
from schemas.pipeline import PipelineResult
from schemas.common import ApiStatus


# Registry of all available pipelines
# Order matters for run_all_pipelines - dependencies should come first
PIPELINE_REGISTRY: dict[str, Type[BasePipeline]] = {
    # Core daily pipelines
    "daily_player_stats": DailyPlayerStatsPipeline,
    "cumulative_player_stats": CumulativePlayerStatsPipeline,
    "daily_matchup_scores": DailyMatchupScoresPipeline,
    # Extended data pipelines
    "advanced_stats": AdvancedStatsPipeline,
    "game_schedule": GameSchedulePipeline,
    # "injury_report": InjuryReportPipeline, -- requires BALLDONTLIE All-Star tier subscription
    # Reference data pipelines (run less frequently)
    "player_profiles": PlayerProfilesPipeline,
}


def get_pipeline(name: str) -> BasePipeline:
    """
    Get a pipeline instance by name.

    Args:
        name: Pipeline name (e.g., "daily_player_stats")

    Returns:
        Instantiated pipeline

    Raises:
        KeyError: If pipeline name not found
    """
    if name not in PIPELINE_REGISTRY:
        available = ", ".join(PIPELINE_REGISTRY.keys())
        raise KeyError(f"Unknown pipeline '{name}'. Available: {available}")

    return PIPELINE_REGISTRY[name]()


async def run_pipeline(name: str) -> PipelineResult:
    """
    Run a pipeline by name.

    Args:
        name: Pipeline name

    Returns:
        PipelineResult with status and details
    """
    pipeline = get_pipeline(name)
    return await pipeline.run()


async def run_all_pipelines() -> dict[str, PipelineResult]:
    """
    Run all pipelines in sequence.

    Pipelines are run in registration order:
    1. daily_player_stats - Per-game box scores
    2. cumulative_player_stats - Season totals
    3. daily_matchup_scores - Fantasy matchup tracking
    4. advanced_stats - Efficiency/usage metrics
    5. game_schedule - NBA game results
    6. injury_report - Player injury status
    7. player_profiles - Biographical data (slow, run weekly)

    Returns:
        Dict mapping pipeline name to PipelineResult
    """
    log = get_logger("pipeline").bind(operation="run_all")

    results = {}
    pipeline_names = list(PIPELINE_REGISTRY.keys())

    log.info("all_pipelines_started", count=len(pipeline_names))

    for i, name in enumerate(pipeline_names, 1):
        log.info("running_pipeline", pipeline=name, step=f"{i}/{len(pipeline_names)}")
        results[name] = await run_pipeline(name)

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
    "DailyPlayerStatsPipeline",
    "CumulativePlayerStatsPipeline",
    "DailyMatchupScoresPipeline",
    # Extended data pipelines
    "AdvancedStatsPipeline",
    "PlayerProfilesPipeline",
    "GameSchedulePipeline",
    "InjuryReportPipeline",
    # Registry functions
    "PIPELINE_REGISTRY",
    "get_pipeline",
    "run_pipeline",
    "run_all_pipelines",
    "list_pipelines",
]
