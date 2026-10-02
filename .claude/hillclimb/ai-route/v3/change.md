The same code as v2, served by Claude Sonnet 5.5 instead of Claude Opus 5.5 (effort low).

**Why.** Sonnet 5.5 is half Opus 5.5's price per token. Routing is a small, well-specified task; if
Sonnet routes as well and as fast, it is the cheaper way to run it.

**What changed.** Only `--model claude-sonnet-5-5`. No code, prompt, tool or schema change from v2.

**What to watch.** Right place and the three zeros against v2; time per question; cost per question.
