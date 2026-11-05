"""
The app map eval's end-to-end check: each case asked of Dime.

A case runs through the real turn (services/ai/agent.py `run_turn`): the same
prompt, tool list, loop, display blocks and place check production uses, on
production's model and effort. Around it, only what would touch the database
or a league is replaced:

- find_in_app reads the eval's map (the real tool, its input checked as usual);
- every other lookup answers that it isn't part of this eval, so an answer
  about the app can't lean on a league's data, and a turn that reaches for one
  is visible in its row;
- the turn isn't written, and the month's spend isn't read.

The asker has one ESPN points team, as the header says; nothing reads it.

**Every case costs money**: about $0.05 to $0.15 on Opus 5.5 at medium effort.
The run stops before a case could take it past `--max-usd`, and resumes where
it stopped: rows already in the variant's results.jsonl are skipped.
"""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Optional
from unittest import mock

from evals.app_map import grade as grading

FIXTURE_USER_ID = 0
FIXTURE_TEAMS = [{"team_id": 1, "provider": "espn", "scoring": "points", "points_scoring": "espn_default",
                  "season": "2026-27"}]
# A failure of the service, not the answer: kept apart and asked again next run
RETRYABLE = {"AI_BUSY", "AI_UNAVAILABLE"}
OFF_LIMITS = ("Not part of this eval: here only find_in_app and show_place answer, so answer from the app map "
              "alone and don't use this tool again.")
# The agent's code a row was measured on (prompt, playbook, tools, search, place check), as ai_route keeps its router's
AGENT_CODE = "services/ai"


