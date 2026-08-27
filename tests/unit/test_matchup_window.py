"""
The live-overlay decision, as a table.

Every row is a moment that used to be reasoned about in comments (and, in the
cases marked NEW, one that the old wall-clock rule got wrong). The function
under test has no clock and no database, so each row is a pure assertion.

Calendar: 2025-26, opening night 2025-10-21 (tests/conftest.py pins
NBA_SEASON=2025-26). Day 135 = 2026-03-04, day 154 = 2026-03-23.
"""

from datetime import date, timedelta

import pytest

from services.matchup_window import (
    DayWatermark,
    WatermarkSource,
    resolve_matchup_window,
)

OPENING_NIGHT = date(2025, 10, 21)


def day_to_date(day: int) -> date:
    """Mirror of schedule_service.date_for_espn_scoring_period for 2025-26."""
    return OPENING_NIGHT + timedelta(days=day - 1)


def exploding_day_to_date(day: int) -> date:
    raise FileNotFoundError("no calendar shipped for this season")


def prov(day):
    return DayWatermark(day, WatermarkSource.PROVIDER)


def cal(day):
    return DayWatermark(day, WatermarkSource.CALENDAR)


UNKNOWN = DayWatermark.unknown()

# A regular week, and the two-week playoff round starting day 154.
WEEK = (date(2026, 3, 2), date(2026, 3, 8))
PLAYOFF = (date(2026, 3, 23), date(2026, 4, 5))
OPENING_WEEK = (OPENING_NIGHT, date(2025, 10, 26))
ALL_STAR = (date(2026, 2, 16), date(2026, 3, 1))
FINAL_WEEK = (date(2026, 4, 6), date(2026, 4, 12))

# (id, provider, baseline, period, expected include_live, overlay, stale, reason)
CASES = [
    (
        "steady state, games in progress",
        prov(135), prov(135), WEEK,
        True, date(2026, 3, 4), 0, "current",
    ),
    (
        # 2:15 AM ET: ESPN flipped and materialized through 03-04, our pipeline
        # has not run. The overlay must stay on 03-04 -- it follows the baseline,
        # not the provider. This is the window last season's bugs lived in.
        "provider flipped, pipeline has not run",
        prov(136), prov(135), WEEK,
        True, date(2026, 3, 4), 0, "current",
    ),
    (
        # 3:30 AM ET, pipeline has written B=136. The overlay moves with it, so
        # 03-04 can never be counted twice. 03-05 has no games yet -> empty.
        "after the pipeline run, overlay advances",
        prov(136), prov(136), WEEK,
        True, date(2026, 3, 5), 0, "current",
    ),
    (
        # NEW: today this returns include_live=False and the score sits flat all
        # evening, because the post-game pipeline has never run before opening
        # night (nba.games holds no preseason rows).
        "opening night, no stored row, first day of the period",
        prov(1), None, OPENING_WEEK,
        True, OPENING_NIGHT, 0, "seeded_period_start",
    ),
    (
        # Mid-period with no row means the snapshot was lost. Seeding zeros here
        # would show 0 rather than a nearly-right number.
        "no stored row, mid-period",
        prov(138), None, WEEK,
        False, None, 5, "no_baseline",
    ),
    (
        # The live rows for day 136 were deleted by the live pipeline, so the
        # score is knowably short by a day. Overlay what we can, flag the rest.
        "pipeline missed a day",
        prov(137), prov(135), WEEK,
        True, date(2026, 3, 4), 1, "stale_baseline",
    ),
    (
        "baseline ahead of the provider",
        prov(135), prov(136), WEEK,
        False, None, 0, "baseline_ahead",
    ),
    (
        # An ESPN payload with no status block. The baseline alone is enough to
        # place the overlay; only staleness is unmeasurable.
        "provider watermark missing",
        UNKNOWN, prov(135), WEEK,
        True, date(2026, 3, 4), 0, "current",
    ),
    (
        # Written before the watermark column was populated. We cannot know what
        # it covers, so prefer a slightly stale score to a double-counted one.
        "legacy row with no watermark",
        prov(135), UNKNOWN, WEEK,
        False, None, 0, "legacy_date_rule",
    ),
    (
        "two-week playoff period",
        prov(160), prov(160), PLAYOFF,
        True, date(2026, 3, 29), 0, "current",
    ),
    (
        "playoff period rollover, first day",
        prov(154), None, PLAYOFF,
        True, date(2026, 3, 23), 0, "seeded_period_start",
    ),
    (
        # Day 122 = 2026-02-19, inside the merged 14-day All-Star week. No week
        # arithmetic is involved anywhere, so the break needs no special case.
        "All-Star break day inside a merged week",
        prov(122), prov(122), ALL_STAR,
        True, date(2026, 2, 19), 0, "current",
    ),
    (
        "overlay day falls past the period end",
        prov(137), prov(137), (date(2026, 3, 2), date(2026, 3, 5)),
        False, None, 0, "outside_period",
    ),
    (
        # Yahoo in the offseason: season_day() returns None. Nothing may crash,
        # and staleness is simply unmeasurable.
        "offseason, provider watermark unusable",
        DayWatermark(None, WatermarkSource.CALENDAR), cal(174), FINAL_WEEK,
        True, date(2026, 4, 12), 0, "current",
    ),
    (
        # ESPN reports latestScoringPeriod=0 in the preseason.
        "preseason, provider reports day zero",
        prov(0), None, OPENING_WEEK,
        False, None, 0, "no_baseline",
    ),
]


