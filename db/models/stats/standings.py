from peewee import IntegerField, CharField, DecimalField, SmallIntegerField
from db.base import BaseModel


class Standings(BaseModel):
    id = IntegerField()
    curr_rank = SmallIntegerField()
    name = CharField(max_length=50)
    team = CharField(max_length=3)
    fpts = SmallIntegerField()
    avg_fpts = DecimalField(max_digits=6, decimal_places=2)
    rank_change = SmallIntegerField()

    class Meta:
        schema = 'stats_s2'
        table_name = 'standings'
        primary_key = False

