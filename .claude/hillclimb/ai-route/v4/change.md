The version to ship: v2 on Claude Opus 5.5 at low effort, plus a two-trip cap and a rankings fix found by v2 and v3.

**Why.** v2 passed every bar. Its one routing miss, and v3's, was a category filter sent without
the categories format, which /rankings ignores. v3 (Sonnet 5.5) also asked for the same lookup up
to three times in a row, four trips to the model where two would do.

**What changed since v2.**

1. `_tidy` (services/ai/routing.py): a rankings target with `cats` gets `format: "categories"`.
2. `ROUTER_MAX_MODEL_CALLS = 2` (services/ai/service.py): one round of lookups, then the answer.
   v2 never went past two calls, so this only bounds what it already did.
3. Defaults (core/settings.py): `ai_model = "claude-opus-5-5"`, `ai_effort = "low"`.

**What to watch.** Every bar as in v2; `rank-blocks-2-weeks` should now pass.
