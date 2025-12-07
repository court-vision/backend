from peewee import (
    Model,
    IntegerField,
    CharField,
    DateField,
    DecimalField,
)
from db.base import BaseModel


class FreeAgent(BaseModel):
    espn_id = IntegerField(primary_key=True)
    name = CharField(max_length=255)
    team = CharField(max_length=3)
    date = DateField()
    rostered_pct = DecimalField(max_digits=5, decimal_places=2)

    class Meta:
        table_name = "freeagents"
        schema = "stats_s1"

    def __repr__(self):
        return f"<FreeAgent(espn_id={self.espn_id}, name='{self.name}', team='{self.team}')>"
