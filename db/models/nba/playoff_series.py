"""
Playoff Series Table (read-only in backend)

Written by data-platform PlayoffBracketPipeline.
"""

from datetime import datetime

from peewee import (
    CharField,
    SmallIntegerField,
    IntegerField,
    BooleanField,
    DateTimeField,
)

from db.base import BaseModel


class PlayoffSeries(BaseModel):
    season = CharField(max_length=7, index=True)
    series_id = CharField(max_length=20, index=True)
    conference = CharField(max_length=10)
    round_num = SmallIntegerField()
    top_seed_team_id = IntegerField(null=True)
    top_seed_name = CharField(max_length=50, null=True)
    top_seed_abbr = CharField(max_length=5)
    top_seed_wins = SmallIntegerField(default=0)
    bottom_seed_team_id = IntegerField(null=True)
    bottom_seed_name = CharField(max_length=50, null=True)
    bottom_seed_abbr = CharField(max_length=5)
    bottom_seed_wins = SmallIntegerField(default=0)
    series_complete = BooleanField(default=False)
    series_leader_abbr = CharField(max_length=5, null=True)
    updated_at = DateTimeField(default=datetime.utcnow)

    class Meta:
        table_name = "playoff_series"
        schema = "nba"
        indexes = (
            (("season", "series_id"), True),
        )
