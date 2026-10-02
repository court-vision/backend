#!/usr/bin/env python
"""
Builds the two pages a person reads to decide whether to trust this eval. Free:
no model, no database.

    .venv/bin/python -m evals.ai_route.review                 # inputs.html: every case and what should happen
    .venv/bin/python -m evals.ai_route.review --variant baseline   # graded.html: what did happen, and the grade

Both land in .claude/hillclimb/ai-route/. Each is one static file that loads
nothing from the network. Every value from disk -- case text, model output,
tool results -- is escaped: the pages show data, they never run it.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.ai_route import grade as grading  # noqa: E402
from evals.ai_route.run import FLOW, check_bars, cost_usd, load_cases, load_state, read_jsonl  # noqa: E402

GROUPS = {
    "terminal": "Open the terminal",
    "rankings": "Open the rankings page",
    "page": "Open another page",
    "statmuse": "Link out to StatMuse",
    "cannot": "Say it can't",
}
CSS = """
:root { color-scheme: light dark; --line: #8884; --dim: #888; --ok: #2a8a4a; --bad: #c0392b; --chip: #8882; }
* { box-sizing: border-box; }
body { font: 15px/1.5 system-ui, -apple-system, sans-serif; margin: 0 auto; padding: 16px; max-width: 860px;
       background: Canvas; color: CanvasText; }
h1 { font-size: 22px; margin: 0 0 4px; } h2 { font-size: 17px; margin: 28px 0 8px; }
p { margin: 6px 0; } .dim { color: var(--dim); } .lead { border-left: 3px solid var(--line); padding-left: 10px; }
.case { border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; margin: 8px 0; }
.q { font-weight: 600; font-size: 16px; overflow-wrap: anywhere; }
.id { font: 12px ui-monospace, monospace; color: var(--dim); }
.chip { display: inline-block; font-size: 11px; padding: 0 6px; border-radius: 9px; background: var(--chip); margin-left: 4px; }
.chip.judgment { background: #e6a70033; } .row { margin-top: 4px; overflow-wrap: anywhere; }
.k { color: var(--dim); } .pass { color: var(--ok); font-weight: 600; } .fail { color: var(--bad); font-weight: 600; }
.case.failed { border-color: var(--bad); }
details { margin-top: 6px; } summary { cursor: pointer; color: var(--dim); font-size: 13px; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; font: 12px/1.45 ui-monospace, monospace; background: var(--chip);
      padding: 8px; border-radius: 6px; margin: 4px 0; }
table { border-collapse: collapse; margin: 8px 0; } td, th { padding: 2px 12px 2px 0; text-align: left; vertical-align: top; }
"""
CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def context_words(context: dict[str, Any], names: dict[int, str]) -> str:
    """Where the asker is, in words."""
    where = context.get("page", "somewhere")
    if context.get("mode"):
        where += f", {context['mode'].replace('_', ' ')} view"
    bits = [where]
    if context.get("player_id"):
        bits.append(f"looking at {names.get(context['player_id'], context['player_id'])}")
    if context.get("compare_ids"):
        bits.append("comparing with " + ", ".join(str(names.get(i, i)) for i in context["compare_ids"]))
    if context.get("nba_team"):
        bits.append(f"NBA team {context['nba_team']}")
    if context.get("window"):
        bits.append("window " + ("full season" if context["window"] == "season" else f"last {context['window'][1:]} games"))
    if context.get("team_id"):
        bits.append(f"selected team {names.get(context['team_id'], context['team_id'])}")
    return "; ".join(bits)


def page(title: str, body: list[str]) -> str:
    return ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width, initial-scale=1'>"
            f"<meta http-equiv=Content-Security-Policy content=\"{CSP}\">"
            f"<title>{esc(title)}</title><style>{CSS}</style></head><body>" + "".join(body) + "</body></html>")


def case_head(case: dict[str, Any]) -> str:
    chips = "".join(f"<span class='chip {esc(tag) if tag == 'judgment' else ''}'>{esc(tag)}</span>" for tag in case["tags"][1:])
    return f"<div class=id>{esc(case['id'])}{chips}</div><div class=q>{esc(case['question'])}</div>"


def expected_rows(case: dict[str, Any], names: dict[int, str]) -> str:
    wanted = "<br><span class=k>or</span> ".join(esc(grading.describe(alt, names)) for alt in case["expect"])
    rows = f"<div class=row><span class=k>On screen:</span> {esc(context_words(case['context'], names))}</div>"
    if case.get("season") == "in_progress":
        rows += "<div class=row><span class=k>Season:</span> 2026-27 under way (every other case is the preseason)</div>"
    rows += f"<div class=row><span class=k>Should:</span> {wanted}</div>"
    if "max_lookups" in case:
        n = case["max_lookups"]
        rows += ("<div class=row><span class=k>Trips:</span> "
                 + ("one model call, nothing looked up" if n == 0
                    else f"at most {n} lookup{'s' if n > 1 else ''}, made together") + "</div>")
    if case.get("note"):
        rows += f"<div class='row dim'>{esc(case['note'])}</div>"
    return rows


def build_inputs(cases: list[dict[str, Any]], names: dict[int, str], teams: list[dict[str, Any]]) -> str:
    judgment = [case for case in cases if "judgment" in case["tags"]]
    body = [
        "<h1>Routing test set: the questions</h1>",
        f"<p class=dim>{len(cases)} questions for POST /ai/route. Built {esc(datetime.now().strftime('%Y-%m-%d %H:%M'))}. "
        "Nothing has been run yet.</p>",
        "<p class=lead>Each card is one question the router will be asked, where the asker is when they ask it, and "
        "every destination that counts as right. <b>What to check:</b> are these the questions people will really ask, "
        "is anything important missing, and is any expected destination wrong? Answer in chat by the grey id.</p>",
        "<p>The asker is a made-up user with two fantasy teams: "
        + " and ".join(f"<b>{esc(t['team_name'])}</b> ({esc(t['scoring'])}, {esc(t['provider'])}, league “{esc(t['league_name'])}”)"
                       for t in teams)
        + ". The first is the selected team unless a card says otherwise.</p>",
        f"<p><span class='chip judgment'>judgment</span> marks {len(judgment)} cards where the right answer is a product "
        "decision rather than a fact. Those are the ones to look at first.</p>",
    ]
    for key, title in GROUPS.items():
        group = [case for case in cases if case["tags"][0] == key]
        body.append(f"<h2>{esc(title)} <span class=dim>({len(group)})</span></h2>")
        body += [f"<div class=case>{case_head(case)}{expected_rows(case, names)}</div>" for case in group]
    return page("Routing test set: the questions", body)


def _transcript(variant: str, row: dict[str, Any]) -> str:
    path = FLOW / variant / "traces" / f"{row['prompt_id']}_rep{row['rep']}.json"
    if not path.is_file() or path.is_symlink():
        return ""
    turns = json.loads(path.read_text())
    # The system prompt is the same on every case; it is shown once at the top of the page
    parts = [f"<div class=k>{esc(turn['role'] + (' ' + turn['name'] if turn.get('name') else ''))}</div>"
             f"<pre>{esc(turn['content'])}</pre>" for turn in turns if turn["role"] != "system"]
    return "<details><summary>Full exchange</summary>" + "".join(parts) + "</details>"


def build_graded(variant: str, cases: list[dict[str, Any]], names: dict[int, str], state: dict[str, Any]) -> str:
    rows = read_jsonl(FLOW / variant / "results.jsonl")
    errors = read_jsonl(FLOW / variant / "errors.jsonl")
    by_case: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_case.setdefault(row["prompt_id"], []).append(row)
    summary = grading.summarize(rows, cases)
    labels = {m["id"]: m["label"] for m in state["metrics"]}
    total = sum(cost_usd(r, state["prices"]) for r in [*rows, *errors])

    def pct(m: Any) -> str:
        return "n/a" if m is None else f"{m['rate']:.0%} ({m['rate'] * m['n']:.0f} of {m['n']}; 95% range {m['low']:.0%}–{m['high']:.0%})"

    scored = len(by_case)
    body = [
        f"<h1>Routing test set: results ({esc(variant)})</h1>",
        f"<p class=dim>{scored} of {len(cases)} questions run on {esc(', '.join(sorted({r['model'] for r in rows})) or 'no model')}, "
        f"{len(errors)} failed attempts not scored, ${total:.3f} spent. Built {esc(datetime.now().strftime('%Y-%m-%d %H:%M'))}.</p>",
        "<p class=lead>Each card shows what the router did and whether the grader counted it right. <b>What to check:</b> "
        "would you have scored any of these differently? Failures are listed first.</p>",
        "<table>" + "".join(f"<tr><th>{esc(labels[m])}</th><td>{esc(pct(summary['metrics'][m]))}</td></tr>"
                            for m in grading.METRICS) + "</table>",
        f"<p class=dim>Right place without the {sum('in-prompt' in c['tags'] for c in cases)} questions the router's "
        f"instructions quote as examples: {esc(pct(summary['unquoted']))}.</p>",
        "<table>" + "".join(f"<tr><th>{esc(GROUPS.get(g, g))}</th><td>{esc(pct(m))}</td></tr>"
                            for g, m in summary["by_group"].items()) + "</table>",
        f"<p>Fantasy questions sent to StatMuse: <b>{len(summary['stay_home']['leaks'])}</b> of {summary['stay_home']['n']}. "
        f"Destinations the server refused: <b>{len(summary['invalid_targets'])}</b>.</p>",
    ]
    ok = [r for r in rows if r.get("status", "ok") == "ok"]
    bars = check_bars(state.get("bars", {}), summary, sorted(r["latency_s"] for r in ok))
    if bars:
        body.append("<h2>Bars</h2><table>" + "".join(
            f"<tr><td><span class={'pass' if passed else 'fail'}>{'PASS' if passed else 'FAIL'}</span></td>"
            f"<th>{esc(name)}</th><td>{esc(detail)}</td></tr>" for name, passed, detail in bars) + "</table>")
    if ok:
        def spread(sample: list[dict[str, Any]]) -> str:
            times = sorted(r["latency_s"] for r in sample)
            at = lambda q: times[min(len(times) - 1, round(q * (len(times) - 1)))]  # noqa: E731
            return f"half under {at(0.5):.1f}s, 9 in 10 under {at(0.9):.1f}s, slowest {times[-1]:.1f}s"
        body.append("<h2>Time to answer</h2><p class=dim>From the question arriving to the destination being ready, "
                    "measured in this run. Each lookup adds a second trip to the model.</p><table>"
                    + f"<tr><th>All {len(ok)}</th><td>{esc(spread(ok))}</td></tr>"
                    + "".join(f"<tr><th>{n} model call{'s' if n > 1 else ''} ({len(g)})</th><td>{esc(spread(g))}</td></tr>"
                              for n in sorted({r['model_calls'] for r in ok})
                              if (g := [r for r in ok if r['model_calls'] == n]))
                    + "".join(f"<tr><th>Within {cut}s</th><td>{sum(r['latency_s'] <= cut for r in ok)} of {len(ok)}</td></tr>"
                              for cut in (3, 5, 8))
                    + "</table>")
    system = next((json.loads(p.read_text())[0]["content"] for p in sorted((FLOW / variant / "traces").glob("*.json"))
                   if p.is_file() and not p.is_symlink()), None)
    if system:
        body.append(f"<details><summary>The router's instructions (the same on every question)</summary><pre>{esc(system)}</pre></details>")

    def passed(case: dict[str, Any]) -> bool:
        return all(r["grade"]["route_ok"] == 1 for r in by_case[case["id"]])

    ran = [case for case in cases if case["id"] in by_case]
    for title, group in (("Not passing", [c for c in ran if not passed(c)]), ("Passing", [c for c in ran if passed(c)])):
        body.append(f"<h2>{esc(title)} <span class=dim>({len(group)})</span></h2>")
        for case in group:
            card = [case_head(case), expected_rows(case, names)]
            for row in by_case[case["id"]]:
                out = row["output"]
                verdict = "<span class=pass>PASS</span>" if row["grade"]["route_ok"] == 1 else "<span class=fail>FAIL</span>"
                flags = [labels[m] for m in grading.METRICS[1:] if row["grade"].get(m) == 0]
                card.append(f"<div class=row>{verdict} <span class=k>Did:</span> {esc(grading.got(out))}</div>")
                if out.get("text"):
                    card.append(f"<div class=row><span class=k>Said:</span> “{esc(out['text'])}”</div>")
                if out.get("statmuse_url"):
                    card.append(f"<div class='row dim'>{esc(out['statmuse_url'])}</div>")
                if flags:
                    card.append(f"<div class=row><span class=fail>Also failed:</span> {esc(', '.join(flags))}</div>")
                looked = ", ".join(f"{c['name']}({json.dumps(c['input'], ensure_ascii=False)})" for c in out.get("lookups") or [])
                card.append(f"<div class='row dim'>{row['model_calls']} model calls, {esc(looked or 'no lookups')}, "
                            f"{row['latency_s']:.1f}s, ${cost_usd(row, state['prices']):.4f}</div>")
                card.append(_transcript(variant, row))
            body.append(f"<div class='case{'' if passed(case) else ' failed'}'>{''.join(card)}</div>")
    if errors:
        body.append(f"<h2>Failed attempts, not scored <span class=dim>({len(errors)})</span></h2>")
        body += [f"<div class=case><span class=id>{esc(e['prompt_id'])}</span> {esc(e['class'])} {esc(e['code'])}: "
                 f"{esc(e.get('message', ''))}</div>" for e in errors]
    return page(f"Routing test set: results ({variant})", body)


def main() -> None:
    from evals.ai_route import fixtures

    parser = argparse.ArgumentParser(description="Build the eval's review pages (free)")
    parser.add_argument("--variant", help="build graded.html for this variant instead of inputs.html")
    args = parser.parse_args()
    cases = load_cases()
    FLOW.mkdir(parents=True, exist_ok=True)
    if args.variant:
        target = FLOW / f"graded-{args.variant}.html"
        target.write_text(build_graded(args.variant, cases, fixtures.NAMES, load_state()))
    else:
        target = FLOW / "inputs.html"
        target.write_text(build_inputs(cases, fixtures.NAMES, fixtures.TEAMS))
    print(target)


if __name__ == "__main__":
    main()
