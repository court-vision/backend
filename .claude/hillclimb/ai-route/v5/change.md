The view lists each of the user's teams with its scoring format, and a first name everyone reads one way is written out in full.

**Why.** "Best streamers for my categories league" took two trips to the model (about 6 s): the
router had to call `get_my_teams` to learn which team is the categories one. In v1 and v3 it
skipped that call and opened the selected points team instead. And in v4 "who does Luka play
next?" was passed on as "Luka", so the server asked "Luka Dončić or Luka Garza?".

**What changed since v4.**

1. The view sent to the model carries `teams`: every fantasy team the caller has, as
   `team_id`, `scoring`, `provider`, `season`, with the selected one marked. No names: those are
   league data users wrote, and the view is prompt text. One cheap database read per request; if
   it fails the router falls back to looking teams up.
2. `ROUTER_PROMPT`: a described team ("my categories league", "my Yahoo team", "my other team")
   is the `team_id` that fits, with no lookup; `get_my_teams` only for a team or league called by
   its name. Write a player's full name whenever it's clear who is meant; pass a bare surname on
   only when it could be several current players.
3. Cases: the five described-team questions now allow no lookup, and four questions are new
   (`streamers-cats-selected`, `streamers-yahoo-league`, `matchup-points-from-cats`,
   `player-first-name`).

**What to watch.** The described-team questions should pass in one model call, and
`player-next-game` should open Luka Dončić. Nothing that passed in v4 should fail.
