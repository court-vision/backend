"""
Which fixture a box-score row belongs to.

`nba.player_game_stats` records what a player did on a date, not which game he
did it in — there is no game id on the row. The schedule in `nba.games` supplies
the rest, and the join that gets there is (game_date, team): an NBA team plays
at most once a day, so that pair identifies a fixture without a doubleheader to
disambiguate. A traded player is fine too, because the team on the row is
whichever one he represented that day.

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

    def __init__(self, by_date_team: dict[tuple, list]):
        self._by_date_team = by_date_team

    @classmethod
    def for_rows(cls, rows: Iterable) -> "GameContext":
        rows = list(rows)
        by_date_team: dict[tuple, list] = {}
        if not rows:
            return cls(by_date_team)

        teams = {r.team_id for r in rows if r.team_id}
        dates = {r.game_date for r in rows}
        if not teams or not dates:
            return cls(by_date_team)

        for game in Game.select().where(
            Game.game_date.in_(list(dates))
            & (Game.home_team.in_(list(teams)) | Game.away_team.in_(list(teams)))
        ):
            for team_id in (game.home_team_id, game.away_team_id):
                by_date_team.setdefault((game.game_date, team_id), []).append(game)
        return cls(by_date_team)

    def of(self, row) -> dict:
        """`{game_id, home, opponent}` for one row, or `{}` when unresolvable.

        Returned as a dict so callers can splat it into a `GameLog(...)` and let
        the model's own defaults stand in when the fixture is unknown.
        """
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

