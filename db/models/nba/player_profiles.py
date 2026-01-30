"""
Player Profiles Table

Biographical and metadata for NBA players - height, weight, position, draft info, etc.
"""

from datetime import datetime

from peewee import (
    IntegerField,
    CharField,
    DateField,
    DateTimeField,
    ForeignKeyField,
)

from db.base import BaseModel
from db.models.nba.players import Player
from db.models.nba.teams import NBATeam


class PlayerProfile(BaseModel):
    """
    Player biographical and metadata information.

    This table stores relatively static player information that doesn't
    change game-to-game: physical attributes, draft history, career span.

    Attributes:
        player: Foreign key to Player dimension
        first_name: Player's first name
        last_name: Player's last name
        birthdate: Date of birth
        height: Height as string (e.g., "6-11")
        weight: Weight in pounds
        position: Primary position (G, F, C, G-F, etc.)
        jersey_number: Current jersey number
        team: Current team
        draft_year: Year drafted (null if undrafted)
        draft_round: Round drafted (null if undrafted)
        draft_number: Overall pick number (null if undrafted)
        season_exp: Years of NBA experience
        country: Country of origin
        school: College/last team before NBA
        from_year: First NBA season year
        to_year: Most recent NBA season year
        updated_at: When this record was last modified
    """

    player = ForeignKeyField(
        Player,
        primary_key=True,
        backref="profile",
        on_delete="CASCADE",
        column_name="player_id",
    )
    first_name = CharField(max_length=50, null=True)
    last_name = CharField(max_length=50, null=True)
    birthdate = DateField(null=True)
    height = CharField(max_length=10, null=True)  # "6-11"
    weight = IntegerField(null=True)
    position = CharField(max_length=20, null=True)
    jersey_number = CharField(max_length=5, null=True)
    team = ForeignKeyField(
        NBATeam,
        backref="player_profiles",
        on_delete="SET NULL",
        column_name="team_id",
        null=True,
    )
    draft_year = IntegerField(null=True)
    draft_round = IntegerField(null=True)
    draft_number = IntegerField(null=True)
    season_exp = IntegerField(null=True)
    country = CharField(max_length=50, null=True)
    school = CharField(max_length=100, null=True)
    from_year = IntegerField(null=True)
    to_year = IntegerField(null=True)
    updated_at = DateTimeField(default=datetime.utcnow)

    class Meta:
        table_name = "player_profiles"
        schema = "nba"

    def __repr__(self) -> str:
        return f"<PlayerProfile(player_id={self.player_id}, name='{self.first_name} {self.last_name}')>"

    def save(self, *args, **kwargs):
        """Override save to auto-update updated_at timestamp."""
        self.updated_at = datetime.utcnow()
        return super().save(*args, **kwargs)

    @property
    def full_name(self) -> str:
        """Get player's full name."""
        return f"{self.first_name or ''} {self.last_name or ''}".strip()

    @property
    def height_inches(self) -> int | None:
        """Convert height string to total inches."""
        if not self.height or "-" not in self.height:
            return None
        try:
            feet, inches = self.height.split("-")
            return int(feet) * 12 + int(inches)
        except (ValueError, TypeError):
            return None

    @classmethod
    def upsert_profile(cls, player_id: int, profile_data: dict) -> "PlayerProfile":
        """
        Insert or update a player profile.

        Args:
            player_id: NBA player ID
            profile_data: Dict with profile fields

        Returns:
            The created or updated PlayerProfile instance
        """
        profile, created = cls.get_or_create(
            player_id=player_id,
            defaults=profile_data,
        )

        if not created:
            for key, value in profile_data.items():
                if value is not None:
                    setattr(profile, key, value)
            profile.save()

        return profile