def agent_sha() -> str:
    root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    for path in sorted((root / AGENT_CODE).rglob("*")):
        if path.is_file() and path.suffix in {".py", ".md"}:
            digest.update(str(path.relative_to(root)).encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()[:12]


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _patches(loaded: Any, model: Optional[str]) -> ExitStack:
    from core.settings import settings
    from services.ai import agent, tools
    from services.ai.tools import ToolOutcome

    real_run_tool = tools.run_tool

    async def run_tool(name: str, raw: Any, *, ctx: Any = None, allowed: Any = None) -> ToolOutcome:
        if name == "find_in_app":
            return await real_run_tool(name, raw, ctx=ctx, allowed=allowed)
        return ToolOutcome(content=OFF_LIMITS, is_error=True)

    async def load(build: Optional[str]) -> Any:
        return loaded

    async def record(*args: Any, **kwargs: Any) -> None:
        return None

    async def month_spend(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the eval reads no spend")

    stack = ExitStack()
    stack.enter_context(mock.patch.object(agent, "run_tool", run_tool))
    stack.enter_context(mock.patch.object(tools.app_map, "load", load))
    stack.enter_context(mock.patch.object(agent.conversations, "record", record))
    stack.enter_context(mock.patch.object(agent.spend, "month_spend", month_spend))
    stack.enter_context(mock.patch.object(agent, "_log_turn", lambda *a, **kw: None))
    if model:
        stack.enter_context(mock.patch.object(settings, "ai_agent_model", model))
    return stack


async def header_line() -> str:
    from core.nba_calendar import nba_date_et
    from services.ai import agent
    from services.ai.service import _season_line

    today = nba_date_et()
    return (f"[Today is {today.strftime('%A')} {today.isoformat()} (US Eastern). {await _season_line()} "
            f"{agent._team_facts(FIXTURE_TEAMS, None)}]")


def outcome_of(turn: Any, done: dict[str, Any], desks: dict[str, str], question: str) -> dict[str, Any]:
    """What the grader reads off a finished turn, and what a person reads in its row."""
    from services.ai import agent

    app = [r["data"] for r in turn.results.values() if r["tool"] == "find_in_app"]
    others = [r["data"] for r in turn.results.values() if r["tool"] != "find_in_app"] + [question]
    text = turn.answer_text()
    made_up = agent.made_up_places(text, app, others)
    places = [{"id": b.get("screen_id"), "desk": desks.get(b.get("desk"), b.get("desk")), "steps": b.get("steps"),
               "href": b.get("href")}
              for b in (p["block"] for p in turn.parts if p["kind"] == "block") if b.get("type") == "place"]
    return {
        "ok": done.get("outcome") == "ok",
        "outcome": done.get("outcome"),
        "tools": [c.name for c in turn.run.tool_calls],
        "queries": [c.input.get("query") for c in turn.run.tool_calls if c.name == "find_in_app"],
        "places": places,
        "text": text,
        "made_up": made_up,
        "ungrounded_places": len(made_up),
        "cost_usd": turn.cost / 1e6,
        "model": turn.run.model,
        "model_calls": turn.run.model_calls,
    }


async def ask(loaded: Any, case: dict[str, Any], header: str) -> dict[str, Any]:
    """One case, one turn."""
    from services.ai import agent, conversations

    turn = agent.Turn(conversation_id=0, user_id=FIXTURE_USER_ID, message=case["question"],
                      history=conversations.History(), build=loaded.build)
    emit = agent.Emitter()
    await agent.run_turn(turn, emit, header_line=header)
    done: dict[str, Any] = {}
    while not emit.queue.empty():
        item = emit.queue.get_nowait()
        if item and item[0] == "done":
            done = item[1]
    desks = {d.label: d.id for d in loaded.map.desks}
    return outcome_of(turn, done, desks, case["question"])


async def run(loaded: Any, cases: list[dict[str, Any]], out: Path, *, max_usd: float, model: Optional[str]) -> int:
    from core.settings import settings

    if settings.anthropic_api_key is None:
        raise SystemExit("ANTHROPIC_API_KEY isn't set: the answers check asks the real model")
    results = out / "results.jsonl"
    rows = _read(results)
    # A later crawl of the same build replaces its map, so the crawl is part of what was measured
    asked_of = {"model": model or settings.ai_agent_model, "effort": settings.ai_agent_effort, "build": loaded.build,
                "crawled_at": loaded.crawled_at.isoformat(), "agent": agent_sha()}
    if rows and any({k: r.get(k) for k in asked_of} != asked_of for r in rows):
        raise SystemExit(f"{results} holds rows from another model, effort, map or version of the agent's code: "
                         "a new measurement goes in a new --variant")
    done_ids = {r["id"] for r in rows}
    pending = [c for c in cases if c["id"] not in done_ids]
    spent = sum(r.get("cost_usd", 0) for r in [*rows, *_read(out / "errors.jsonl")])
    # A turn stops making calls once it has spent its own cap, so that is the most one case can cost
    worst = settings.ai_agent_turn_max_usd
    print(f"{len(pending)} to ask of {asked_of['model']} (effort {asked_of['effort']}) on map {loaded.build[:12]}, "
          f"${spent:.2f} spent, cap ${max_usd:.2f}")
    header = await header_line()
    with _patches(loaded, model):
        for case in pending:
            if spent + worst > max_usd:
                print(f"stopping: another case could pass the ${max_usd:.2f} cap")
                break
            started = time.monotonic()
            outcome = await ask(loaded, case, header)
            spent += outcome["cost_usd"]
            if outcome["outcome"] in RETRYABLE:
                with (out / "errors.jsonl").open("a") as f:
                    f.write(json.dumps({"id": case["id"], **asked_of, **outcome}, default=str) + "\n")
                print(f"  ---- {case['id']:<22} {outcome['outcome']}: asked again next run")
                continue
            row = {**asked_of, **grading.grade_answer(case, outcome), "question": case["question"], **outcome,
                   "seconds": round(time.monotonic() - started, 1)}
            with results.open("a") as f:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            mark = "ok  " if row["pass"] else "FAIL"
            where = ", ".join(p["steps"] or "" for p in outcome["places"]) or "no place"
            print(f"  {mark} {case['id']:<22} ${outcome['cost_usd']:.3f}  {where}"
                  + (f"  made up: {outcome['made_up']}" if outcome["made_up"] else ""))
    rows = _read(results)
    s = grading.summary(rows, ("pass", "looked", "right_place", "clean"))
    print(f"\n{len(rows)}/{len(cases)} asked, ${sum(r.get('cost_usd', 0) for r in rows):.2f}: "
          f"pass {s['pass']:.0%}, called find_in_app {s['looked']:.0%}, right place {s['right_place']:.0%}, "
          f"no made-up place {s['clean']:.0%}")
    return 0
