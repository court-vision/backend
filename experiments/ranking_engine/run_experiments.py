"""Reproducible historical ablations and explicitly synthetic diagnostics.

From backend: .venv/bin/python experiments/ranking_engine/run_experiments.py
Requires the ignored read-only history export for the historical experiments.
"""
import argparse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from services.scoring.category_rank import PoolRow, compute_category_scores
from services.scoring.models import CategoryDef, StatLine
from services.scoring.points import DEFAULT_POINTS
from services.draft_fit import build_fit_model
from engine import (RAW, IX, CATS, MinuteEvent, adjust_minutes, best_lineup, cdf,
                    exact_week_results, impact_features, independent_match_value,
                    matchup_probabilities)

CATEGORY_DEFS = [CategoryDef.for_key(k) for k in CATS]
WEIGHTS = np.array([DEFAULT_POINTS.weights.get(k, 0) for k in RAW])
METHODS = ("season", "last_7_days", "ewma_6_games", "shrunk_recent", "minutes_x_rate")


def provenance():
    paths = [HERE / p for p in ("engine.py", "run_experiments.py", "test_engine.py")]
    paths += [HERE.parents[1] / "services" / p for p in
              ("scoring/category_rank.py", "scoring/points.py", "draft_fit.py")]
    return {"python": sys.version, "numpy": np.__version__,
            "source_sha256": {str(p.relative_to(HERE.parents[1])): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in paths}}


def load_history():
    blob = gzip.decompress((HERE / "data/history.json.gz").read_bytes())
    manifest = json.loads((HERE / "data/manifest.json").read_text())
    assert hashlib.sha256(blob).hexdigest() == manifest["sha256_uncompressed"]
    obj = json.loads(blob)
    rows = obj["rows"]
    assert obj["columns"][3:] == list(RAW)
    ids = np.array([r[0] for r in rows], dtype=int)
    dates = np.array([r[2] for r in rows], dtype="datetime64[D]")
    x = np.array([r[3:] for r in rows], dtype=float)
    assert np.isfinite(x).all() and (x >= 0).all()
    for m, a in (("fgm", "fga"), ("ftm", "fta"), ("fg3m", "fg3a")):
        assert (x[:, IX[m]] <= x[:, IX[a]]).all()
    assert len(set(zip(ids.tolist(), dates.astype(str).tolist()))) == len(rows)
    names = {r[0]: r[1] for r in rows}
    return ids, dates, x, names, manifest


def forecast(train, dates, cutoff):
    season = train.mean(axis=0)
    recent = train[-6:]
    n = len(recent)
    ew = 2 ** (-np.arange(len(train) - 1, -1, -1) / 6.)
    ew /= ew.sum()
    last7 = train[dates >= cutoff - np.timedelta64(7, "D")]
    # Fixed, untuned prior strength: 12 pseudo-games. No holdout data used.
    shrunk = (recent.sum(axis=0) + 12 * season) / (n + 12)
    minutes = float(ew @ train[:, IX["min"]])
    rate = train.sum(axis=0) / max(train[:, IX["min"]].sum(), 1)
    minutes_rate = rate * minutes
    return {"season": season, "last_7_days": last7.mean(axis=0) if len(last7) else season,
            "ewma_6_games": ew @ train, "shrunk_recent": shrunk, "minutes_x_rate": minutes_rate}


