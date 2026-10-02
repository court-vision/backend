#!/usr/bin/env python
"""
Routing eval: does POST /v1/internal/ai/route take each question to the right place?

Runs every case in cases.jsonl through the real `AiService.route` -- the same
prompt, tools, loop and validation production uses -- with the asker's teams,
the season line, the quota and the question log pinned by fixtures.py. Each
answer is graded by grade.py against the destinations the case lists.

**Every run that reaches the model costs money.** A warm request is about
$0.01 and a cold one about $0.03, so the full set is about $1; `--max-usd`
stops the run before it passes a ceiling. `--selfcheck` and `--summary` are free.

Output goes to .claude/hillclimb/ai-route/<variant>/:

  results.jsonl   one row per (case, rep) that produced a gradable outcome
  errors.jsonl    attempts that did not (API errors, timeouts, the wrong model
                  served) -- never scored, and re-run on the next invocation
  traces/         the full exchange for each row

A run resumes where it stopped: rows already in results.jsonl are skipped.

The first paid run refuses to start until the harness is approved. The approval
records a hash of this file, the grader, the fixtures and the cases, so a score
can't quietly come from a grader that changed underneath it:

    .venv/bin/python -m evals.ai_route.run --approve-harness

Usage:
    .venv/bin/python -m evals.ai_route.run --selfcheck          # free: grader on known answers
    .venv/bin/python -m evals.ai_route.run                      # the full set, variant `baseline`
    .venv/bin/python -m evals.ai_route.run --ids matchup,player-last-15
    .venv/bin/python -m evals.ai_route.run --variant v1 --model claude-opus-5-5
    .venv/bin/python -m evals.ai_route.run --summary            # reprint the numbers from disk
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.ai_route import grade as grading  # noqa: E402

HERE = Path(__file__).resolve().parent
FLOW = ROOT / ".claude" / "hillclimb" / "ai-route"
CASES = HERE / "cases.jsonl"
# Spend a case could reach in the worst case seen so far (4 model calls, cold cache)
WORST_CASE_USD = 0.08
RETRYABLE = {"AI_BUSY", "AI_UNAVAILABLE"}
MAX_ATTEMPTS = 3


def load_cases() -> list[dict[str, Any]]:
    cases = [json.loads(line) for line in CASES.read_text().splitlines() if line.strip()]
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise SystemExit("cases.jsonl: duplicate ids")
    return cases


def load_state() -> dict[str, Any]:
    return json.loads((FLOW / "_state.json").read_text())


def harness_sha(state: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    for rel in sorted(state["harness_paths"]):
        digest.update(rel.encode() + b"\0" + hashlib.sha256((ROOT / rel).read_bytes()).digest())
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        f.flush()


def cost_usd(row: dict[str, Any], prices: dict[str, dict[str, float]]) -> float:
    """Dollars for one row or failed attempt, from its own model and token counts.
    A price entry is $ per million tokens: `in`, `out`, and optionally `cache_write`
    and `cache_read` (1.25x and 0.1x input when a model doesn't list its own). A
    fallback model this table doesn't list is priced as the model that was asked for."""
    served = row.get("model") or ""
    price = prices.get(served) or prices.get(re.sub(r"-\d{8}$", "", served)) or prices[row["requested_model"]]
    usage = row.get("usage") or {}
    return (usage.get("input_tokens", 0) * price["in"]
            + usage.get("output_tokens", 0) * price["out"]
            + usage.get("cache_creation_input_tokens", 0) * price.get("cache_write", price["in"] * 1.25)
            + usage.get("cache_read_input_tokens", 0) * price.get("cache_read", price["in"] * 0.1)) / 1e6


def served_as_asked(requested: str, served: str) -> bool:
    """An alias may resolve to its dated snapshot; anything else is a different model."""
    return served == requested or re.fullmatch(re.escape(requested) + r"-\d{8}", served) is not None


def prompt_text(case: dict[str, Any]) -> str:
    """What a row was asked, as stored on it. A row whose text no longer matches
    its case answered a different question."""
    return f"{case['question']}\ncontext: {json.dumps(case['context'], sort_keys=True)}"


# ------------------------------------------------------------------ one case


def _trace(recorder: Any) -> list[dict[str, Any]]:
    """The exchange as the model saw it: system, user, then each call's tool
    calls, their results, and the final answer."""
    calls = recorder.calls
    if not calls:
        return []
    turns: list[dict[str, Any]] = [
        {"role": "system", "content": calls[0]["system"][0]["text"]},
        {"role": "user", "content": calls[0]["messages"][0]["content"]},
    ]
    for i, call in enumerate(calls):
        response = call["response"]
        if response is None:
            turns.append({"role": "assistant", "content": "(this model call failed before answering)"})
            continue
        for block in response.content:
            if block.type == "tool_use":
                turns.append({"role": "tool_call", "name": block.name, "content": json.dumps(block.input, indent=2)})
            elif block.type == "text" and block.text.strip():
                turns.append({"role": "assistant", "content": block.text})
        if i + 1 < len(calls):
            sent_back = calls[i + 1]["messages"][-1]["content"]
            for block in sent_back if isinstance(sent_back, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    turns.append({"role": "tool_result", "content": str(block.get("content"))})
    return turns


def _usage(data: Any, recorder: Any) -> dict[str, int]:
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    if data is not None:
        return {key: getattr(data.usage, key) for key in keys}
    # A failed request: the question log's meters are the same ones the service keeps
    row = recorder.question_row or {}
    return {key: int(row.get(key) or 0) for key in keys}


async def attempt(case: dict[str, Any], rep: int, *, model: str, timeout_s: float, attempt_no: int) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    """Run one case once. Returns ("row" | "error", record, trace)."""
    from core.errors import AppError
    from schemas.ai import AiContext
    from services.ai.service import AiService

    from evals.ai_route import fixtures

    recorder = fixtures.Recorder(season=case.get("season", "preseason"))
    fixtures.recording(recorder)
    started = time.monotonic()
    data, failure, error = None, None, None
    try:
        async with asyncio.timeout(timeout_s):
            response = await AiService.route(case["question"], AiContext(**case["context"]),
                                             user_id=fixtures.FIXTURE_USER_ID)
        data = response.data
    except TimeoutError:
        error = {"class": "timeout", "code": "EVAL_WALL_CLOCK", "message": f"no answer within {timeout_s:.0f}s"}
    except AppError as exc:
        if exc.error_code == "AI_DECLINED":
            failure = "refusal"          # a graded outcome: the model declined
        elif exc.error_code == "AI_INCOMPLETE":
            failure = "unreadable"       # a graded outcome: the model's answer could not be used
        else:
            error = {"class": "timeout" if exc.error_code == "AI_TIMEOUT" else "serving_error",
                     "code": exc.error_code, "message": exc.message}
    except Exception as exc:  # the harness's own failure, not the model's
        error = {"class": "harness_error", "code": type(exc).__name__, "message": str(exc)[:300]}
    latency = round(time.monotonic() - started, 3)

    answered = [call for call in recorder.calls if call["response"] is not None]
    served = data.usage.model if data is not None else (answered[-1]["response"].model if answered else None)
    usage = _usage(data, recorder)
    trace = _trace(recorder)
    base = {"prompt_id": case["id"], "rep": rep, "attempt": attempt_no, "model": served, "requested_model": model,
            "usage": usage, "latency_s": latency}

    if error is None and data is not None and not served_as_asked(model, served) and not data.usage.fallback:
        error = {"class": "served_model_mismatch", "code": "EVAL_MODEL",
                 "message": f"asked for {model}, served by {served}"}
    if error is not None:
        return "error", {**base, **error}, trace

    logged = recorder.question_row or {}
    if data is not None:
        out = {
            "kind": data.kind, "text": data.text,
            "target": data.target.model_dump() if data.target else None,
            "statmuse_query": data.statmuse_query, "statmuse_url": data.statmuse_url,
            "gap": data.gap, "missing": logged.get("missing"), "suggestions": data.suggestions,
            # What the server turned away (a refused target, an unknown or ambiguous name), from the question log
            "rejected_target": logged.get("target") if data.kind == "cannot" else None,
            "lookups": [call.model_dump() for call in data.sources],
            "ungrounded_numbers": logged.get("ungrounded_numbers"),
            "model_calls": len(recorder.calls),
        }
    else:
        out = {"kind": None, "failure": failure, "lookups": logged.get("tool_calls") or [],
               "model_calls": len(recorder.calls)}
    stop_reason = answered[-1]["response"].stop_reason if answered else None

    row = {
        **base,
        "prompt": prompt_text(case),
        "tags": case["tags"],
        "status": "truncated" if stop_reason == "max_tokens" else "ok",
        "stop_reason": stop_reason,
        "model_calls": len(recorder.calls),
        "tool_calls": len(out["lookups"]),
        "model_s": round(sum(call.get("latency_s", 0) for call in recorder.calls), 3),
        "grade": grading.grade(case, out),
        "explanation": {"route_ok": grading.explain(case, out, fixtures.NAMES)},
        "output": out,
        "meta": {"fallback": bool(data.usage.fallback) if data is not None else False,
                 "season": recorder.season, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
    }
    # A zero here is a wiring bug, not a measurement: fail loud rather than write it
    if not served or usage["input_tokens"] + usage["cache_read_input_tokens"] <= 0 or not trace:
        raise RuntimeError(f"{case['id']}: row is missing model, usage or trace -- refusing to write it")
    return "row", row, trace


# ------------------------------------------------------------------- the run


async def run(args: argparse.Namespace, state: dict[str, Any], cases: list[dict[str, Any]]) -> None:
    from core.settings import settings

    from evals.ai_route import fixtures

    if not settings.ai_enabled or settings.anthropic_api_key is None:
        raise SystemExit("AI_ENABLED and ANTHROPIC_API_KEY must be set in .env for a paid run")
    model = args.model or settings.ai_model
    if model not in state["prices"]:
        raise SystemExit(f"no price for {model} in _state.json, so the spend cap can't be enforced")
    settings.ai_model = model
    if args.effort:
        settings.ai_effort = args.effort
    fixtures.install()

    out_dir = FLOW / args.variant
    (out_dir / "traces").mkdir(parents=True, exist_ok=True)
    results, errors = out_dir / "results.jsonl", out_dir / "errors.jsonl"
    done = {(row["prompt_id"], row["rep"]) for row in read_jsonl(results)}
    pending = [(case, rep) for case in cases for rep in range(args.reps) if (case["id"], rep) not in done]
    if not pending:
        print("nothing to run: every (case, rep) is already in results.jsonl")
        return

    prices = state["prices"]
    spent = sum(cost_usd(r, prices) for r in [*read_jsonl(results), *read_jsonl(errors)])
    spent_before = spent
    in_flight = 0
    skipped: list[str] = []
    gate = asyncio.Semaphore(args.concurrency)
    print(f"{len(pending)} to run on {model} (effort {settings.ai_effort}), cap ${args.max_usd:.2f}, "
          f"${spent:.3f} already spent in this variant")

    async def one(case: dict[str, Any], rep: int) -> None:
        nonlocal spent, in_flight
        async with gate:
            if spent + (in_flight + 1) * WORST_CASE_USD > args.max_usd:
                skipped.append(case["id"])
                return
            in_flight += 1
            try:
                for attempt_no in range(1, MAX_ATTEMPTS + 1):
                    kind, record, trace = await attempt(case, rep, model=model, timeout_s=args.timeout_s,
                                                        attempt_no=attempt_no)
                    spent += cost_usd(record, prices)
                    if kind == "row":
                        (out_dir / "traces" / f"{case['id']}_rep{rep}.json").write_text(
                            json.dumps(trace, ensure_ascii=False, indent=1))
                        append_jsonl(results, record)
                        mark = "pass" if record["grade"]["route_ok"] == 1 else "FAIL"
                        print(f"  {mark}  {case['id']:<28} {record['model_calls']} calls  {record['latency_s']:5.1f}s  "
                              f"${cost_usd(record, prices):.4f}")
                        return
                    append_jsonl(errors, record)
                    print(f"  ERR   {case['id']:<28} {record['class']} {record['code']} (attempt {attempt_no})")
                    if record["code"] not in RETRYABLE or attempt_no == MAX_ATTEMPTS:
                        return
                    await asyncio.sleep(2 ** attempt_no + random.uniform(0, 1))
            finally:
                in_flight -= 1

    # The first request writes the shared prompt cache; the rest read it. Started
    # together, they would each pay to write the same prefix.
    first, rest = pending[0], pending[1:]
    await asyncio.create_task(one(*first))
    await asyncio.gather(*(asyncio.create_task(one(case, rep)) for case, rep in rest))

    print(f"spent this run: ${spent - spent_before:.3f} (variant total ${spent:.3f})")
    if skipped:
        print(f"STOPPED BY THE CAP: {len(skipped)} not run -- raise --max-usd and re-run to finish them")


# ------------------------------------------------------------------- reports


def print_summary(variant: str, state: dict[str, Any], cases: list[dict[str, Any]]) -> None:
    rows = read_jsonl(FLOW / variant / "results.jsonl")
    errors = read_jsonl(FLOW / variant / "errors.jsonl")
    if not rows:
        print(f"{variant}: no results yet")
        return
    s = grading.summarize(rows, cases)
    prices = state["prices"]

    def pct(m: Optional[dict[str, Any]]) -> str:
        if m is None:
            return "n/a"
        return f"{m['rate']:.1%} ({m['rate'] * m['n']:.0f}/{m['n']}, 95% CI {m['low']:.0%}-{m['high']:.0%})"

    labels = {m["id"]: m["label"] for m in state["metrics"]}
    print(f"\n{variant}: {s['cases']} of {len(cases)} cases scored, {s['rows']} rows, "
          f"{s['truncated']} truncated, {len(errors)} failed attempts in errors.jsonl")
    for metric in grading.METRICS:
        print(f"  {labels[metric]:<16} {pct(s['metrics'][metric])}")
    print(f"  {'  minus quoted':<16} {pct(s['unquoted'])}   (without the cases the prompt quotes as examples)")
    print("  by destination:")
    for group, m in s["by_group"].items():
        print(f"    {group:<10} {pct(m)}")
    print("  kind, as a confusion table (precision = when it chose this, was it right; recall = when it should have, did it):")
    for kind, c in s["confusion"].items():
        p = "n/a" if c["precision"] is None else f"{c['precision']:.0%}"
        r = "n/a" if c["recall"] is None else f"{c['recall']:.0%}"
        print(f"    {kind:<9} precision {p:>4} (chosen {c['said']})   recall {r:>4} (wanted {c['wanted']})")
    print(f"  fantasy questions sent to StatMuse: {len(s['stay_home']['leaks'])} of {s['stay_home']['n']} {s['stay_home']['leaks'] or ''}")
    print(f"  refused destinations (invalid_target): {len(s['invalid_targets'])} {s['invalid_targets'] or ''}")
    if s["refusals"]:
        print(f"  refusals: {s['refusals']}")
    ok = [r for r in rows if r.get("status", "ok") == "ok"]
    costs = sorted(cost_usd(r, prices) for r in ok)
    lat = sorted(r["latency_s"] for r in ok)
    total = sum(cost_usd(r, prices) for r in [*rows, *errors])
    print(f"  cost: ${total:.3f} total, per question median ${statistics.median(costs):.4f} "
          f"(min ${costs[0]:.4f}, max ${costs[-1]:.4f})")
    print(f"  time: half within {quantile(lat, 0.5):.1f}s, 9 in 10 within {quantile(lat, 0.9):.1f}s, slowest {lat[-1]:.1f}s; "
          f"answered in one model call: {s['one_call']:.0%}")
    bars = check_bars(state.get("bars", {}), s, lat)
    if bars:
        print("  bars:")
        for name, passed, detail in bars:
            print(f"    {'PASS' if passed else 'FAIL'}  {name}: {detail}")
    models = sorted({r["model"] for r in rows})
    print(f"  served by: {', '.join(models)}; fallbacks: {sum(r.get('meta', {}).get('fallback', False) for r in rows)}")
    if s["failed"]:
        print(f"  not passing ({len(s['failed'])}): {', '.join(s['failed'])}")
    if s["missing"]:
        print(f"  NOT YET RUN ({len(s['missing'])}): {', '.join(s['missing'][:12])}{' ...' if len(s['missing']) > 12 else ''}")


def regrade(variant: str, cases: list[dict[str, Any]]) -> None:
    """Free: grade the answers already on disk again, after a case or the grader
    changed. The model's answers are not touched. A row that answered a question
    since reworded is dropped, with its trace, so the next run asks it again."""
    from evals.ai_route import fixtures

    out_dir = FLOW / variant
    rows, known = read_jsonl(out_dir / "results.jsonl"), {case["id"]: case for case in cases}
    kept, dropped, changed = [], [], []
    for row in rows:
        case = known.get(row["prompt_id"])
        if case is None or row["prompt"] != prompt_text(case):
            dropped.append(row["prompt_id"])
            (out_dir / "traces" / f"{row['prompt_id']}_rep{row['rep']}.json").unlink(missing_ok=True)
            continue
        out = {"model_calls": row["model_calls"], **row["output"]}
        new = grading.grade(case, out)
        if new != row["grade"]:
            changed.append(f"{row['prompt_id']}: " + ", ".join(
                f"{m} {row['grade'].get(m, '-')}->{new[m]}" for m in new if new[m] != row["grade"].get(m)))
        kept.append({**row, "tags": case["tags"], "grade": new,
                     "explanation": {"route_ok": grading.explain(case, out, fixtures.NAMES)}})
    tmp = out_dir / "results.jsonl.tmp"
    tmp.write_text("".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in kept))
    tmp.replace(out_dir / "results.jsonl")
    print(f"regraded {len(kept)} rows; {len(changed)} changed score; dropped {len(dropped)} {dropped or ''}")
    for line in changed:
        print("  " + line)


def quantile(sorted_values: list[float], q: float) -> float:
    return sorted_values[min(len(sorted_values) - 1, round(q * (len(sorted_values) - 1)))]


def check_bars(bars: dict[str, float], s: dict[str, Any], latencies: list[float]) -> list[tuple[str, bool, str]]:
    """The agreed pass/fail lines (`bars` in _state.json) against one variant's numbers.
    Routing and time are separate lines on purpose: a slow right answer and a fast
    wrong one are different problems."""
    if not bars or not latencies or s["metrics"]["route_ok"] is None:
        return []
    right = s["metrics"]["route_ok"]["rate"]
    made_up = s["metrics"]["clean_text"]
    made_up_n = round((1 - made_up["rate"]) * made_up["n"])
    p50, p90 = quantile(latencies, 0.5), quantile(latencies, 0.9)
    return [
        ("right place", right >= bars["route_ok_min"], f"{right:.1%} (needs {bars['route_ok_min']:.0%})"),
        ("fantasy questions sent to StatMuse", len(s["stay_home"]["leaks"]) <= bars["leaks_max"],
         f"{len(s['stay_home']['leaks'])} (allows {bars['leaks_max']:.0f})"),
        ("made-up numbers", made_up_n <= bars["made_up_max"], f"{made_up_n} (allows {bars['made_up_max']:.0f})"),
        ("refused destinations", len(s["invalid_targets"]) <= bars["refused_max"],
         f"{len(s['invalid_targets'])} (allows {bars['refused_max']:.0f})"),
        ("time, half within", p50 <= bars["latency_p50_s"], f"{p50:.1f}s (needs {bars['latency_p50_s']:.0f}s)"),
        ("time, 9 in 10 within", p90 <= bars["latency_p90_s"], f"{p90:.1f}s (needs {bars['latency_p90_s']:.0f}s)"),
    ]


def selfcheck(cases: list[dict[str, Any]]) -> int:
    """Free: push answers of known quality through the grader. The reference
    answers must all pass, and each do-nothing answer must fail nearly everywhere."""
    problems = 0
    failing = [case["id"] for case in cases if grading.grade(case, grading.oracle(case))["route_ok"] != 1]
    print(f"reference answers: {len(cases) - len(failing)}/{len(cases)} pass" + (f"  FAILING: {failing}" if failing else ""))
    problems += len(failing)
    for name, out in grading.NULLS.items():
        passing = [case["id"] for case in cases if grading.grade(case, out)["route_ok"] == 1]
        print(f"{name:<34} passes {len(passing):>2}/{len(cases)}")
        # A constant answer can only be right where that constant is the expected answer
        if len(passing) > len(cases) * 0.15:
            problems += 1
            print(f"  TOO LENIENT: {passing[:8]}")
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--variant", default="baseline", help="output directory: `baseline` or `v<N>`")
    parser.add_argument("--model", help="override AI_MODEL for this run")
    parser.add_argument("--effort", help="override AI_EFFORT for this run")
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--ids", help="comma-separated case ids (default: all)")
    parser.add_argument("--timeout-s", type=float, default=120.0, help="wall-clock ceiling per case")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--max-usd", type=float, default=2.0, help="stop starting cases before spend passes this")
    parser.add_argument("--selfcheck", action="store_true", help="free: grade known-good and do-nothing answers")
    parser.add_argument("--summary", action="store_true", help="free: reprint the numbers from results.jsonl")
    parser.add_argument("--regrade", action="store_true", help="free: grade the stored answers again after a case or grader change")
    parser.add_argument("--approve-harness", action="store_true", help="record the current harness hash and exit")
    args = parser.parse_args()
    if not re.fullmatch(r"baseline|v\d+", args.variant):
        raise SystemExit("--variant must be `baseline` or `v<N>`; the report ignores anything else")

    cases, state = load_cases(), load_state()
    if args.selfcheck:
        raise SystemExit(1 if selfcheck(cases) else 0)
    if args.regrade:
        regrade(args.variant, cases)
        print_summary(args.variant, state, cases)
        return
    if args.summary:
        print_summary(args.variant, state, cases)
        return

    sha, lock = harness_sha(state), FLOW / "harness.lock"
    if args.approve_harness:
        lock.write_text(json.dumps({"sha": sha, "approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}))
        print(f"harness approved: {sha[:12]}")
        return
    approved = json.loads(lock.read_text())["sha"] if lock.exists() else None
    if approved != sha:
        print("The harness (runner, grader, fixtures or cases) has changed since it was last approved, "
              "or was never approved.\nReview it, then run:  .venv/bin/python -m evals.ai_route.run --approve-harness",
              file=sys.stderr)
        raise SystemExit(2)

    if args.ids:
        wanted = set(args.ids.split(","))
        unknown = wanted - {case["id"] for case in cases}
        if unknown:
            raise SystemExit(f"unknown case ids: {sorted(unknown)}")
        selected = [case for case in cases if case["id"] in wanted]
    else:
        selected = cases
    asyncio.run(run(args, state, selected))
    print_summary(args.variant, state, cases)


if __name__ == "__main__":
    main()
