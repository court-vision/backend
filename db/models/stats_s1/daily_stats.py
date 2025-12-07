from peewee import (
    Model,
    IntegerField,
    CharField,
    DateField,
    SmallIntegerField,
    DecimalField,
)
from db.base import BaseModel

class DailyStats(BaseModel):
    id = IntegerField(primary_key=True)
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
    rost_pct = DecimalField(max_digits=7, decimal_places=4, null=True)

    class Meta:
        table_name = "daily_stats"
        schema = "stats_s1"
        indexes = (
            (('id', 'date'), True),
        )

    def __repr__(self):
        return f"<DailyStats(id={self.id}, date={self.date}, name='{self.name}')>"
