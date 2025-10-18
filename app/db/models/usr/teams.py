from peewee import (
    AutoField,
    CharField,
    TextField,
    ForeignKeyField,
)
from app.db.base import BaseModel
from app.db.models.usr.users import User


class Team(BaseModel):
    team_id = AutoField(primary_key=True)
    user_id = ForeignKeyField(User, backref='teams', on_delete='CASCADE')
    team_identifier = CharField(max_length=255, unique=True)
    team_info = TextField()  # JSON string

    class Meta:
        table_name = "teams"
        schema = "usr"

    def __repr__(self):
        return f"<Team(team_id={self.team_id}, user_id={self.user_id}, team_identifier='{self.team_identifier}')>"