def projection_benchmark(ids, dates, x):
    # Non-overlapping 14-day targets. Eligibility depends ONLY on training data.
    cutoffs = np.array(["2026-01-12", "2026-01-26", "2026-02-09", "2026-02-23", "2026-03-09", "2026-03-23"], dtype="datetime64[D]")
    errors = {k: [] for k in METHODS}
    folds, omitted = [], 0
    for cutoff in cutoffs:
        fold = {k: [] for k in METHODS}
        for pid in np.unique(ids):
            mask = (ids == pid) & (dates < cutoff)
            train, td = x[mask], dates[mask]
            if len(train) < 15 or not len(td) or td.max() < cutoff - np.timedelta64(14, "D"):
                continue
            target = x[(ids == pid) & (dates >= cutoff) & (dates < cutoff + np.timedelta64(14, "D"))]
            if not len(target):
                omitted += 1  # Conditional production experiment; NOT an availability evaluation.
                continue
            actual = target.mean(axis=0)
            for method, prediction in forecast(train, td, cutoff).items():
                err = prediction - actual
                record = {"player_id": int(pid), "cutoff": str(cutoff),
                          "points_abs_error": float(abs(err @ WEIGHTS)),
                          "minutes_abs_error": float(abs(err[IX["min"]])),
                          "raw_abs_error": np.abs(err).tolist()}
                errors[method].append(record)
                fold[method].append(record["points_abs_error"])
        folds.append({"cutoff": str(cutoff), "player_windows": len(fold["season"]),
                      "points_mae": {k: float(np.mean(v)) for k, v in fold.items()}})
    # Paired bootstrap clustered by player across all cutoffs. Exploratory,
    # same-season estimates; correlated calendar shocks are not resampled.
    players = np.array(sorted({r["player_id"] for r in errors["season"]}))
    index = {pid: i for i, pid in enumerate(players)}
    rng = np.random.default_rng(92026)
    draws = rng.integers(len(players), size=(1500, len(players)))
    summaries = {}
    base = np.array([r["points_abs_error"] for r in errors["season"]])
    for method, records in errors.items():
        vals = np.array([r["points_abs_error"] for r in records])
        sums, counts = np.zeros(len(players)), np.zeros(len(players))
        for r, delta in zip(records, vals - base):
            i = index[r["player_id"]]
            sums[i] += delta
            counts[i] += 1
        boot = sums[draws].sum(axis=1) / counts[draws].sum(axis=1)
        summaries[method] = {
            "points_mae": float(vals.mean()),
            "minutes_mae": float(np.mean([r["minutes_abs_error"] for r in records])),
            "points_mae_delta_vs_season": float((vals - base).mean()),
            "delta_95pct_player_bootstrap": np.quantile(boot, [.025, .975]).tolist(),
            "raw_stat_mae": dict(zip(RAW, np.mean([r["raw_abs_error"] for r in records], axis=0).tolist())),
        }
    return {"design": "Next 14 days per played game; final box scores; no historical news/projection inputs; fixed hyperparameters.",
            "players": len(players), "player_windows": len(errors["season"]),
            "zero_game_targets_excluded": omitted, "methods": summaries, "folds": folds}


def production_z(ids, names, pergame):
    rows = [PoolRow(int(pid), names[int(pid)], None, 30, StatLine.from_dict(dict(zip(RAW, line))),
                    float(line @ WEIGHTS), 0.) for pid, line in zip(ids, pergame)]
    scored = compute_category_scores(rows, CATEGORY_DEFS)
    by_id = {s.row.id: s for s in scored}
    z = np.array([[by_id[int(pid)].z[k] for k in CATS] for pid in ids])
    balanced = np.array([by_id[int(pid)].score for pid in ids])
    return z, balanced


