from peewee import (
    AutoField,
    CharField,
    TextField,
    ForeignKeyField,
)
from db.base import BaseModel
from db.models.users import User
from db.models.leagues import League


class Team(BaseModel):
    team_id = AutoField(primary_key=True)
    user_id = ForeignKeyField(User, backref='teams', on_delete='CASCADE')
    team_identifier = CharField(max_length=255)
    league_info = TextField()  # JSON string (credentials + provider ids)
    # Provider-detected league settings; NULL until synced. Distinct from league_info.league_id (provider id).
    league = ForeignKeyField(League, column_name='league_id', null=True, backref='teams', on_delete='SET NULL')

    class Meta:
        table_name = "teams"
        schema = "usr"

    def __repr__(self):
        return f"<Team(team_id={self.team_id}, user_id={self.user_id}, team_identifier='{self.team_identifier}')>"