@pytest.mark.unit
class TestResolveMatchupWindow:
    @pytest.mark.parametrize(
        "provider,baseline,period,include_live,overlay,stale,reason",
        [pytest.param(*c[1:], id=c[0]) for c in CASES],
    )
    def test_table(self, provider, baseline, period, include_live, overlay, stale, reason):
        window = resolve_matchup_window(
            provider=provider,
            baseline=baseline,
            period_start=period[0],
            period_end=period[1],
            day_to_date=day_to_date,
            fallback_today=date(2026, 3, 4),
        )
        assert window.include_live is include_live
        assert window.overlay_date == overlay
        assert window.stale_days == stale
        assert window.reason == reason

    def test_overlay_always_follows_the_baseline_never_the_provider(self):
        """The invariant the whole design rests on.

        Whenever an overlay happens, its date is the baseline watermark's own
        day -- so the moment the pipeline writes B+1 the overlay moves with it
        and the day just absorbed cannot be counted twice. A seeded period start
        is the one case with no stored baseline, and there the provider's day is
        the period's first day, so the two coincide.
        """
        for _id, provider, baseline, period, include_live, overlay, _stale, _reason in CASES:
            if not include_live:
                continue
            window = resolve_matchup_window(
                provider=provider, baseline=baseline,
                period_start=period[0], period_end=period[1],
                day_to_date=day_to_date, fallback_today=date(2026, 3, 4),
            )
            expected_day = provider.day if window.seed_zero_baseline else baseline.day
            assert window.overlay_date == day_to_date(expected_day), _id

    def test_missing_period_bounds(self):
        window = resolve_matchup_window(
            provider=prov(135), baseline=prov(135),
            period_start=None, period_end=None,
            day_to_date=day_to_date, fallback_today=date(2026, 3, 4),
        )
        assert window.include_live is False
        assert window.reason == "no_period_bounds"
        assert window.display_date == date(2026, 3, 4)

    def test_unmappable_watermark_degrades_rather_than_raising(self):
        """The production mapper raises when a season's calendar is missing."""
        window = resolve_matchup_window(
            provider=prov(135), baseline=prov(135),
            period_start=WEEK[0], period_end=WEEK[1],
            day_to_date=exploding_day_to_date, fallback_today=date(2026, 3, 4),
        )
        assert window.include_live is False
        assert window.reason == "legacy_date_rule"

    def test_seeded_period_start_signals_zero_baseline(self):
        window = resolve_matchup_window(
            provider=prov(1), baseline=None,
            period_start=OPENING_WEEK[0], period_end=OPENING_WEEK[1],
            day_to_date=day_to_date, fallback_today=OPENING_NIGHT,
        )
        assert window.seed_zero_baseline is True

    def test_normal_path_does_not_seed(self):
        window = resolve_matchup_window(
            provider=prov(135), baseline=prov(135),
            period_start=WEEK[0], period_end=WEEK[1],
            day_to_date=day_to_date, fallback_today=date(2026, 3, 4),
        )
        assert window.seed_zero_baseline is False

    def test_display_date_follows_the_overlay_when_live(self):
        window = resolve_matchup_window(
            provider=prov(136), baseline=prov(136),
            period_start=WEEK[0], period_end=WEEK[1],
            day_to_date=day_to_date, fallback_today=date(2026, 3, 4),
        )
        assert window.display_date == window.overlay_date == date(2026, 3, 5)

    def test_watermark_usability(self):
        assert prov(1).usable is True
        assert prov(0).usable is False
        assert DayWatermark(None, WatermarkSource.CALENDAR).usable is False
        assert DayWatermark.unknown().usable is False


