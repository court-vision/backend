"""
The app map eval's generated check (docs: "AI knows the whole app" § 8): for
every screen in the map, a small model writes the questions a user would ask
that the screen answers, and each question goes through find_in_app's search.
A hit is the screen's own place (`app_map._place`: the same control on any row
of data) in the top 3.

No model is in the loop being tested: the model only writes questions. They
are written at eval time, from the map being tested, and cached by the
screen's fingerprint and the writing model, so the questions follow the app
as it changes and a new crawl pays only for the screens that changed.

**Writing questions costs money**: a few hundred input tokens a screen, about
$0.30 a map on a small model, priced high by services/ai/spend.py when the
model is one it doesn't list. Searching them is free.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from evals.app_map import grade as grading

MODEL = "claude-haiku-5-5"
PER_SCREEN = 3
TREE_CHARS = 1_500
CACHE = "_questions.jsonl"

PROMPT = """\
Below is one screen of Court Vision, a fantasy basketball website, as a crawler \
saw it: the desk it is on, the clicks that lead to it from the desk's front, and \
what it shows (an accessibility tree; names and numbers in it are sample data).

Write {n} short questions a fantasy basketball player might ask about using the \
website that this screen answers: "where do I…", "how do I…", "can I…". Ask the \
way a user would, in their own words: don't copy the screen's labels unless a \
user would say them, and never name the sample players or teams. One question \
per line, nothing else.

Desk: {desk} ({note})
Steps: {steps}
Shows:
{tree}"""


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


async def write_questions(screen: Any, desk: Any, steps: str, model: str) -> tuple[list[str], int]:
    """A screen's questions and what writing them cost, in micro-dollars."""
    from services.ai import spend
    from services.ai.app_map import _cut
    from services.ai.client import get_client

    prompt = PROMPT.format(n=PER_SCREEN, desk=desk.label if desk else screen.desk, note=desk.note if desk else "",
                           steps=steps, tree=_cut(screen.tree, TREE_CHARS))
    response = await get_client().messages.create(model=model, max_tokens=400,
                                                  messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in response.content if b.type == "text")
    questions = [q.strip(" -•\t") for q in text.splitlines() if q.strip(" -•\t")][:PER_SCREEN]
    usage = response.usage
    cost = spend.cost_micros(response.model or model, input_tokens=usage.input_tokens,
                             output_tokens=usage.output_tokens,
                             cache_read_input_tokens=usage.cache_read_input_tokens or 0,
                             cache_creation_input_tokens=usage.cache_creation_input_tokens or 0)
    return questions, cost


def grade_screen(loaded: Any, screen: Any, questions: list[str]) -> dict[str, Any]:
    """Each question's rank for the screen's own place: 1-based, None past the search's results."""
    from services.ai.app_map import _place

    want = _place(screen)
    ranks = []
    for q in questions:
        found = [_place(s) for s in loaded.search(q)]
        ranks.append(found.index(want) + 1 if want in found else None)
    return {"id": screen.id, "desk": screen.desk, "questions": questions, "ranks": ranks,
            "hits": sum(1 for r in ranks if r is not None and r <= grading.TOP)}


async def run(loaded: Any, out: Path, *, max_usd: float, model: Optional[str]) -> int:
    from core.settings import settings
    from services.ai.app_map import _steps

    model = model or MODEL
    cache_path = out.parent / CACHE
    cache = {(r["fingerprint"], r["model"]): r["questions"] for r in _read(cache_path)}
    desks = {d.id: d for d in loaded.map.desks}
    screens = loaded.screens
    missing = [s for s in screens if (s.fingerprint, model) not in cache]
    if missing and settings.anthropic_api_key is None:
        raise SystemExit(f"{len(missing)} screens need questions and ANTHROPIC_API_KEY isn't set")
    print(f"map {loaded.build[:12]}: {len(screens)} screens, {len(missing)} need questions from {model}, "
          f"cap ${max_usd:.2f}")
    spent = 0
    for screen in missing:
        if spent / 1e6 >= max_usd:
            print(f"stopping at the ${max_usd:.2f} cap: {len(missing) - missing.index(screen)} screens left")
            break
        desk = desks.get(screen.desk)
        questions, cost = await write_questions(screen, desk, _steps(desk.label if desk else screen.desk, screen),
                                                model)
        spent += cost
        cache[(screen.fingerprint, model)] = questions
        with cache_path.open("a") as f:
            f.write(json.dumps({"fingerprint": screen.fingerprint, "model": model, "screen": screen.id,
                                "questions": questions}, ensure_ascii=False) + "\n")

    rows = [grade_screen(loaded, s, cache[(s.fingerprint, model)]) for s in screens if (s.fingerprint, model) in cache]
    with (out / f"generated-{loaded.build[:12]}.jsonl").open("w") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    asked = sum(len(r["questions"]) for r in rows)
    hits = sum(r["hits"] for r in rows)
    by_desk: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        by_desk[r["desk"]][0] += r["hits"]
        by_desk[r["desk"]][1] += len(r["questions"])
    print(f"\n{hits}/{asked} questions found their screen in the top {grading.TOP} "
          f"({hits / asked:.0%}), ${spent / 1e6:.2f} spent writing questions" if asked else "no questions yet")
    for desk, (h, n) in sorted(by_desk.items()):
        print(f"  {desk:<10} {h}/{n} ({h / n:.0%})")
    worst = sorted(rows, key=lambda r: (r["hits"] - len(r["questions"]), r["id"]))[:10]
    print("\nworst screens:")
    for r in worst:
        print(f"  {r['hits']}/{len(r['questions'])} {r['id']}: {' | '.join(r['questions'])}")
    return 0
