from datetime import datetime

from peewee import (
    IntegerField,
    CharField,
    DateField,
    DateTimeField,
    SmallIntegerField,
    DecimalField,
    UUIDField,
)
from db.base import BaseModel


class CumulativePlayerStats(BaseModel):
    id = IntegerField()
    name = CharField(max_length=50)
    team = CharField(max_length=3)
    date = DateField()
    fpts = SmallIntegerField()
    pts = SmallIntegerField()
    reb = SmallIntegerField()
    ast = SmallIntegerField()
    stl = SmallIntegerField()
    blk = SmallIntegerField()
    tov = SmallIntegerField()
    fgm = SmallIntegerField()
    fga = SmallIntegerField()
    fg3m = SmallIntegerField()
    fg3a = SmallIntegerField()
    ftm = SmallIntegerField()
    fta = SmallIntegerField()
    min = IntegerField()
    gp = SmallIntegerField()
    rank = SmallIntegerField(null=True)
    rost_pct = DecimalField(max_digits=7, decimal_places=4, null=True)

    # Audit columns for pipeline tracking
    pipeline_run_id = UUIDField(null=True, index=True)
    created_at = DateTimeField(default=datetime.utcnow)
    updated_at = DateTimeField(default=datetime.utcnow)

    class Meta:
        schema = 'stats_s2'
        table_name = 'cumulative_player_stats'
        primary_key = False
        indexes = (
            (('id', 'date'), True),  # Composite unique index
        )
