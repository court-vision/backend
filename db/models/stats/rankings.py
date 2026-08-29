"""Season rankings, in two shapes of the same query (migration 0006).

`Rankings` reads the materialized copy the API serves; `RankingsSource` reads
the view it is materialized from, which is always current. The read path uses
the second only when the first has fallen behind nba.player_season_stats.
"""

from peewee import BigIntegerField, CharField, DateField, DecimalField, IntegerField, SmallIntegerField

from db.base import BaseModel


class _RankingsRow(BaseModel):
    id = IntegerField()
    curr_rank = BigIntegerField()   # RANK() returns bigint in Postgres
    name = CharField(max_length=100)
    team = CharField(max_length=3, null=True)
    fpts = IntegerField()           # cumulative season total
    avg_fpts = DecimalField(max_digits=6, decimal_places=2)
    rank_change = BigIntegerField()  # prev_rank - curr_rank, both bigint
    gp = SmallIntegerField(null=True)
    as_of_date = DateField(null=True)   # snapshot date this player's row runs through
    season = CharField(max_length=7, null=True)

    class Meta:
        schema = 'nba'
        primary_key = False


class Rankings(_RankingsRow):
    """The materialized view. Refreshed post-game; may lag, never blocks."""

    class Meta:
        table_name = 'rankings'


class RankingsSource(_RankingsRow):
    """The underlying view. Always current, and the cost migration 0006 removes."""

    class Meta:
        table_name = 'rankings_source'
