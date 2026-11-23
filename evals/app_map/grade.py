"""
The app map eval's grader (docs: "AI knows the whole app", step 5). Pure: no
model, no database.

A case is a question a user asks about the app and the places that answer it.
A place is a desk and, usually, the steps that lead to a screen on it, as
find_in_app writes them ("Week › Market P › ONE DAY"): `steps` is a
case-insensitive regex searched in those steps, so a case names the control
("Market", "Stream this spot") and not a screen id, which changes when the
crawl's sample data does. A case marked `absent` is something the app can't do.

Two checks read it:

- retrieval (free): does find_in_app's search rank a matching screen in its
  top 3 for the question as asked?
- answers (paid): asked of Dime end to end, does the answer point at a matching
  screen with show_place, name no made-up place, and say so when the app can't?
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

TOP = 3

# An answer that says the app can't do it ("Court Vision doesn't do trades")
_CANT = re.compile(
    r"\b(?:can(?:no|')t|cannot|doesn(?:'|’)t|does not|isn(?:'|’)t|is not|don(?:'|’)t|do not|no way to|not (?:something|"
    r"available|supported|part of|in court vision|in the app)|there(?:'|’)s no|there is no|has no|only (?:supports|works))\b",
    re.IGNORECASE,
)


def matches(expect: dict[str, Any], desk: Optional[str], steps: str) -> bool:
    """Whether a screen (its desk id and steps) is one of the places `expect` names."""
    if desk is None or not re.fullmatch(expect["desk"], desk):
        return False
    pattern = expect.get("steps")
    return pattern is None or re.search(pattern, steps, re.IGNORECASE) is not None


def first_match(case: dict[str, Any], screens: Iterable[dict[str, Any]]) -> Optional[int]:
    """The 1-based rank of the first screen that answers the case, None when none does."""
    for rank, screen in enumerate(screens, start=1):
        if any(matches(e, screen.get("desk"), screen.get("steps") or "") for e in case.get("expect", [])):
            return rank
    return None


def grade_retrieval(case: dict[str, Any], screens: list[dict[str, Any]]) -> dict[str, Any]:
    """One case's search result: `screens` as find_in_app returns them, best first."""
    rank = first_match(case, screens)
    return {
        "id": case["id"],
        "rank": rank,
        "hit": rank is not None and rank <= TOP,
        "top": [s.get("id") for s in screens[:TOP]],
    }


def grade_answer(case: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    """One case asked end to end. `outcome` is what the runner kept of the turn:

    - `ok`: the turn finished;
    - `tools`: the tools it called, in order;
    - `places`: its show_place blocks ({desk id, steps});
    - `text`: the answer;
    - `ungrounded_places`: the made-up-place check's count for the answer.
    """
    places = outcome.get("places") or []
    looked = "find_in_app" in (outcome.get("tools") or [])
    clean = outcome.get("ungrounded_places") == 0
    if case.get("absent"):
        # Nothing to point at: the answer says the app can't, and points nowhere
        right_place = not places and bool(_CANT.search(outcome.get("text") or ""))
    else:
        right_place = first_match(case, places) is not None
    return {
        "id": case["id"],
        "ok": bool(outcome.get("ok")),
        "looked": looked,
        "right_place": bool(outcome.get("ok")) and right_place,
        "clean": bool(outcome.get("ok")) and clean,
        "pass": bool(outcome.get("ok")) and looked and right_place and clean,
    }


def summary(rows: list[dict[str, Any]], keys: Iterable[str]) -> dict[str, float]:
    """The share of rows where each key is true."""
    return {k: (sum(1 for r in rows if r.get(k)) / len(rows)) if rows else 0.0 for k in keys}


def check_cases(cases: list[dict[str, Any]]) -> list[str]:
    """What is wrong with the cases file, if anything."""
    problems = []
    seen: set[str] = set()
    for i, case in enumerate(cases, start=1):
        where = f"line {i} ({case.get('id', '?')})"
        if not case.get("id") or not case.get("question"):
            problems.append(f"{where}: needs an id and a question")
        if case.get("id") in seen:
            problems.append(f"{where}: duplicate id")
        seen.add(case.get("id"))
        if bool(case.get("absent")) == bool(case.get("expect")):
            problems.append(f"{where}: give either expect (the places that answer it) or absent: true")
        for e in case.get("expect") or []:
            if set(e) - {"desk", "steps"} or "desk" not in e:
                problems.append(f"{where}: a place is {{desk, steps?}}")
                continue
            for key in ("desk", "steps"):
                try:
                    re.compile(e.get(key) or "")
                except re.error as exc:
                    problems.append(f"{where}: {key} is not a regex ({exc})")
    return problems
