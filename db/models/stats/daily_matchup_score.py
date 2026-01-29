from datetime import datetime

from peewee import (
    IntegerField,
    CharField,
    DateField,
    DateTimeField,
    SmallIntegerField,
    DecimalField,
    UUIDField,
)
from db.base import BaseModel


class DailyMatchupScore(BaseModel):
    """
    Tracks daily fantasy team scores for matchup visualization.

    Each record represents a team's score snapshot on a specific day
    of a matchup period. Both team's and opponent's scores are stored
    in the same record for efficient chart data retrieval.
    """

    # Team identification
    team_id = IntegerField()  # References Team.team_id
    team_name = CharField(max_length=100)

    # Matchup identification
    matchup_period = SmallIntegerField()  # Week number (1-20+)
    opponent_team_name = CharField(max_length=100)

    # Time tracking
    date = DateField()
    day_of_matchup = SmallIntegerField()  # 0-indexed day within matchup

    # Scores
    current_score = DecimalField(max_digits=8, decimal_places=2)
    opponent_current_score = DecimalField(max_digits=8, decimal_places=2)

    # Audit columns for pipeline tracking
    pipeline_run_id = UUIDField(null=True, index=True)
    created_at = DateTimeField(default=datetime.utcnow)
    updated_at = DateTimeField(default=datetime.utcnow)

    class Meta:
        table_name = "daily_matchup_scores"
        schema = "stats_s2"
        primary_key = False
        indexes = (
            (("team_id", "matchup_period", "date"), True),  # Composite unique
        )

    def __repr__(self):
        return f"<DailyMatchupScore(team_id={self.team_id}, matchup={self.matchup_period}, date={self.date}, score={self.current_score})>"
