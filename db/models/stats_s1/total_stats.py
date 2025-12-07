from peewee import (
    Model,
    IntegerField,
    CharField,
    DateField,
    SmallIntegerField,
    DecimalField,
)
from db.base import BaseModel

class TotalStats(BaseModel):
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
    gp = SmallIntegerField()
    c_rank = SmallIntegerField(null=True)
    p_rank = SmallIntegerField(null=True)
    rost_pct = DecimalField(max_digits=7, decimal_places=4, null=True)

    class Meta:
        table_name = "total_stats"
        schema = "stats_s1"

    def __repr__(self):
        return f"<TotalStats(id={self.id}, date={self.date}, name='{self.name}')>"
