#!/usr/bin/env python
"""
The app map eval (docs: "AI knows the whole app", step 5): does Dime know
where things are in Court Vision?

Three checks, cheapest first:

  --retrieval   free. Each case's question, as asked, through find_in_app's
                search; a hit is a matching screen in the top 3.
  --generated   about $0.30 a map. A small model writes questions for every
                screen (cached by the screen's fingerprint, so a new crawl
                pays only for screens that changed), then the same top-3
                check for each screen's own questions.
  --answers     about $0.10 a question. Each case asked of Dime end to end:
                the real prompt, tools and turn loop, with find_in_app and
                show_place on the map and every other tool answering that it
                isn't part of this eval. A pass called find_in_app, pointed
                at a matching screen with show_place (or, for something the
                app can't do, said so and pointed nowhere), and named no
                place the map doesn't have.

The map is a file the App Map workflow made (`--map app-map.json`, from the
run's artifact) or one stored in the database in `.env` (`--build <sha>`, the
newest by default). Nothing is written to the database.

The paid checks name a `--variant`, stop before `--max-usd`, and refuse to
start until the harness is approved, which records a hash of this file, the
grader and the cases:

    .venv/bin/python -m evals.app_map.run --approve-harness

Usage:
    .venv/bin/python -m evals.app_map.run --selfcheck
    .venv/bin/python -m evals.app_map.run --retrieval --map app-map.json
    .venv/bin/python -m evals.app_map.run --generated --map app-map.json --variant g1
    .venv/bin/python -m evals.app_map.run --answers --map app-map.json --variant a1 --max-usd 4
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.app_map import grade as grading  # noqa: E402

HERE = Path(__file__).resolve().parent
CASES = HERE / "cases.jsonl"
FLOW = ROOT / ".claude" / "hillclimb" / "app-map"
HARNESS = ("evals/app_map/run.py", "evals/app_map/grade.py", "evals/app_map/answers.py",
           "evals/app_map/generated.py", "evals/app_map/cases.jsonl")


def load_cases() -> list[dict[str, Any]]:
    cases = [json.loads(line) for line in CASES.read_text().splitlines() if line.strip()]
    problems = grading.check_cases(cases)
    if problems:
        raise SystemExit("cases.jsonl:\n  " + "\n  ".join(problems))
    return cases


def harness_sha() -> str:
    digest = hashlib.sha256()
    for rel in sorted(HARNESS):
        digest.update(rel.encode() + b"\0" + hashlib.sha256((ROOT / rel).read_bytes()).digest())
    return digest.hexdigest()


def approved() -> bool:
    lock = FLOW / "harness.lock"
    return lock.exists() and json.loads(lock.read_text()).get("sha") == harness_sha()


# ------------------------------------------------------------------ retrieval


def retrieval(loaded: Any, cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each findable case's question through the search, graded."""
    rows = []
    for case in cases:
        if case.get("absent"):
            continue
        found = loaded.find(case["question"])["screens"]
        rows.append(grading.grade_retrieval(case, found))
    return rows


def print_retrieval(loaded: Any, rows: list[dict[str, Any]]) -> None:
    print(f"map {loaded.build[:12]} crawled {loaded.crawled_at:%Y-%m-%d %H:%M}Z, "
          f"{len(loaded.map.screens)} screens\n")
    width = max(len(r["id"]) for r in rows)
    for r in rows:
        rank = f"#{r['rank']}" if r["rank"] else "--"
        print(f"  {'ok  ' if r['hit'] else 'MISS'} {r['id']:<{width}}  {rank:>3}   {', '.join(r['top'])}")
    hits = sum(r["hit"] for r in rows)
    mrr = sum(1 / r["rank"] for r in rows if r["rank"]) / len(rows)
    print(f"\n{hits}/{len(rows)} in the top {grading.TOP} ({hits / len(rows):.0%}), MRR {mrr:.2f}")


# ------------------------------------------------------------------ selfcheck


