"""Mathematical and temporal invariants for the isolated research harness.

Run from backend with unittest to avoid the application/DB pytest conftest:
  .venv/bin/python -m unittest discover -s experiments/ranking_engine -p 'test_*.py' -v
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

import numpy as np

from engine import (RAW, IX, CATS, MinuteEvent, adjust_minutes, best_lineup,
                    category_moments, category_values, exact_week_results,
                    independent_match_value, matchup_probabilities)


def line(**kwargs):
    return np.array([kwargs.get(k, 0.) for k in RAW])


class CategoryTests(unittest.TestCase):
    def test_aggregate_percentage_uses_volume(self):
        a, b = line(fgm=1, fga=1), line(fgm=0, fga=9)
        self.assertAlmostEqual(category_values(a + b)[0], .1)

    def test_makes_attempt_covariance_cancels_fixed_ratio_noise(self):
        mean = line(fgm=5, fga=10)
        direction = line(fgm=1, fga=2)
        _, var = category_moments(mean, np.outer(direction, direction))
        self.assertAlmostEqual(var[0], 0.)

    def test_rate_variance_invariant_to_same_random_variable_rescaling(self):
        mean = line(fgm=5, fga=10, ftm=3, fta=4)
        cov = np.eye(len(RAW))
        a, av = category_moments(mean, cov)
        b, bv = category_moments(2 * mean, 4 * cov)
        np.testing.assert_allclose(a[:2], b[:2])
        np.testing.assert_allclose(av[:2], bv[:2])

    def test_lower_turnovers_wins(self):
        zero = np.zeros((len(RAW), len(RAW)))
        p = matchup_probabilities(line(tov=1), zero, line(tov=4), zero)
        self.assertEqual(p[CATS.index("tov")], 1.)

    def test_deterministic_ties_have_half_category_credit(self):
        zero = np.zeros((len(RAW), len(RAW)))
        p = matchup_probabilities(line(), zero, line(), zero)
        np.testing.assert_array_equal(p, np.full(9, .5))

    def test_actual_tied_categories_not_counted_as_losses(self):
        a, b = line(pts=10, tov=2), line(pts=8, tov=2)
        cats, match = exact_week_results(a, b)
        self.assertEqual(cats, 5.)
        self.assertEqual(match, 1.)

    def test_actual_full_tie(self):
        cats, match = exact_week_results(line(), line())
        self.assertEqual(cats, 4.5)
        self.assertEqual(match, .5)

    def test_more_points_monotonic_category_probability(self):
        cov = np.eye(len(RAW))
        a = matchup_probabilities(line(pts=9), cov, line(pts=10), cov)
        b = matchup_probabilities(line(pts=11), cov, line(pts=10), cov)
        self.assertGreater(b[CATS.index("pts")], a[CATS.index("pts")])

    def test_poisson_binomial_matches_three_category_hand_calculation(self):
        p = np.array([.2, .5, .8])
        # Exactly 2 or 3 wins.
        expected = .2*.5*.2 + .2*.5*.8 + .8*.5*.8 + .2*.5*.8
        self.assertAlmostEqual(float(independent_match_value(p)), expected)

    def test_even_category_match_tie(self):
        self.assertEqual(float(independent_match_value(np.array([1., 0.]))), .5)

    def test_nine_fair_independent_categories(self):
        self.assertAlmostEqual(float(independent_match_value(np.full(9, .5))), .5)


class PointsTests(unittest.TestCase):
    def test_one_player_cannot_fill_two_slots(self):
        self.assertEqual(best_lineup([(50., {"PG", "C"})], ["PG", "C"]), 50.)

    def test_assignment_moves_flexible_player(self):
        self.assertEqual(best_lineup([(50., {"PG", "C"}), (40., {"PG"})], ["PG", "C"]), 90.)

    def test_negative_player_can_be_benched(self):
        self.assertEqual(best_lineup([(-2., {"C"})], ["C"]), 0.)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.t = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
        self.e = MinuteEvent("1", "role", self.t, self.t, self.t, self.t + timedelta(days=10), 8., .8)

    def test_duplicate_evidence_is_not_added_twice(self):
        self.assertAlmostEqual(adjust_minutes(22, [self.e, self.e], self.t), 28.4)

    def test_late_ingestion_unavailable_to_historical_cutoff(self):
        e = replace(self.e, observed_at=self.t + timedelta(hours=2))
        self.assertEqual(adjust_minutes(22, [e], self.t), 22.)

    def test_future_effect_not_active(self):
        e = replace(self.e, effective_at=self.t + timedelta(days=1))
        self.assertEqual(adjust_minutes(22, [e], self.t), 22.)

    def test_retraction_replaces_prior_even_after_retraction_expires(self):
        e = replace(self.e, event_id="2", published_at=self.t + timedelta(hours=1),
                    observed_at=self.t + timedelta(hours=1), delta=0., valid_until=self.t + timedelta(days=1))
        self.assertEqual(adjust_minutes(22, [self.e, e], self.t + timedelta(days=2)), 22.)

    def test_absorbed_event_not_reapplied(self):
        self.assertEqual(adjust_minutes(28.4, [self.e], self.t, absorbed_clusters={"role"}), 28.4)

    def test_structural_event_does_not_decay(self):
        self.assertAlmostEqual(adjust_minutes(22, [self.e], self.t + timedelta(days=5)), 28.4)

    def test_rumor_decays_once_from_publication(self):
        e = replace(self.e, half_life_hours=24.)
        self.assertAlmostEqual(adjust_minutes(22, [e], self.t + timedelta(days=1)), 25.2)

    def test_minutes_bounded(self):
        self.assertEqual(adjust_minutes(45, [self.e], self.t), 48.)

    def test_naive_timestamps_rejected(self):
        with self.assertRaises(ValueError):
            replace(self.e, observed_at=self.t.replace(tzinfo=None))


class TemporalBenchmarkTests(unittest.TestCase):
    def test_future_changes_do_not_change_draft_inputs(self):
        from run_experiments import make_draft_inputs
        ids = np.repeat([1, 2, 3], 30)
        dates = np.tile(np.arange(np.datetime64("2025-12-01"), np.datetime64("2025-12-31")), 3)
        x = np.tile(line(pts=15, fgm=5, fga=10, ftm=4, fta=5, min=25), (90, 1))
        x[:, IX["pts"]] += ids
        cutoff = np.datetime64("2025-12-22")
        a = make_draft_inputs(ids, dates, x, {1: "A", 2: "B", 3: "C"}, cutoff)
        x[dates >= cutoff] *= 100
        b = make_draft_inputs(ids, dates, x, {1: "A", 2: "B", 3: "C"}, cutoff)
        for key in ("ids", "z", "g", "mean", "cov", "weekly_z"):
            np.testing.assert_array_equal(a[key], b[key])
        self.assertFalse(np.array_equal(a["test"], b["test"]))


if __name__ == "__main__":
    unittest.main()