def make_draft_inputs(ids, dates, x, names, cutoff):
    # Train up to (not including) cutoff. Candidate selection has no future screen.
    eligible, pergame = [], []
    for pid in np.unique(ids):
        train = x[(ids == pid) & (dates < cutoff)]
        if len(train) >= 15:
            eligible.append(pid)
            pergame.append(train.mean(axis=0))
    eligible, pergame = np.array(eligible), np.array(pergame)
    _, full_z = production_z(eligible, names, pergame)
    chosen = np.argsort(-full_z, kind="stable")[:220]
    eligible, pergame = eligible[chosen], pergame[chosen]
    z, balanced = production_z(eligible, names, pergame)
    # Trailing eight complete Monday-Sunday weeks; missing player-weeks are ZERO.
    weekly = np.zeros((len(eligible), 8, len(RAW)))
    test = np.zeros((len(eligible), 4, len(RAW)))
    for i, pid in enumerate(eligible):
        for w in range(8):
            start = cutoff - np.timedelta64((8 - w) * 7, "D")
            weekly[i, w] = x[(ids == pid) & (dates >= start) & (dates < start + np.timedelta64(7, "D"))].sum(axis=0)
        for w in range(4):
            start = cutoff + np.timedelta64(w * 7, "D")
            test[i, w] = x[(ids == pid) & (dates >= start) & (dates < start + np.timedelta64(7, "D"))].sum(axis=0)
    means = weekly.mean(axis=1)
    covariance = np.array([np.cov(w, rowvar=False, ddof=1) for w in weekly])
    # Stable shrinkage of sparse per-player covariance toward shared residuals.
    covariance = .5 * covariance + .5 * covariance.mean(axis=0)
    # G-inspired *extra-makes* version, not a reproduction of the paper's rate formula.
    tier = np.argsort(-balanced)[:156]
    features = impact_features(means, means[tier])
    weekly_features = impact_features(weekly, means[tier])
    between = features[tier].var(axis=0)
    within = weekly_features[tier].var(axis=1, ddof=1).mean(axis=0)
    g = (features - features[tier].mean(axis=0)) / np.sqrt(np.maximum(between + within, 1e-9))
    # Hold the mean and pool fixed to isolate the variance-denominator ablation.
    weekly_z = (features - features[tier].mean(axis=0)) / np.sqrt(np.maximum(between, 1e-9))
    return {"ids": eligible, "z": z, "balanced": balanced, "g": g, "weekly_z": weekly_z,
            "mean": means, "cov": covariance, "weekly": weekly, "test": test,
            "g_to_z_weight": np.sqrt(between / np.maximum(between + within, 1e-9))}


def dynamic_scores(data, rosters, hero, available, mode):
    mean, cov = data["mean"], data["cov"]
    # Completion baseline shared for all candidates at this pick. It is a mean,
    # not a sampled player assignment; actual selected players remain unique.
    remaining = sum(13 - len(r) for r in rosters)
    future = sorted(available, key=lambda i: -data["balanced"][i])[:remaining]
    future_mean = mean[future].mean(axis=0)
    future_cov = cov[future].mean(axis=0) + np.cov(mean[future], rowvar=False) if len(future) > 1 else cov[future[0]]
    my_ids = rosters[hero]
    my_mean = mean[my_ids].sum(axis=0) + (12 - len(my_ids)) * future_mean
    my_cov = cov[my_ids].sum(axis=0) + (12 - len(my_ids)) * future_cov
    candidate_mean = my_mean + mean[available]
    candidate_cov = my_cov + cov[available]
    scores = np.zeros(len(available))
    for seat, roster in enumerate(rosters):
        if seat == hero:
            continue
        om = mean[roster].sum(axis=0) + (13 - len(roster)) * future_mean
        oc = cov[roster].sum(axis=0) + (13 - len(roster)) * future_cov
        p = matchup_probabilities(candidate_mean, candidate_cov, om, oc)
        scores += p.sum(axis=-1) if mode == "each" else independent_match_value(p)
    return scores / 11


