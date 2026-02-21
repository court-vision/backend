"""
Games Table

NBA game schedule and results.
"""

from datetime import datetime, date, timedelta

from peewee import (
    CharField,
    DateField,
    DateTimeField,
    IntegerField,
    BooleanField,
    ForeignKeyField,
    TimeField,
)

from db.base import BaseModel
from db.models.nba.teams import NBATeam


class Game(BaseModel):
    """
    NBA game schedule and results.

    Stores both upcoming scheduled games and completed game results.
    Used for schedule analysis, back-to-back detection, and matchup context.

    Attributes:
        game_id: NBA game ID (primary key)
        game_date: Date of the game
        season: Season identifier (e.g., '2024-25')
        home_team: Home team
        away_team: Away team
        home_score: Home team final score (null if not yet played)
        away_score: Away team final score (null if not yet played)
        status: Game status (scheduled, in_progress, final)
        arena: Arena name
        attendance: Attendance count
        updated_at: When this record was last modified
    """

    game_id = CharField(max_length=20, primary_key=True)
    game_date = DateField(index=True)
    season = CharField(max_length=7, index=True)
    home_team = ForeignKeyField(
        NBATeam,
        backref="home_games",
        on_delete="CASCADE",
        column_name="home_team_id",
    )
    away_team = ForeignKeyField(
        NBATeam,
        backref="away_games",
        on_delete="CASCADE",
        column_name="away_team_id",
    )
    home_score = IntegerField(null=True)
    away_score = IntegerField(null=True)
    status = CharField(max_length=20, default="scheduled")  # scheduled, in_progress, final
    start_time_et = TimeField(null=True)  # e.g., 19:30 for 7:30 PM ET
    arena = CharField(max_length=100, null=True)
    attendance = IntegerField(null=True)
    updated_at = DateTimeField(default=datetime.utcnow)

    class Meta:
        table_name = "games"
        schema = "nba"
        indexes = (
            (("game_date", "home_team"), False),
            (("game_date", "away_team"), False),
            (("season", "game_date"), False),
        )

    def __repr__(self) -> str:
        return (
            f"<Game("
            f"id={self.game_id}, "
            f"date={self.game_date}, "
            f"{self.away_team_id}@{self.home_team_id})>"
        )

    def save(self, *args, **kwargs):
        """Override save to auto-update updated_at timestamp."""
        self.updated_at = datetime.utcnow()
        return super().save(*args, **kwargs)

    @property
    def is_completed(self) -> bool:
        """Check if game has been completed."""
        return self.status == "final"

    @property
    def winner(self) -> str | None:
        """Get the winning team ID, or None if not completed or tie."""
        if not self.is_completed or self.home_score is None or self.away_score is None:
            return None
        if self.home_score > self.away_score:
            return self.home_team_id
        elif self.away_score > self.home_score:
            return self.away_team_id
        return None  # Tie (shouldn't happen in NBA)

    @classmethod
    def upsert_game(cls, game_id: str, game_data: dict) -> "Game":
        """
        Insert or update a game record.

        Args:
            game_id: NBA game ID
            game_data: Dict with game fields

        Returns:
            The created or updated Game instance
        """
        game, created = cls.get_or_create(
            game_id=game_id,
            defaults=game_data,
        )

        if not created:
            for key, value in game_data.items():
                if value is not None:
                    setattr(game, key, value)
            game.save()

        return game

    @classmethod
    def get_team_games(
        cls,
        team_id: str,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list["Game"]:
        """
        Get games for a specific team within a date range.

        Args:
            team_id: Team abbreviation
            start_date: Start of date range (inclusive)
            end_date: End of date range (inclusive)

        Returns:
            List of Game records
        """
        query = cls.select().where(
            (cls.home_team_id == team_id) | (cls.away_team_id == team_id)
        )

        if start_date:
            query = query.where(cls.game_date >= start_date)
        if end_date:
            query = query.where(cls.game_date <= end_date)

        return list(query.order_by(cls.game_date))

    @classmethod
    def get_games_on_date(cls, game_date: date) -> list["Game"]:
        """Get all games on a specific date."""
        return list(
            cls.select()
            .where(cls.game_date == game_date)
            .order_by(cls.game_id)
        )

    @classmethod
    def is_back_to_back(cls, team_id: str, game_date: date) -> bool:
        """
        Check if this is the second game of a back-to-back for a team.

        Args:
            team_id: Team abbreviation
            game_date: Date to check

        Returns:
            True if team played yesterday
        """
        yesterday = game_date - timedelta(days=1)
        return (
            cls.select()
            .where(
                ((cls.home_team_id == team_id) | (cls.away_team_id == team_id))
                & (cls.game_date == yesterday)
            )
            .exists()
        )

    @classmethod
    def get_remaining_games(cls, team_id: str, season: str) -> int:
        """
        Get count of remaining games for a team this season.

        Args:
            team_id: Team abbreviation
            season: Season identifier

        Returns:
            Number of scheduled games remaining
        """
        today = date.today()
        return (
            cls.select()
            .where(
                ((cls.home_team_id == team_id) | (cls.away_team_id == team_id))
                & (cls.season == season)
                & (cls.game_date >= today)
                & (cls.status == "scheduled")
            )
            .count()
        )

    @classmethod
    def get_earliest_game_time_on_date(cls, game_date: date):
        """
        Get the earliest game start time (ET) on a given date.

        Args:
            game_date: Date to check

        Returns:
            datetime.time or None if no games with start times
        """
        from peewee import fn

        result = (
            cls.select(fn.MIN(cls.start_time_et))
            .where(
                (cls.game_date == game_date)
                & (cls.start_time_et.is_null(False))
            )
            .scalar()
        )
        return result

    @classmethod
    def get_latest_game_time_on_date(cls, game_date: date):
        """
        Get the latest game start time (ET) on a given date.

        Args:
            game_date: Date to check

        Returns:
            datetime.time or None if no games with start times
        """
        from peewee import fn

        result = (
            cls.select(fn.MAX(cls.start_time_et))
            .where(
                (cls.game_date == game_date)
                & (cls.start_time_et.is_null(False))
            )
            .scalar()
        )
        return result

    @classmethod
    def get_teams_playing_on_date(cls, game_date: date) -> set[str]:
        """
        Get set of team abbreviations with games on a given date.

        Args:
            game_date: Date to check

        Returns:
            Set of team abbreviation strings (both home and away)
        """
        games = cls.select(cls.home_team_id, cls.away_team_id).where(
            cls.game_date == game_date
        )
        teams = set()
        for game in games:
            teams.add(game.home_team_id)
            teams.add(game.away_team_id)
        return teams