# --------------------------------------------------------------------------
# The seam between the pure function and the database rows.
# --------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from freezegun import freeze_time  # noqa: E402

from services.matchup_service import MatchupService  # noqa: E402


def _md(scoring_period_id, source="provider", start="2026-03-02", end="2026-03-08"):
    return SimpleNamespace(
        matchup_period=22,
        scoring_period_id=scoring_period_id,
        scoring_period_source=source,
        matchup_period_start=start,
        matchup_period_end=end,
    )


def _row(snapshot_date, scoring_period_id, source="provider"):
    return SimpleNamespace(
        date=snapshot_date,
        scoring_period_id=scoring_period_id,
        scoring_period_source=source,
        current_score=100.0,
        opponent_current_score=90.0,
    )


@pytest.mark.unit
class TestServiceWiring:
    def test_watermark_window_reads_the_stored_columns(self):
        window = MatchupService._watermark_window(
            _md(136), _row(date(2026, 3, 5), 135)
        )
        assert window.include_live is True
        assert window.overlay_date == date(2026, 3, 4)
        assert window.reason == "current"

    def test_unrecognised_source_degrades_to_unknown(self):
        """A value written by some future writer must not raise."""
        assert MatchupService._watermark_source("something-new") is WatermarkSource.UNKNOWN
        assert MatchupService._watermark_source(None) is WatermarkSource.UNKNOWN
        assert MatchupService._watermark_source("calendar") is WatermarkSource.CALENDAR

    @freeze_time("2026-03-04T20:00:00Z")  # 3 PM ET, mid-afternoon on 03-04
    def test_legacy_window_reproduces_the_old_rule(self):
        """Shadow comparison is only meaningful if this is a faithful copy."""
        window = MatchupService._legacy_window(_md(135), _row(date(2026, 3, 4), 135))
        assert window.include_live is True
        assert window.overlay_date == date(2026, 3, 4)
        assert window.seed_zero_baseline is False

    @freeze_time("2026-03-05T08:00:00Z")  # 3:00 AM EST on 03-05
    def test_late_pipeline_run_drops_a_day_under_the_old_rule(self):
        """The failure this change exists to remove.

        The pipeline ran at 1:30 AM ET -- before ESPN's ~2 AM batch -- so it
        captured totals through 03-03 and stamped watermark 135. Because
        `date` is a US/Central label with no cutoff, it rolled to 03-05 while
        the reader's 6 AM ET "today" is still 03-04.

        Old rule: baseline.date (03-05) > today (03-04) -> no overlay, so
        03-04's games vanish from the score entirely.
        New rule: the baseline's own watermark says it covers through 03-03,
        so 03-04 is exactly the day to overlay.
        """
        md = _md(136)                                # ESPN has batched: 136
        row = _row(date(2026, 3, 5), 135)            # our snapshot: pre-batch

        legacy = MatchupService._legacy_window(md, row)
        assert legacy.include_live is False, "reproduces the bug"
        assert legacy.display_date == date(2026, 3, 5)

        window = MatchupService._watermark_window(md, row)
        assert window.include_live is True
        assert window.overlay_date == date(2026, 3, 4), "the day the baseline is missing"
        assert window.stale_days == 0

    @freeze_time("2026-03-05T08:00:00Z")
    def test_the_two_rules_disagree_here_so_shadow_mode_logs_it(self):
        md, row = _md(136), _row(date(2026, 3, 5), 135)
        legacy = MatchupService._legacy_window(md, row)
        window = MatchupService._watermark_window(md, row)
        assert (window.include_live, window.overlay_date) != (legacy.include_live, legacy.overlay_date)