def draft_once(data, method, hero, seed, field):
    rng = np.random.default_rng(seed)
    # Same opponent preferences for every compared hero policy; choices adapt
    # naturally to available players. Two fields: balanced and category-specialist.
    preferences = np.ones((12, 9))
    if field == "mixed":
        preferences = rng.uniform(.55, 1.45, size=(12, 9))
        for seat in range(12):
            if seat % 3 == 0:
                preferences[seat, rng.integers(9)] = 0
    field_features = data["g"] if field == "volume_aware" else data["z"]
    market = preferences @ field_features.T + rng.normal(0, .35, (12, len(data["ids"])))
    rosters = [[] for _ in range(12)]
    available = list(range(len(data["ids"])))
    ranked_z = [(int(data["ids"][i]), dict(zip(CATS, data["z"][i])))
                for i in np.argsort(-data["balanced"], kind="stable")]
    ranked_g = [(int(data["ids"][i]), dict(zip(CATS, data["g"][i])))
                for i in np.argsort(-data["g"].sum(axis=1), kind="stable")]
    latency = []
    for rnd in range(13):
        for seat in (range(12) if rnd % 2 == 0 else reversed(range(12))):
            if seat != hero:
                chosen = available[int(np.argmax(market[seat, available]))]
            else:
                start = perf_counter()
                if method == "production_z":
                    scores = data["balanced"][available]
                elif method == "weekly_z":
                    scores = data["weekly_z"][available].sum(axis=1)
                elif method == "g_inspired":
                    scores = data["g"][available].sum(axis=1)
                elif method in ("current_fit", "punt_ft_to"):
                    owned = [int(data["ids"][i]) for i in rosters[hero]]
                    fit = build_fit_model(ranked_z, owned, CATEGORY_DEFS, 156,
                                          punts=("ft_pct", "tov") if method == "punt_ft_to" else ())
                    scores = np.array([fit.fit_z(data["balanced"][i], dict(zip(CATS, data["z"][i]))) for i in available])
                elif method == "g_need_fit":
                    owned = [int(data["ids"][i]) for i in rosters[hero]]
                    fit = build_fit_model(ranked_g, owned, CATEGORY_DEFS, 156)
                    scores = np.array([fit.fit_z(data["g"][i].sum(), dict(zip(CATS, data["g"][i]))) for i in available])
                else:
                    scores = dynamic_scores(data, rosters, hero, available, method.removeprefix("marginal_"))
                latency.append(1000 * (perf_counter() - start))
                chosen = available[int(np.argmax(scores))]
            rosters[seat].append(chosen)
            available.remove(chosen)
    assert len({p for r in rosters for p in r}) == 156
    mine = data["test"][rosters[hero]].sum(axis=0)
    cats, matches, normal_brier, joint_brier = [], [], [], []
    for seat, roster in enumerate(rosters):
        if seat != hero:
            a, b = exact_week_results(mine, data["test"][roster].sum(axis=0))
            cats.extend(a)
            matches.extend(b)
            p = matchup_probabilities(data["mean"][rosters[hero]].sum(axis=0),
                                      data["cov"][rosters[hero]].sum(axis=0),
                                      data["mean"][roster].sum(axis=0), data["cov"][roster].sum(axis=0))
            normal = float(independent_match_value(p))
            # Exhaustive empirical calendar-week bootstrap: all eight prior
            # weeks, synchronized across players/teams, exact shooting ratios.
            # Evaluates probability quality only; does not affect draft choices.
            _, joint = exact_week_results(data["weekly"][rosters[hero]].sum(axis=0),
                                           data["weekly"][roster].sum(axis=0))
            # A half-credit tie target is a utility outcome rather than a binary
            # event; name the diagnostic mean squared match-score error.
            normal_brier.extend((normal - b)**2)
            joint_brier.extend((joint.mean() - b)**2)
    return {"each_category_score": float(np.mean(cats)), "match_score": float(np.mean(matches)),
            "normal_match_score_mse": float(np.mean(normal_brier)),
            "calendar_bootstrap_match_score_mse": float(np.mean(joint_brier)),
            "pick_latency_ms": latency}


