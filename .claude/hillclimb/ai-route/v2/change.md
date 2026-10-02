The router names players and the server finds them: no player lookup, no player ID for the model to get wrong.

**Why.** In v1 the router skipped a lookup and guessed a player ID from memory ("show me Jokic" came
back as his ESPN ID, which the server refused). And every question about a player cost two trips to
the model, about 5.4 s against 2.4 s, only to turn a name into an ID.

**What changed.**

1. The answer the model fills (`ANSWER_SCHEMA`, services/ai/routing.py) takes `player` and `compare`
   as names. `validate` turns them into IDs: players on the caller's screen by name with no query,
   the rest with one query to nba.players (`_find_players`). The response is unchanged: `target`
   still carries `player_id` and `compare_ids`.
2. A name that fits several players gets a server-written "Which Thompson do you mean: ...?"
   (`gap: "ambiguous"`, new). A name that fits none gets "I couldn't find a player called X."
3. The view sent to the model names players and no longer carries their IDs.
4. `ROUTER_PROMPT`: players need no lookup; `search_players` only for a fact about a player that
   decides the destination (his current NBA team). `get_my_teams` when the user names **or
   describes** a team or league ("my 9-cat league") -- the wording v1 got wrong on three questions.
   A comparison on any stat other than fantasy points is a StatMuse question.
5. Run with `--effort low`, on the model named in the run.

**What to watch.** v1's five misses (a guessed ID, three "my 9-cat league" questions, one StatMuse
comparison) should pass; player questions should take one model call.
