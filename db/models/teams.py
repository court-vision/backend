from peewee import (
    AutoField,
    CharField,
    TextField,
    ForeignKeyField,
)
from db.base import BaseModel
from db.models.users import User
from db.models.leagues import League
from db.models.provider_connections import ProviderConnection


class Team(BaseModel):
    team_id = AutoField(primary_key=True)
    user_id = ForeignKeyField(User, backref='teams', on_delete='CASCADE')
    team_identifier = CharField(max_length=255)
    # JSON string. Held credentials until migration 0005; new writes keep only
    # non-secret fields here and put the credentials in provider_connection.
    league_info = TextField()
    # Provider-detected league settings; NULL until synced. Distinct from league_info.league_id (provider id).
    league = ForeignKeyField(League, column_name='league_id', null=True, backref='teams', on_delete='SET NULL')
    # Encrypted provider credentials. NULL means this team has not been migrated
    # yet and its secrets are still inline in league_info -- see
    # services/credential_service.py, which reads from whichever is populated.
    provider_connection = ForeignKeyField(
        ProviderConnection, column_name='provider_connection_id', null=True,
        backref='teams', on_delete='SET NULL',
    )

    class Meta:
        table_name = "teams"
        schema = "usr"

    def __repr__(self):
        return f"<Team(team_id={self.team_id}, user_id={self.user_id}, team_identifier='{self.team_identifier}')>"