def draft_benchmark(ids, dates, x, names, quick=False):
    methods = ("production_z", "weekly_z", "g_inspired", "current_fit", "g_need_fit", "punt_ft_to", "marginal_each", "marginal_most")
    runs, weights = [], {}
    # Three disjoint four-week target blocks, all 12 draft seats, three fields.
    for cutoff_text in ("2026-01-12", "2026-02-09", "2026-03-09"):
        data = make_draft_inputs(ids, dates, x, names, np.datetime64(cutoff_text))
        weights[cutoff_text] = dict(zip(CATS, data["g_to_z_weight"].tolist()))
        for field in ("balanced", "mixed", "volume_aware"):
            for hero in (range(0, 12, 4) if quick else range(12)):
                for method in methods:
                    result = draft_once(data, method, hero, 20260905 + hero, field)
                    runs.append({"cutoff": cutoff_text, "field": field, "seat": hero, "method": method, **result})
        print(f"Draft holdout {cutoff_text} complete", flush=True)
    summaries = {}
    for method in methods:
        rows = [r for r in runs if r["method"] == method]
        latency = [v for r in rows for v in r["pick_latency_ms"]]
        summaries[method] = {"drafts": len(rows),
                             "each_category_score": float(np.mean([r["each_category_score"] for r in rows])),
                             "match_score": float(np.mean([r["match_score"] for r in rows])),
                             "normal_match_score_mse": float(np.mean([r["normal_match_score_mse"] for r in rows])),
                             "calendar_bootstrap_match_score_mse": float(np.mean([r["calendar_bootstrap_match_score_mse"] for r in rows])),
                             "pick_latency_p50_ms": float(np.median(latency)),
                             "pick_latency_p95_ms": float(np.quantile(latency, .95))}
    by_fold = []
    for cutoff in weights:
        for field in ("balanced", "mixed", "volume_aware"):
            by_fold.append({"cutoff": cutoff, "field": field, "methods": {
                method: {key: float(np.mean([r[key] for r in runs if r["method"] == method and r["cutoff"] == cutoff and r["field"] == field]))
                         for key in ("each_category_score", "match_score")}
                for method in methods}})
    # Strip timing lists; keep per-draft observations for auditable paired comparisons.
    for r in runs:
        del r["pick_latency_ms"]
    return {"design": "Historical midseason redraft diagnostic; 12 teams x 13 players, top 220 training-only candidates, all games count, 4 held-out weeks per cutoff, 11 opponents per week. No eligibility, streaming, ADP or draft lookahead. Final corrected data, not ingestion-time replay.",
            "seed": 20260905, "quick": quick, "methods": summaries, "folds": by_fold,
            "variance_weights": weights, "runs": runs}


def diagnostics():
    # Two equal-static-value specialists; one attacks an essentially lost category.
    before = np.array([-4., -.1])
    choices = {"deep_deficit_specialist": np.array([1., 0.]), "swing_category_specialist": np.array([0., 1.])}
    swing = {name: float((cdf(before + delta) - cdf(before)).sum()) for name, delta in choices.items()}
    lineup = [(48., {"PG"}), (20., {"C"})]
    base = best_lineup(lineup, ["PG", "C"])
    points = {"guard_44": best_lineup(lineup + [(44., {"PG"})], ["PG", "C"]) - base,
              "center_40": best_lineup(lineup + [(40., {"C"})], ["PG", "C"]) - base}
    rng = np.random.default_rng(13)
    # Synthetic jointly normal category margins, 0.7 pairwise correlation.
    samples = .4 + np.sqrt(.7) * rng.normal(size=(100000, 1)) + np.sqrt(.3) * rng.normal(size=(100000, 9))
    simulated = (samples > 0).sum(axis=1)
    marginals = (samples > 0).mean(axis=0)
    correlated = {"independent_most_estimate": float(independent_match_value(marginals)),
                  "joint_most_estimate": float((simulated >= 5).mean()),
                  "expected_categories_from_marginals": float(marginals.sum()),
                  "expected_categories_from_joint_samples": float(simulated.mean()), "samples": len(samples)}
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    event = MinuteEvent("e1", "starter_absence", now, now, now, now + timedelta(days=14), 8., .8)
    duplicate = replace(event, event_id="syndication", observed_at=now + timedelta(minutes=1))
    later = now + timedelta(hours=1)
    baseline, fppm = 22., 1.1
    news = {"baseline_minutes": baseline, "single_event_minutes": adjust_minutes(baseline, [event], later),
            "duplicate_event_minutes": adjust_minutes(baseline, [event, duplicate], later),
            "before_observed_minutes": adjust_minutes(baseline, [event], now - timedelta(seconds=1)),
            "absorbed_minutes": adjust_minutes(28.4, [event], later, absorbed_clusters={event.cluster}),
            "expired_minutes": adjust_minutes(baseline, [event], now + timedelta(days=15)),
            "points_per_game_delta": (adjust_minutes(baseline, [event], later) - baseline) * fppm,
            "assumed_event_games": 6, "assumed_season_games": 65,
            "season_average_points_delta": (adjust_minutes(baseline, [event], later) - baseline) * fppm * 6 / 65}
    return {"label": "Synthetic mechanism tests, not measured player forecasts or real news.",
            "swing_category_expected_wins_gain": swing, "marginal_startable_points": points,
            "correlation": correlated, "news": news}


