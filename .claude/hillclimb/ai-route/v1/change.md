Fewer trips to the model, and less thinking per trip: lookups only when the answer needs them, effort low instead of medium.

**Why.** The baseline routes well (90 of 91 right) but is slow: half of answers took over 4 s, and a
question that makes a lookup takes about 6 s against 3 s for one that doesn't. Twelve questions made
a lookup they didn't need, ten of them `get_my_teams` when the team was already selected or the
question had nothing to do with a team ("playoff picture", "open my watchlist").

**What changed.**

1. `ROUTER_PROMPT` (services/ai/prompts.py):
   - Lookups: make only the ones the answer needs, all in one step. "My team" means the `team_id`
     in the view; `get_my_teams` only when a team or league is named, a different team is asked
     for, or no team is selected. A StatMuse question needs names, not IDs.
   - Pages: leave `team_id` null to use the selected team.
   - The player view's description now mentions the injury status it shows (the one baseline miss:
     "is Embiid hurt?" was answered "can't").
2. `get_my_teams` tool description (services/ai/tools.py): the same rule.
3. Run with `--effort low` (production: `AI_EFFORT=low`). Baseline ran at `medium`.

**What to watch.** Right place must not drop; "No extra trips" and the time bars should improve.
Two changes ride together: if routing gets worse, a second run at `--effort medium` with this prompt
separates them.
