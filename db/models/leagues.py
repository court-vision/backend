from datetime import datetime

from peewee import AutoField, CharField, DateTimeField, IntegerField
from playhouse.postgres_ext import BinaryJSONField

from db.base import BaseModel


class League(BaseModel):
    """Provider-detected league settings (scoring type, categories, point weights).

    Identified by (provider, provider_league_id, season). Credentials are NOT
    stored here — they remain in usr.teams.league_info.
    """

    id = AutoField()
    provider = CharField(max_length=16)                 # espn | yahoo
    provider_league_id = CharField(max_length=64)       # ESPN: str(league_id); Yahoo: league_key "466.l.12345"
    season = IntegerField()
    name = CharField(max_length=255, null=True)
    scoring_type = CharField(max_length=16, default="points")     # points | categories | roto
    category_win_mode = CharField(max_length=16, null=True)       # each_category | most_categories
    categories = BinaryJSONField(default=list)          # ordered [{key, label, higher_is_better, is_rate}]
    point_weights = BinaryJSONField(default=dict)       # {canonical_stat_key: float}
    matchup_periods = BinaryJSONField(default=dict)
    roster_slots = BinaryJSONField(default=dict)        # {"PG": 1, ..., "UT": 3, "BE": 3, "IR": 1}
    raw_settings = BinaryJSONField(null=True)
    settings_synced_at = DateTimeField(null=True)
    created_at = DateTimeField(default=datetime.utcnow)
    updated_at = DateTimeField(default=datetime.utcnow)

    class Meta:
        table_name = "leagues"
        schema = "usr"
        indexes = (
            (("provider", "provider_league_id", "season"), True),
        )

    @property
    def settings_synced(self) -> bool:
        return self.settings_synced_at is not None

    def __repr__(self):
        return (
            f"<League(id={self.id}, provider='{self.provider}', "
            f"provider_league_id='{self.provider_league_id}', season={self.season}, "
            f"scoring_type='{self.scoring_type}')>"
        )