def selfcheck(cases: list[dict[str, Any]]) -> int:
    """The grader on answers whose grades are known. Free."""
    case = {"id": "x", "question": "q", "expect": [{"desk": "week", "steps": "Market"}]}
    absent = {"id": "y", "question": "q", "absent": True}
    market = {"desk": "week", "steps": "Week › Market P › ONE DAY", "id": "week/market-p/one-day"}
    checks = [
        ("a matching screen in the top 3 is a hit",
         grading.grade_retrieval(case, [{"desk": "scout", "steps": "Scout"}, market])["hit"], True),
        ("a matching screen 4th is a miss",
         grading.grade_retrieval(case, [{"desk": "scout", "steps": "Scout"}] * 3 + [market])["hit"], False),
        ("the desk must match too",
         grading.grade_retrieval(case, [{"desk": "scout", "steps": "Scout › MARKET"}])["hit"], False),
        ("an answer that looked, showed the place and made nothing up passes",
         grading.grade_answer(case, {"ok": True, "tools": ["find_in_app", "show_place"], "places": [market],
                                     "text": "Open **Market**.", "ungrounded_places": 0})["pass"], True),
        ("an answer that never looked fails",
         grading.grade_answer(case, {"ok": True, "tools": ["show_place"], "places": [market],
                                     "ungrounded_places": 0})["pass"], False),
        ("a made-up place fails",
         grading.grade_answer(case, {"ok": True, "tools": ["find_in_app"], "places": [market],
                                     "ungrounded_places": 1})["pass"], False),
        ("something the app can't do is said so",
         grading.grade_answer(absent, {"ok": True, "tools": ["find_in_app"], "places": [],
                                       "text": "Court Vision doesn't do trades.", "ungrounded_places": 0})["pass"],
         True),
        ("and pointed nowhere",
         grading.grade_answer(absent, {"ok": True, "tools": ["find_in_app"], "places": [market],
                                       "text": "Court Vision doesn't do trades.", "ungrounded_places": 0})["pass"],
         False),
        ("a failed turn fails",
         grading.grade_answer(case, {"ok": False, "tools": ["find_in_app"], "places": [market],
                                     "ungrounded_places": 0})["pass"], False),
    ]
    bad = [name for name, got, want in checks if got != want]
    for name in bad:
        print(f"selfcheck failed: {name}")
    print(f"{len(cases)} cases, grader {len(checks) - len(bad)}/{len(checks)} checks")
    return 1 if bad else 0


# ------------------------------------------------------------------ main


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--selfcheck", action="store_true", help="free: the grader on known answers")
    what.add_argument("--retrieval", action="store_true", help="free: the cases' questions through the search")
    what.add_argument("--generated", action="store_true", help="paid: questions written per screen")
    what.add_argument("--answers", action="store_true", help="paid: the cases asked of Dime end to end")
    what.add_argument("--approve-harness", action="store_true", help="record the harness's hash and exit")
    parser.add_argument("--map", type=Path, help="an app-map.json from the App Map workflow")
    parser.add_argument("--build", help="a stored map's build (the newest when neither is given)")
    parser.add_argument("--variant", help="where a paid run's rows go, under .claude/hillclimb/app-map/")
    parser.add_argument("--max-usd", type=float, default=2.0, help="stop a paid run before it spends this")
    parser.add_argument("--ids", help="only these cases, comma-separated")
    parser.add_argument("--model", help="the model a paid run asks (each check has its own default)")
    args = parser.parse_args(argv)

    cases = load_cases()
    if args.ids:
        wanted = set(args.ids.split(","))
        cases = [c for c in cases if c["id"] in wanted]

    if args.selfcheck:
        return selfcheck(cases)
    if args.approve_harness:
        FLOW.mkdir(parents=True, exist_ok=True)
        sha = harness_sha()
        (FLOW / "harness.lock").write_text(json.dumps(
            {"sha": sha, "approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}))
        print(f"harness approved: {sha[:12]}")
        return 0

    from evals.app_map import maps

    loaded = asyncio.run(maps.load(args.map, args.build))
    if args.retrieval:
        rows = retrieval(loaded, cases)
        print_retrieval(loaded, rows)
        return 0

    if not args.variant:
        raise SystemExit("A paid run names its --variant (a new one per measurement)")
    if not approved():
        raise SystemExit("The harness (runner, grader or cases) changed since it was last approved, or never "
                         "was. Review it, then run:  .venv/bin/python -m evals.app_map.run --approve-harness")
    out = FLOW / args.variant
    out.mkdir(parents=True, exist_ok=True)
    if args.generated:
        from evals.app_map import generated

        return asyncio.run(generated.run(loaded, out, max_usd=args.max_usd, model=args.model))
    from evals.app_map import answers

    return asyncio.run(answers.run(loaded, cases, out, max_usd=args.max_usd, model=args.model))


if __name__ == "__main__":
    sys.exit(main())
