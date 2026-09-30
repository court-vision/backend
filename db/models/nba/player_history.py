"""
Player History Table
One row per player per NBA regular season: totals, age and usage, from nba_api's
LeagueDashPlayerStats. The Court Vision projection reads the last three seasons
of it and fits its age curve on all of them. Written by the data-platform
season-history pipeline; backend reads.

Not nba.player_season_stats on purpose: that table's baseline walk-back would
put anyone with an old row back on the draft board, retired or not.
"""

from datetime import datetime

from peewee import CharField, CompositeKey, DateTimeField, DecimalField, IntegerField, SmallIntegerField, UUIDField

from db.base import BaseModel


class PlayerHistory(BaseModel):
    """
    A player's regular-season totals. `player_id` is nba_api's PLAYER_ID — the
    nba.players.id space — without a foreign key: history holds players who
    never reached nba.players.
    """

    # Counting columns, all season totals.
    TOTAL_KEYS = ("pts", "reb", "ast", "stl", "blk", "tov", "fgm", "fga",
                  "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "dd2", "td3")

    player_id = IntegerField()
    season = CharField(max_length=7)                    # '2025-26'
    player_name = CharField(max_length=100)
    team = CharField(max_length=5, null=True)           # last team that season
    age = DecimalField(max_digits=4, decimal_places=1, null=True)
    from_year = SmallIntegerField(null=True)            # first NBA season (start year)
    gp = SmallIntegerField()
    min = DecimalField(max_digits=7, decimal_places=1, default=0)
    pts = SmallIntegerField(default=0)
    reb = SmallIntegerField(default=0)
    ast = SmallIntegerField(default=0)
    stl = SmallIntegerField(default=0)
    blk = SmallIntegerField(default=0)
    tov = SmallIntegerField(default=0)
    fgm = SmallIntegerField(default=0)
    fga = SmallIntegerField(default=0)
    fg3m = SmallIntegerField(default=0)
    fg3a = SmallIntegerField(default=0)
    ftm = SmallIntegerField(default=0)
    fta = SmallIntegerField(default=0)
    oreb = SmallIntegerField(default=0)
    dreb = SmallIntegerField(default=0)
    dd2 = SmallIntegerField(default=0)
    td3 = SmallIntegerField(default=0)
    usg_pct = DecimalField(max_digits=5, decimal_places=3, null=True)

    # Audit columns
    pipeline_run_id = UUIDField(null=True)
    created_at = DateTimeField(default=datetime.utcnow)
    updated_at = DateTimeField(default=datetime.utcnow)

    class Meta:
        table_name = "player_history"
        schema = "nba"
        primary_key = CompositeKey("player_id", "season")

    def __repr__(self) -> str:
        return f"<PlayerHistory(player_id={self.player_id}, season='{self.season}', gp={self.gp})>"

    @classmethod
    def for_seasons(cls, seasons: list[str]) -> list["PlayerHistory"]:
        """Every row of the given seasons."""
        if not seasons:
            return []
        return list(cls.select().where(cls.season.in_(seasons)))
