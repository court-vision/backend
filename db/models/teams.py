from peewee import (
    AutoField,
    CharField,
    TextField,
    ForeignKeyField,
)
from db.base import BaseModel
from db.models.users import User


class Team(BaseModel):
    team_id = AutoField(primary_key=True)
    user_id = ForeignKeyField(User, backref='teams', on_delete='CASCADE')
    team_identifier = CharField(max_length=255)
    league_info = TextField()  # JSON string

    class Meta:
        table_name = "teams"
        schema = "usr"

    def __repr__(self):
        return f"<Team(team_id={self.team_id}, user_id={self.user_id}, team_identifier='{self.team_identifier}')>"
