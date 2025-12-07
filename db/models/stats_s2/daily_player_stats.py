from peewee import (
    Model,
    IntegerField,
    CharField,
    DateField,
    SmallIntegerField,
    DecimalField,
)
from db.base import BaseModel

class DailyPlayerStats(BaseModel):
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
    rost_pct = DecimalField(max_digits=7, decimal_places=4, null=True, default=None)

    class Meta:
        table_name = "daily_player_stats"
        schema = "stats_s2"
        primary_key = False
        indexes = (
            (('id', 'date'), True),
        )

    def __repr__(self):
        return f"<DailyPlayerStats(id={self.id}, date={self.date}, name='{self.name}')>"

