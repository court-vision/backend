from peewee import (
    Model,
    IntegerField,
    CharField,
    TextField,
    ForeignKeyField,
)
from app.db.base import BaseModel
from app.db.models.usr.teams import Team


class Lineup(BaseModel):
    lineup_id = IntegerField(primary_key=True)
    team_id = ForeignKeyField(Team, backref='lineups', on_delete='CASCADE')
    lineup_info = TextField()  # JSON string
    lineup_hash = CharField(max_length=32, unique=True)

    class Meta:
        table_name = "lineups"
        schema = "usr"

    def __repr__(self):
        return f"<Lineup(lineup_id={self.lineup_id}, team_id={self.team_id}, lineup_hash='{self.lineup_hash}')>"