def write_summary(results):
    lines = ["# Experiment results", "", "Generated by `run_experiments.py`. See README.md for interpretation and limitations.", ""]
    lines += ["## Forecasts: next 14 days, per game played", "", "| Method | ESPN points MAE | Minutes MAE | MAE change vs season (95% player bootstrap) |", "|---|---:|---:|---:|"]
    for method, r in results["projection"]["methods"].items():
        ci = r["delta_95pct_player_bootstrap"]
        lines.append(f"| {method} | {r['points_mae']:.3f} | {r['minutes_mae']:.3f} | {r['points_mae_delta_vs_season']:+.3f} [{ci[0]:+.3f}, {ci[1]:+.3f}] |")
    lines += ["", "## Historical category redrafts", "", "Scores include half credit for ties. These are paired simulations on one season, not independent real league wins.", "", "| Method | Mean held-out category score / 9 | Match score | Pick compute p95 (ms) |", "|---|---:|---:|---:|"]
    for method, r in results["draft"]["methods"].items():
        lines.append(f"| {method} | {r['each_category_score']:.3f} | {r['match_score']:.1%} | {r['pick_latency_p95_ms']:.3f} |")
    lines += ["", "## Variation across holdout and opponent field", "", "| Cutoff | Field | Z match score | Current fit | G-inspired | Marginal each | Marginal most |", "|---|---|---:|---:|---:|---:|---:|"]
    for f in results["draft"]["folds"]:
        scores = " | ".join(f"{f['methods'][m]['match_score']:.1%}" for m in ("production_z", "current_fit", "g_inspired", "marginal_each", "marginal_most"))
        lines.append(f"| {f['cutoff']} | {f['field']} | {scores} |")
    lines += ["", "## Probability diagnostic on the same finalized rosters", "", "Match-score MSE includes half-credit ties; lower is better. The calendar bootstrap uses only eight prior weeks and does not affect picks. Compare the two estimators within each row, not across different drafted rosters.", "", "| Draft policy | Independent normal MSE | Calendar bootstrap MSE |", "|---|---:|---:|"]
    for method, r in results["draft"]["methods"].items():
        lines.append(f"| {method} | {r['normal_match_score_mse']:.4f} | {r['calendar_bootstrap_match_score_mse']:.4f} |")
    lines += ["", "## Synthetic diagnostics", "", "```json", json.dumps(results["diagnostics"], indent=2), "```", ""]
    (HERE / "RESULTS.md").write_text("\n".join(lines))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="Only three seats per scenario")
    args = parser.parse_args()
    ids, dates, x, names, manifest = load_history()
    results = {"data": manifest, "runtime": provenance(), "projection": projection_benchmark(ids, dates, x),
               "draft": draft_benchmark(ids, dates, x, names, quick=args.quick), "diagnostics": diagnostics()}
    (HERE / "results.json").write_text(json.dumps(results, indent=2, allow_nan=False) + "\n")
    write_summary(results)
    print(json.dumps({"projection": results["projection"]["methods"], "draft": results["draft"]["methods"]}, indent=2))


if __name__ == "__main__":
    main()
