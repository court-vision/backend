# Routing eval

Does `POST /v1/internal/ai/route` take a question to the right place?

Each case in `cases.jsonl` is a question, where the asker is when they ask it
(`context`), and every destination that counts as right (`expect`). The runner
sends each one through the real `AiService.route` and `grade.py` scores the
answer. Nothing is judged by a model: a destination is right or it isn't.

| File | What it is |
|---|---|
| `cases.jsonl` | The questions and their expected destinations |
| `grade.py` | The grader. Pure: no model, no database |
| `fixtures.py` | What is pinned: the asker's two teams, the season line, quota, the question log |
| `run.py` | The runner. **A paid run costs about $0.01 a question** |
| `review.py` | Builds the pages a person reads: the questions, and the graded results |

```bash
.venv/bin/python -m evals.ai_route.run --selfcheck        # free
.venv/bin/python -m evals.ai_route.review                 # free: inputs.html
.venv/bin/python -m evals.ai_route.run --approve-harness  # yours to run, after reviewing
.venv/bin/python -m evals.ai_route.run                    # paid, capped by --max-usd
.venv/bin/python -m evals.ai_route.review --variant baseline
```

Output lands in `.claude/hillclimb/ai-route/<variant>/`. Player lookups read the
database in `.env`; nothing is written to it.

## Scores

- **Right place** (the headline): the answer matched one of the case's destinations.
- **Right kind**: show, StatMuse or can't, whatever the details.
- **No made-up #s**: the one line of text states no number that isn't in the question or the target.
- **No extra trips**: no lookup beyond what the answer needs (`max_lookups` on the case), and the
  ones it needs made together. Each lookup round is a second model call and doubles the wait.

A fantasy question sent to StatMuse (a "leak") and a destination the server
refused (`invalid_target`) are counted separately; both should be zero.

`_state.json` holds the agreed pass/fail lines (`bars`): routing and time are judged separately.
Time is measured per question, from the request arriving to the destination being ready.

After changing a case or the grader, `--regrade` grades the stored answers again for free; a row
whose question was reworded is dropped so the next run asks it again.

## Adding a case

Append a line to `cases.jsonl`, run `--selfcheck` and
`pytest tests/unit/test_ai_route_eval.py`, then re-approve the harness. Real
questions from `usr.ai_questions` are the best source once the feature has users.
