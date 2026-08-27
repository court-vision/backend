"""
Which day of live stats belongs on top of the stored matchup baseline.

The live matchup score is `baseline + overlay`, where the baseline is the
provider's materialized `totalPoints` snapshotted into
`stats_s2.daily_matchup_scores` and the overlay is one day of
`nba.live_player_stats`.

The whole decision rests on one invariant:

    A baseline captured at watermark B covers through day B-1.
    Therefore the day to overlay is B itself.

The overlay day comes from the *baseline's* own stored watermark, never from
the provider's current one. Two things follow:

- The overlay is exactly one day, by construction.
- Double-counting is impossible: when the pipeline writes B+1 the overlay day
  moves to B+1 in the same step, so the day just absorbed can never be added
  twice.

A watermark is the 1-based day of the regular season (1 = opening night) --
the same integer space as ESPN's `status.latestScoringPeriod` and as
`services.schedule_service.season_day`. The snapshot's `date` column plays no
part in this decision, which is why it is not a parameter.

The provider's watermark is used only to measure staleness (how many days the
baseline is missing) and to recognise the first day of a matchup period.

This module is pure: no DB, no clock, no calendar files. `day_to_date` is
injected so it stays table-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Callable, Optional


class WatermarkSource(str, Enum):
    """Where a watermark came from. Kept explicit so a provider-reported day is
    never silently conflated with one we derived ourselves."""

    PROVIDER = "provider"   # ESPN status.latestScoringPeriod
    CALENDAR = "calendar"   # derived from our season calendar (Yahoo, seeded periods)
    UNKNOWN = "unknown"     # absent, legacy row, or outside the regular season


@dataclass(frozen=True)
class DayWatermark:
    """The first season day NOT yet included in a score."""

    day: Optional[int]
    source: WatermarkSource = WatermarkSource.UNKNOWN

    @property
    def usable(self) -> bool:
        # ESPN reports 0 in the preseason; season_day() returns None outside the
        # regular season. Neither can be mapped to a game date.
        return self.day is not None and self.day >= 1

    @classmethod
    def unknown(cls) -> "DayWatermark":
        return cls(None, WatermarkSource.UNKNOWN)


@dataclass(frozen=True)
class MatchupWindow:
    include_live: bool
    overlay_date: Optional[date]   # exactly one day of live stats, or None
    display_date: date             # -> LiveMatchupData.game_date
    seed_zero_baseline: bool       # no stored row, and we are on the period's first day
    stale_days: int                # days missing from the baseline; 0 when fresh
    reason: str                    # stable slug for logs and test assertions


def _to_date(day_to_date: Callable[[int], date], day: Optional[int]) -> Optional[date]:
    """Map a watermark to a calendar date, or None when it cannot be mapped.

    The production mapper reads the season calendar and raises when the file for
    the season is missing, so a failure here has to degrade rather than 500.
    """
    if day is None:
        return None
    try:
        return day_to_date(day)
    except Exception:
        return None


def resolve_matchup_window(
    *,
    provider: DayWatermark,
    baseline: Optional[DayWatermark],
    period_start: Optional[date],
    period_end: Optional[date],
    day_to_date: Callable[[int], date],
    fallback_today: date,
) -> MatchupWindow:
    """Resolve which day to overlay, and whether to overlay at all.

    Args:
        provider: the provider's current watermark (ESPN's latestScoringPeriod,
            or a calendar-derived day for Yahoo). Used for staleness and for
            recognising a period's first day -- never to pick the overlay day.
        baseline: the watermark stored on the newest snapshot for this matchup
            period, or None when no row exists yet.
        period_start / period_end: the matchup period's date range.
        day_to_date: watermark -> calendar date.
        fallback_today: display date when no watermark can be mapped.
    """
    # The overlay only makes sense inside a known matchup period.
    if period_start is None or period_end is None:
        return MatchupWindow(
            include_live=False,
            overlay_date=None,
            display_date=fallback_today,
            seed_zero_baseline=False,
            stale_days=0,
            reason="no_period_bounds",
        )

    provider_date = _to_date(day_to_date, provider.day) if provider.usable else None

    # No stored row. On the first day of a period that is not missing data --
    # H2H scores reset each period, so "covers through day 0" is exactly zero.
    # Mid-period it means the snapshot was genuinely lost, and seeding zeros
    # would show 0 instead of a nearly-right number.
    if baseline is None:
        if provider.usable and provider_date == period_start:
            effective, seed, reason = provider, True, "seeded_period_start"
        else:
            missing = (provider_date - period_start).days if provider_date else 0
            return MatchupWindow(
                include_live=False,
                overlay_date=None,
                display_date=provider_date or fallback_today,
                seed_zero_baseline=False,
                stale_days=max(0, missing),
                reason="no_baseline",
            )
    elif not baseline.usable:
        # A row written before the watermark existed, or one whose provider
        # payload carried no scoring period. We cannot know what it covers, so
        # overlaying would be a guess -- prefer a slightly stale score to a
        # double-counted one.
        return MatchupWindow(
            include_live=False,
            overlay_date=None,
            display_date=fallback_today,
            seed_zero_baseline=False,
            stale_days=0,
            reason="legacy_date_rule",
        )
    else:
        effective, seed, reason = baseline, False, "current"

    # The provider is behind the snapshot: clock skew, a provider-side
    # regression, or a snapshot written from a different season. Do not overlay.
    if provider.usable and effective.usable and provider.day < effective.day:
        return MatchupWindow(
            include_live=False,
            overlay_date=None,
            display_date=_to_date(day_to_date, effective.day) or fallback_today,
            seed_zero_baseline=False,
            stale_days=0,
            reason="baseline_ahead",
        )

    overlay_date = _to_date(day_to_date, effective.day)
    if overlay_date is None:
        return MatchupWindow(
            include_live=False,
            overlay_date=None,
            display_date=fallback_today,
            seed_zero_baseline=False,
            stale_days=0,
            reason="legacy_date_rule",
        )

    # Days between the baseline's day and the provider's are unrecoverable: the
    # live pipeline deletes rows for days before the one it is writing, so the
    # score is knowably too low. Surface it rather than show it silently.
    stale_days = 0
    if provider.usable and not seed:
        stale_days = max(0, provider.day - effective.day - 1)

    if not (period_start <= overlay_date <= period_end):
        return MatchupWindow(
            include_live=False,
            overlay_date=None,
            display_date=overlay_date,
            seed_zero_baseline=False,
            stale_days=stale_days,
            reason="outside_period",
        )

    return MatchupWindow(
        include_live=True,
        overlay_date=overlay_date,
        display_date=overlay_date,
        seed_zero_baseline=seed,
        stale_days=stale_days,
        reason="stale_baseline" if stale_days else reason,
    )
