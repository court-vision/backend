# App map eval

Does Dime know where things are in Court Vision? Step 5 of the app map plan
("AI knows the whole app"): the checks that say whether `find_in_app` and
`show_place` send a user to the right place, and whether an answer names a
place the map doesn't have.

Each case in `cases.jsonl` is a question a user asks about the app and the
places that answer it. A place is a desk and a regex over the steps
`find_in_app` writes ("Week › Market P › ONE DAY"), so a case names the control,
not a screen id (ids change with the crawl's sample data). `absent: true` marks
something the app can't do. Nothing is judged by a model.

| File | What it is |
|---|---|
| `cases.jsonl` | The questions and their places |
| `grade.py` | The grader. Pure: no model, no database |
| `run.py` | The runner and its free checks |
| `generated.py` | Questions written per screen by a small model (paid, cached) |
| `answers.py` | The cases asked of Dime end to end (paid) |
| `maps.py` | Which map: a workflow artifact, or one stored in the database |

```bash
.venv/bin/python -m evals.app_map.run --selfcheck                         # free
.venv/bin/python -m evals.app_map.run --retrieval --map app-map.json      # free
.venv/bin/python -m evals.app_map.run --approve-harness                   # after reading the cases
.venv/bin/python -m evals.app_map.run --generated --map app-map.json --variant g1              # about $0.30
.venv/bin/python -m evals.app_map.run --answers --map app-map.json --variant a1 --max-usd 5    # about $3
```

`app-map.json` is the App Map workflow's artifact (`app-map-<sha>`, kept 30
days, on any run in court-vision/frontend's Actions). Without `--map` the
runner reads the database in `.env`: `--build <sha>`, or the newest map.
Nothing is written to it. Paid runs need `ANTHROPIC_API_KEY`.

## The checks

- **Retrieval** (free): each case's question, word for word, through the
  search; a hit is a matching screen in the top 3. In production the model
  writes the query and usually adds the app's words, so this is a floor.
- **Generated** (§ 8 of the plan): a small model writes three questions for
  every screen, and each must find its own screen in the top 3. The questions
  are cached by the screen's fingerprint, so a new crawl pays only for screens
  that changed. Results go to `.claude/hillclimb/app-map/<variant>/`.
- **Answers**: each case asked of Dime through the real turn (prompt, tools,
  loop, show_place, the place check) on production's model. `find_in_app`
  reads the eval's map; every other lookup answers that it isn't part of the
  eval. A pass called `find_in_app`, showed a matching place (or, for an
  `absent` case, said the app can't and showed none), and named no place the
  map doesn't have. Each row keeps the queries Dime wrote, which are the
  queries to tune the search against next.

A paid run names a new `--variant` per measurement and refuses to add to rows
of another model, effort or map. It refuses to start until the harness (the
runner, the grader and the cases) is approved, so a score can't come from a
grader that changed underneath it.

## Scores so far

Retrieval on the Nov 5 map (7f54393, 168 screens): 18 of 25 before the search
changes in the same PR, 21 of 25 after. The four misses are honest: "schedule
a pickup for a later day" (the crawl never shows the scheduling words; they
are in the copy no screen showed), "set my lineup for tomorrow" (finds the
auto-lineup setting first), "start a mock draft" (4th), and "how Dime's moves
worked out" (finds What Dime knows first).

## Adding a case

Append a line to `cases.jsonl` (a real question from dogfooding is the best
kind), run `--selfcheck`, `--retrieval` and
`pytest tests/unit/test_app_map_eval.py`, then approve the harness again.
