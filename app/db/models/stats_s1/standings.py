from peewee import (
    Model,
    IntegerField,
    CharField,
    DecimalField,
)
from app.db.base import BaseModel


class Standing(BaseModel):
    rank = IntegerField()
    player_id = IntegerField()
    player_name = CharField(max_length=255)
    total_fpts = DecimalField(max_digits=10, decimal_places=2)
    avg_fpts = DecimalField(max_digits=6, decimal_places=2)
    rank_change = IntegerField(null=True)

    class Meta:
        table_name = "standings"
        schema = "stats_s1"

    def __repr__(self):
        return f"<Standing(rank={self.rank}, player_name='{self.player_name}', total_fpts={self.total_fpts})>"
