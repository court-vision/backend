from peewee import BigIntegerField, IntegerField, CharField, DecimalField
from db.base import BaseModel


class Rankings(BaseModel):
    id = IntegerField()
    curr_rank = BigIntegerField()   # RANK() returns bigint in Postgres
    name = CharField(max_length=100)
    team = CharField(max_length=3, null=True)
    fpts = IntegerField()           # cumulative season total
    avg_fpts = DecimalField(max_digits=6, decimal_places=2)
    rank_change = BigIntegerField()  # prev_rank - curr_rank, both bigint

    class Meta:
        schema = 'nba'
        table_name = 'rankings'
        primary_key = False
