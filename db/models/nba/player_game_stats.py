"""
Player Game Stats Fact Table

Per-game statistics for NBA players. This is a fact table that stores
one row per player per game played. Replaces the denormalized
stats_s2.daily_player_stats table.
"""

from datetime import datetime
from uuid import UUID

from peewee import (
    AutoField,
    IntegerField,
    CharField,
    DateField,
    DateTimeField,
    SmallIntegerField,
    UUIDField,
    ForeignKeyField,
)

from db.base import BaseModel
from db.models.nba.games import Game
from db.models.nba.players import Player
from db.models.nba.teams import NBATeam


class PlayerGameStats(BaseModel):
    """
    Per-game statistics for a player.

    This fact table stores box score statistics for each game a player
    participates in. References the Player and NBATeam dimension tables.

    Attributes:
        id: Auto-incrementing primary key
        player: Foreign key to Player dimension
        team: Foreign key to NBATeam dimension (team player played for)
        game_date: Date of the game
        fpts: Fantasy points (calculated)
        pts, reb, ast, stl, blk, tov: Basic counting stats
        min: Minutes played
        fgm, fga, fg3m, fg3a, ftm, fta: Shooting stats
        pipeline_run_id: Reference to the pipeline run that created/updated this record
        created_at: When this record was first created
        updated_at: When this record was last modified
    """

    id = AutoField(primary_key=True)
    player = ForeignKeyField(
        Player,
        backref="game_stats",
        on_delete="CASCADE",
        column_name="player_id",
    )
    team = ForeignKeyField(
        NBATeam,
        backref="player_game_stats",
        on_delete="RESTRICT",
        column_name="team_id",
        null=True,  # Allow null for trades/unknown
    )
    game_date = DateField(index=True)

    # The fixture this line belongs to. NULL only when nba.games has no
    # matching game; readers fall back to a (game_date, team) lookup for those.
    # See migration 0019 — the row's identity is the game, not the date.
    game = ForeignKeyField(
        Game,
        backref="player_stats",
        on_delete="SET NULL",
        column_name="game_id",
        null=True,
        index=True,
    )

    # Fantasy points (calculated based on league scoring)
    fpts = SmallIntegerField()

    # Basic counting stats
    pts = SmallIntegerField()
    reb = SmallIntegerField()
    ast = SmallIntegerField()
    stl = SmallIntegerField()
    blk = SmallIntegerField()
    tov = SmallIntegerField()
    min = IntegerField()

    # Shooting stats
    fgm = SmallIntegerField()
    fga = SmallIntegerField()
    fg3m = SmallIntegerField()
    fg3a = SmallIntegerField()
    ftm = SmallIntegerField()
    fta = SmallIntegerField()

    # Audit columns
    pipeline_run_id = UUIDField(null=True, index=True)
    created_at = DateTimeField(default=datetime.utcnow)
    updated_at = DateTimeField(default=datetime.utcnow)

    class Meta:
        table_name = "player_game_stats"
        schema = "nba"
        indexes = (
            # One row per player per game. A row with no game falls back to the
            # date, as a partial unique index migration 0019 creates — Peewee
            # cannot express `WHERE game_id IS NULL`, so it is not listed here.
            (("player", "game"), True),
            # Index for querying by date range
            (("game_date",), False),
            # Index for querying by team
            (("team",), False),
        )

    def __repr__(self) -> str:
        return (
            f"<PlayerGameStats("
            f"player_id={self.player_id}, "
            f"date={self.game_date}, "
            f"fpts={self.fpts})>"
        )

    def save(self, *args, **kwargs):
        """Override save to auto-update updated_at timestamp."""
        self.updated_at = datetime.utcnow()
        return super().save(*args, **kwargs)

    @classmethod
    def upsert_game_stats(
        cls,
        player_id: int,
        game_date,
        stats: dict,
        team_id: str | None = None,
        pipeline_run_id: UUID | None = None,
        game_id: str | None = None,
    ) -> "PlayerGameStats":
        """
        Insert or update game statistics for a player.

        Keyed on the game when one is known and on the date otherwise, which is
        exactly what the table's two unique indexes enforce (migration 0019).
        Keying on the game alone would be wrong for a row without one: a NULL is
        distinct from every other NULL in Postgres, so the lookup would never
        match and every run would insert a duplicate.

        Args:
            player_id: NBA player ID
            game_date: Date of the game
            stats: Dictionary with stat values (pts, reb, ast, etc.)
            team_id: Optional team abbreviation
            pipeline_run_id: Optional pipeline run UUID
            game_id: NBA game id, when the source knows it

        Returns:
            The created or updated PlayerGameStats instance
        """
        defaults = {
            "team": team_id,
            "fpts": stats.get("fpts", 0),
            "pts": stats.get("pts", 0),
            "reb": stats.get("reb", 0),
            "ast": stats.get("ast", 0),
            "stl": stats.get("stl", 0),
            "blk": stats.get("blk", 0),
            "tov": stats.get("tov", 0),
            "min": stats.get("min", 0),
            "fgm": stats.get("fgm", 0),
            "fga": stats.get("fga", 0),
            "fg3m": stats.get("fg3m", 0),
            "fg3a": stats.get("fg3a", 0),
            "ftm": stats.get("ftm", 0),
            "fta": stats.get("fta", 0),
            "pipeline_run_id": pipeline_run_id,
        }

        # An id nba.games has never heard of cannot be stored — the column is a
        # foreign key, and inserting it would fail the whole row rather than
        # lose one label. The schedule pipeline may simply not have reached this
        # game yet, so the row lands keyed by date and a later run promotes it.
        if game_id is not None and not Game.select().where(Game.game_id == game_id).exists():
            game_id = None

        defaults["game_date"] = game_date
        defaults["game_id"] = game_id

        # Find the row this line belongs to before deciding to write a new one.
        # By game first: a game whose date was corrected keeps its row rather
        # than gaining a second. Then by date, which is what finds a row written
        # before its game id was known — that row is promoted in place, and
        # skipping this step is how the same line ends up stored twice, since
        # (player, NULL) and (player, game) are distinct to both unique indexes.
        game_stats = None
        if game_id is not None:
            game_stats = cls.get_or_none(cls.player == player_id, cls.game == game_id)
        if game_stats is None:
            game_stats = cls.get_or_none(cls.player == player_id, cls.game_date == game_date)

        if game_stats is None:
            return cls.create(player_id=player_id, **defaults)

        for key, value in defaults.items():
            setattr(game_stats, key, value)
        game_stats.save()
        return game_stats

    @classmethod
    def get_player_games(
        cls,
        player_id: int,
        limit: int = 10,
    ) -> list["PlayerGameStats"]:
        """
        Get recent game stats for a player.

        Args:
            player_id: NBA player ID
            limit: Maximum number of games to return

        Returns:
            List of PlayerGameStats ordered by date descending
        """
        return list(
            cls.select()
            .where(cls.player_id == player_id)
            .order_by(cls.game_date.desc())
            .limit(limit)
        )

    @classmethod
    def get_games_by_date(cls, game_date) -> list["PlayerGameStats"]:
        """
        Get all player stats for a specific date.

        Args:
            game_date: Date to query

        Returns:
            List of all PlayerGameStats for that date
        """
        return list(
            cls.select()
            .where(cls.game_date == game_date)
            .order_by(cls.fpts.desc())
        )
