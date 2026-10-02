"""
Grades one routed answer against a case's expected destinations.

Pure: no I/O, no model, no database. A case lists every destination that would
be right (`expect`); an answer passes when it matches any one of them. What
"matches" means is deliberately the frontend's view of a target, not a string
comparison:

- A page target with no `team_id` means "the selected team", so null and the
  selected team's ID are the same place.
- A terminal target with no window keeps whatever window is showing, so null,
  "season" and the context's own window all pass when the question named none.
- `/rankings` honours `cats` only in the categories format, and treats the
  standard nine as no filter at all (frontend lib/rankings-params.ts).
- A StatMuse question is checked for what it has to say (the full names, the
  stat, the season) and what it must not ("vs" where a side-by-side table was
  asked for), never for its exact wording.
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any, Iterable, Optional

ANY = "*"
STANDARD_9CAT = frozenset({"fg_pct", "ft_pct", "fg3m", "pts", "reb", "ast", "stl", "blk", "tov"})
METRICS = ("route_ok", "kind_ok", "clean_text", "lean_lookups")


def words(text: str) -> str:
    """Lower case, accents folded, everything but letters and digits to single
    spaces, padded -- so a phrase can be matched on word boundaries."""
    folded = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii").lower()
    return " " + re.sub(r"[^a-z0-9]+", " ", folded).strip() + " "


def says(query_words: str, phrase: str) -> bool:
    """Whole-word match, tolerant of a plural: "rebound" matches "rebounds"."""
    needle = words(phrase)
    return needle in query_words or needle[:-1] + "s " in query_words


# ------------------------------------------------------------------ matching


def _window_ok(spec: dict[str, Any], got: Optional[str], context: dict[str, Any]) -> bool:
    if "window" not in spec:
        return got in (None, "season", context.get("window"))
    return spec["window"] == ANY or got == spec["window"]


def _terminal_ok(spec: dict[str, Any], got: dict[str, Any], context: dict[str, Any]) -> bool:
    if got.get("type") != "terminal" or got.get("mode") != spec["mode"]:
        return False
    if spec["mode"] == "player":
        focus, compare = got.get("player_id"), set(got.get("compare_ids") or [])
        if "players" in spec:  # any of them may be the focus
            if {focus, *compare} != set(spec["players"]):
                return False
        elif focus != spec["player_id"] or compare != set(spec.get("compare_ids", [])):
            return False
        return _window_ok(spec, got.get("window"), context)
    if spec["mode"] == "team":
        return got.get("team_id") == spec["team_id"]
    if spec["mode"] == "nba_team":
        return got.get("nba_team") == spec["nba_team"]
    return True  # overview has no subject


def _rankings_ok(spec: dict[str, Any], got: dict[str, Any]) -> bool:
    want_format = spec.get("format", "points")
    if want_format != ANY and got.get("format") != want_format and not (want_format == "points" and got.get("format") is None):
        return False
    want_window = spec.get("window")
    if got.get("window") not in (want_window if isinstance(want_window, list) else [want_window]):
        return False
    cats, want_cats = frozenset(got.get("cats") or []), frozenset(spec.get("cats", []))
    if cats != want_cats and not (not want_cats and cats == STANDARD_9CAT):
        return False
    want_scope = spec.get("scope", "global")
    if want_scope != ANY and got.get("scope") != want_scope and not (want_scope == "global" and got.get("scope") is None):
        return False
    return "min_games" not in spec or got.get("min_games") == spec["min_games"]


def _page_ok(spec: dict[str, Any], got: dict[str, Any], context: dict[str, Any]) -> bool:
    if got.get("type") != "page" or got.get("page") != spec["page"]:
        return False
    selected, team = context.get("team_id"), got.get("team_id")
    want = spec.get("team_id", selected)
    if team != want and not (team is None and selected == want):
        return False
    return spec["page"] != "rankings" or _rankings_ok(spec.get("rankings", {}), got.get("rankings") or {})


def _statmuse_ok(spec: dict[str, Any], query: Optional[str]) -> bool:
    said = words(query or "")
    return (
        bool(said.strip())
        and all(says(said, phrase) for phrase in spec.get("all", []))
        and all(any(says(said, phrase) for phrase in group) for group in spec.get("any", []))
        and not any(says(said, phrase) for phrase in spec.get("none", []))
    )


def _matches(alt: dict[str, Any], out: dict[str, Any], context: dict[str, Any]) -> bool:
    if out.get("kind") != alt["kind"]:
        return False
    if alt["kind"] == "statmuse":
        return _statmuse_ok(alt, out.get("statmuse_query"))
    if alt["kind"] == "cannot":
        # invalid_target is the server refusing a destination, never the model declining
        return out.get("gap") != "invalid_target" and ("gap" not in alt or out.get("gap") in alt["gap"])
    target = out.get("target") or {}
    check = _terminal_ok if alt["target"]["type"] == "terminal" else _page_ok
    return check(alt["target"], target, context)


def grade(case: dict[str, Any], out: dict[str, Any]) -> dict[str, float]:
    """One answer's scores. `out` is the route response plus what the question
    log recorded: kind, target, statmuse_query, gap, text, lookups,
    ungrounded_numbers. A request that produced no answer has kind None and
    scores zero everywhere."""
    context = case.get("context", {})
    kinds = {alt["kind"] for alt in case["expect"]}
    scores = {
        "route_ok": float(any(_matches(alt, out, context) for alt in case["expect"])),
        "kind_ok": float(out.get("kind") in kinds and out.get("gap") != "invalid_target"),
        # The router states no statistics: any number not in the question or the target was made up
        "clean_text": float(out.get("kind") is not None and out.get("ungrounded_numbers") == 0
                            and "\n" not in (out.get("text") or "")),
    }
    if "max_lookups" in case:
        scores["lean_lookups"] = float(out.get("kind") is not None and lean(case, out))
    return scores


def lean(case: dict[str, Any], out: dict[str, Any]) -> bool:
    """No wasted trips to the model. A lookup round is a second model call, which
    doubles the wait: an answer that needs no lookup takes one call, and one that
    needs lookups makes them together and takes two."""
    limit = case["max_lookups"]
    calls = out.get("model_calls")
    return len(out.get("lookups") or []) <= limit and (calls is None or calls <= (1 if limit == 0 else 2))


# ------------------------------------------------------------- explanations


def describe(alt: dict[str, Any], names: Optional[dict[Any, str]] = None) -> str:
    """One expected destination in plain words, for the review page and the report."""
    names = names or {}

    def who(i: Any) -> str:
        return names.get(i, names.get(str(i), str(i)))

    if alt["kind"] == "cannot":
        return "Says it can't" + (f" ({' or '.join(alt['gap'])})" if alt.get("gap") else "")
    if alt["kind"] == "statmuse":
        parts = [", ".join(f'"{p}"' for p in alt.get("all", []))]
        parts += ["one of " + " / ".join(f'"{p}"' for p in group) for group in alt.get("any", [])]
        text = "StatMuse link whose question says " + "; ".join(p for p in parts if p)
        return text + (f"; and never {' or '.join(repr(p) for p in alt['none'])}" if alt.get("none") else "")
    target = alt["target"]
    if target["type"] == "terminal":
        window = target.get("window")
        tail = ("" if window in (None, ANY) else ", full season" if window == "season"
                else f", last {window[1:]} games")
        if target["mode"] == "player":
            if "players" in target:
                return "Terminal: " + " vs ".join(who(i) for i in target["players"]) + " compared" + tail
            extra = "".join(f" + {who(i)}" for i in target.get("compare_ids", []))
            return f"Terminal: {who(target['player_id'])}{extra}{tail}"
        if target["mode"] == "team":
            return f"Terminal: my fantasy team {who(target['team_id'])}"
        if target["mode"] == "nba_team":
            return f"Terminal: NBA team {target['nba_team']}"
        return "Terminal: overview"
    text = f"{target['page']} page"
    if "team_id" in target:
        text += f" for {who(target['team_id'])}"
    r = target.get("rankings")
    if target["page"] == "rankings":
        r = r or {}
        bits = []
        if r.get("scope") == ANY:
            bits.append("general or my league's scoring")
        elif r.get("scope"):
            bits.append("my league's scoring")
        if r.get("format") not in (None, ANY):
            bits.append(r["format"])
        if r.get("cats"):
            bits.append("by " + ", ".join(r["cats"]))
        window = r.get("window")
        bits.append("season" if window is None else "last " + " or ".join(str(w) for w in (window if isinstance(window, list) else [window])) + " days")
        if "min_games" in r:
            bits.append(f"min {r['min_games']} games")
        text += " (" + ", ".join(bits) + ")"
    return text


def got(out: dict[str, Any]) -> str:
    """What the router actually did, as one line."""
    if out.get("kind") is None:
        return f"no answer ({out.get('failure') or 'unknown'})"
    if out["kind"] == "statmuse":
        return f'statmuse "{out.get("statmuse_query")}"'
    if out["kind"] == "cannot":
        return f"cannot ({out.get('gap')})" + (f" {out.get('missing')}" if out.get("gap") == "invalid_target" else "")
    target = {k: v for k, v in (out.get("target") or {}).items() if v not in (None, [], {})}
    if isinstance(target.get("rankings"), dict):
        target["rankings"] = {k: v for k, v in target["rankings"].items() if v not in (None, [])}
    return f"show {target}"


def explain(case: dict[str, Any], out: dict[str, Any], names: Optional[dict[Any, str]] = None) -> str:
    wanted = " OR ".join(describe(alt, names) for alt in case["expect"])
    return f"wanted: {wanted} | got: {got(out)}"


# ------------------------------------------------------------- self-checks


def oracle(case: dict[str, Any]) -> dict[str, Any]:
    """An answer built from the case's first expected destination. Must pass."""
    alt = case["expect"][0]
    out: dict[str, Any] = {"kind": alt["kind"], "text": "", "target": None, "statmuse_query": None,
                           "gap": None, "lookups": [], "ungrounded_numbers": 0}
    if alt["kind"] == "statmuse":
        out["statmuse_query"] = " ".join([*alt.get("all", []), *(group[0] for group in alt.get("any", []))])
        out["gap"] = "no_view"
    elif alt["kind"] == "cannot":
        out["gap"] = alt.get("gap", ["no_data"])[0]
    else:
        spec = alt["target"]
        window = spec.get("window")
        if spec["type"] == "terminal":
            ids = spec.get("players") or [spec.get("player_id"), *spec.get("compare_ids", [])]
            out["target"] = {"type": "terminal", "mode": spec["mode"],
                             "player_id": ids[0] if spec["mode"] == "player" else None,
                             "compare_ids": ids[1:] if spec["mode"] == "player" else [],
                             "team_id": spec.get("team_id"), "nba_team": spec.get("nba_team"),
                             "window": None if window in (None, ANY) else window}
        else:
            r = spec.get("rankings", {})
            rank_window = r.get("window")
            out["target"] = {"type": "page", "page": spec["page"], "team_id": spec.get("team_id"),
                             "rankings": {"scope": None if r.get("scope") in (None, ANY) else r["scope"],
                                          "format": None if r.get("format") in (None, ANY) else r["format"],
                                          "window": rank_window[0] if isinstance(rank_window, list) else rank_window,
                                          "cats": list(r.get("cats", [])), "min_games": r.get("min_games")}
                             if spec["page"] == "rankings" else None}
    return out


