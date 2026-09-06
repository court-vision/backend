"""
Which fixture a box-score row belongs to.

`nba.player_game_stats` carries a `game_id` since migration 0019, so the usual
path is a lookup by that id and nothing is inferred at all.

Rows written before it — or whose game is genuinely missing from `nba.games` —
have no id, and those fall back to the join the whole table used to need:
(game_date, team). That pair identifies a fixture because an NBA team plays at
most once a day, which is a fact about the sport rather than a guarantee of the
schema, and is exactly why the id is stored now.

Batched on purpose. Resolving per row would be one query per game in a log; one
query covers the whole log, and the result is a dict the caller reads from.

Ambiguity resolves to nothing rather than to a guess: if the schedule somehow
offers two candidates for a (date, team), the row comes back without context. An
absent opponent costs a label; a wrong one names the wrong team.
"""

from typing import Iterable

from db.models.nba.games import Game


class GameContext:
    """Fixture lookup for a set of box-score rows, resolved in one query."""

    def __init__(self, by_date_team: dict[tuple, list], by_id: dict | None = None):
        self._by_date_team = by_date_team
        self._by_id = by_id or {}

    @classmethod
    def for_rows(cls, rows: Iterable) -> "GameContext":
        rows = list(rows)
        by_id: dict[str, object] = {}
        by_date_team: dict[tuple, list] = {}
        if not rows:
            return cls(by_date_team, by_id)

        ids = {r.game_id for r in rows if getattr(r, "game_id", None)}
        if ids:
            for game in Game.select().where(Game.game_id.in_(list(ids))):
                by_id[game.game_id] = game

        # Only the rows the id could not answer for need the old join, so a
        # table that has been through the backfill does not pay for it at all.
        # A row whose stored id found no game counts as one of them: the FK is
        # ON DELETE SET NULL so it should not happen, but if it ever does, the
        # fallback has to have been loaded or `of` has nothing to fall back to.
        legacy = [r for r in rows if by_id.get(getattr(r, "game_id", None)) is None]
        teams = {r.team_id for r in legacy if r.team_id}
        dates = {r.game_date for r in legacy}
        if teams and dates:
            for game in Game.select().where(
                Game.game_date.in_(list(dates))
                & (Game.home_team.in_(list(teams)) | Game.away_team.in_(list(teams)))
            ):
                for team_id in (game.home_team_id, game.away_team_id):
                    by_date_team.setdefault((game.game_date, team_id), []).append(game)
        return cls(by_date_team, by_id)

    def of(self, row) -> dict:
        """`{game_id, home, opponent}` for one row, or `{}` when unresolvable.

        Returned as a dict so callers can splat it into a `GameLog(...)` and let
        the model's own defaults stand in when the fixture is unknown.
        """
        game = self._by_id.get(getattr(row, "game_id", None))
        if game is None:
            # No stored id: fall back to the inference, which refuses to guess
            # when the schedule offers more than one candidate.
            candidates = self._by_date_team.get((row.game_date, row.team_id), [])
            if len(candidates) != 1:
                return {}
            game = candidates[0]
        home = game.home_team_id == row.team_id
        return {
            "game_id": game.game_id,
            "home": home,
            "opponent": game.away_team_id if home else game.home_team_id,
        }