NULLS: dict[str, dict[str, Any]] = {
    "no answer": {"kind": None},
    "always cannot": {"kind": "cannot", "gap": "no_data", "text": "", "lookups": [], "ungrounded_numbers": 0},
    "always statmuse, empty question": {"kind": "statmuse", "statmuse_query": "", "gap": "no_view", "text": "",
                                        "lookups": [], "ungrounded_numbers": 0},
    "always the overview": {"kind": "show", "text": "", "lookups": [], "ungrounded_numbers": 0,
                            "target": {"type": "terminal", "mode": "overview", "player_id": None, "compare_ids": [],
                                       "team_id": None, "nba_team": None, "window": None}},
    "refused destination": {"kind": "cannot", "gap": "invalid_target", "text": "", "lookups": [],
                            "ungrounded_numbers": 0},
}


# ---------------------------------------------------------------- summaries


def wilson(passed: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% interval for a pass rate. Honest at small n, where +-1.96 SE is not."""
    if n == 0:
        return (0.0, 0.0)
    p = passed / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def _mean(values: Iterable[float]) -> Optional[float]:
    values = list(values)
    return sum(values) / len(values) if values else None


def summarize(rows: list[dict[str, Any]], cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Everything the headline needs, recomputed from the raw rows: per case the
    mean over its reps, then over cases. Truncated rows are counted, not averaged."""
    by_case: dict[str, list[dict[str, Any]]] = {}
    truncated = 0
    for row in rows:
        if row.get("status", "ok") != "ok":
            truncated += 1
            continue
        by_case.setdefault(row["prompt_id"], []).append(row)
    known = {case["id"]: case for case in cases}
    scored = [cid for cid in by_case if cid in known]

    def rate(metric: str, ids: Iterable[str]) -> Optional[dict[str, Any]]:
        per_case = [m for cid in ids
                    if (m := _mean(r["grade"][metric] for r in by_case[cid] if metric in r["grade"])) is not None]
        if not per_case:
            return None
        low, high = wilson(sum(per_case), len(per_case))
        return {"rate": sum(per_case) / len(per_case), "n": len(per_case), "low": low, "high": high}

    groups: dict[str, list[str]] = {}
    for cid in scored:
        groups.setdefault(known[cid]["tags"][0], []).append(cid)

    # Confusion on kind, one vote per (case, rep)
    votes = [(known[cid], row) for cid in scored for row in by_case[cid]]

    def expects(case: dict[str, Any], kind: str) -> bool:
        return any(alt["kind"] == kind for alt in case["expect"])

    confusion: dict[str, Any] = {}
    for kind in ("show", "statmuse", "cannot"):
        said = [(c, r) for c, r in votes if r["output"].get("kind") == kind]
        wanted = [(c, r) for c, r in votes if expects(c, kind)]
        confusion[kind] = {
            "precision": _mean(float(expects(c, kind)) for c, _ in said),
            "recall": _mean(float(r["output"].get("kind") == kind) for _, r in wanted),
            "said": len(said), "wanted": len(wanted),
        }
    stay_home = [(c, r) for c, r in votes if "stay-home" in c["tags"]]
    return {
        "cases": len(scored), "rows": sum(len(by_case[cid]) for cid in scored), "truncated": truncated,
        "missing": sorted(set(known) - set(scored)),
        # How the wait is built: questions answered in a single model call
        "one_call": _mean(float(r.get("model_calls") == 1) for _, r in votes),
        "metrics": {metric: rate(metric, scored) for metric in METRICS},
        "by_group": {group: rate("route_ok", ids) for group, ids in sorted(groups.items())},
        # The router's prompt quotes the answer to a few cases as its own examples;
        # they are regression checks, and this is the score without them
        "unquoted": rate("route_ok", [cid for cid in scored if "in-prompt" not in known[cid]["tags"]]),
        "confusion": confusion,
        "stay_home": {"n": len(stay_home),
                      "leaks": sorted({c["id"] for c, r in stay_home if r["output"].get("kind") == "statmuse"})},
        "invalid_targets": sorted({c["id"] for c, r in votes if r["output"].get("gap") == "invalid_target"}),
        "refusals": sorted({c["id"] for c, r in votes if r["output"].get("failure") == "refusal"}),
        "failed": sorted(cid for cid in scored if _mean(r["grade"]["route_ok"] for r in by_case[cid]) < 1),
    }
