"""
Player Dimension Table

Master data for NBA players. This is a dimension table that stores
static player information. Game statistics reference this table via player_id.
"""

from datetime import datetime

from peewee import (
    SQL,
    IntegerField,
    CharField,
    DateTimeField,
)

from core.season import previous_season
from core.settings import settings
from db.base import BaseModel


class Player(BaseModel):
    """
    NBA Player master data.

    This dimension table stores player identity information.
    The id field is the NBA's official player ID from nba_api.

    Attributes:
        id: NBA player ID (primary key, from nba_api)
        espn_id: ESPN's player ID for cross-referencing fantasy data
        name: Player's display name
        name_normalized: Lowercase, stripped name for fuzzy matching
        position: Player's primary position (G, F, C, etc.)
        created_at: When this record was first created
        updated_at: When this record was last modified
    """

    id = IntegerField(primary_key=True)  # NBA player ID
    espn_id = IntegerField(null=True, unique=True, index=True)
    name = CharField(max_length=100)
    name_normalized = CharField(max_length=100, index=True)
    position = CharField(max_length=10, null=True)
    created_at = DateTimeField(default=datetime.utcnow)
    updated_at = DateTimeField(default=datetime.utcnow)

    class Meta:
        table_name = "players"
        schema = "nba"

    def __repr__(self) -> str:
        return f"<Player(id={self.id}, name='{self.name}')>"

    def save(self, *args, **kwargs):
        """Override save to auto-update updated_at timestamp."""
        self.updated_at = datetime.utcnow()
        return super().save(*args, **kwargs)

    @classmethod
    def upsert_player(
        cls,
        player_id: int,
        name: str,
        espn_id: int | None = None,
        position: str | None = None,
    ) -> "Player":
        """
        Insert or update a player record.

        Args:
            player_id: NBA player ID
            name: Player's display name
            espn_id: Optional ESPN player ID
            position: Optional position

        Returns:
            The created or updated Player instance
        """
        name_normalized = name.lower().strip()

        player, created = cls.get_or_create(
            id=player_id,
            defaults={
                "name": name,
                "name_normalized": name_normalized,
                "espn_id": espn_id,
                "position": position,
            },
        )

        if not created:
            # Update existing record if data changed
            update_needed = False
            if player.name != name:
                player.name = name
                player.name_normalized = name_normalized
                update_needed = True
            if espn_id and player.espn_id != espn_id:
                player.espn_id = espn_id
                update_needed = True
            if position and player.position != position:
                player.position = position
                update_needed = True

            if update_needed:
                player.save()

        return player

    @classmethod
    def in_play(cls):
        """Where-clause: players of today's game.

        That is a PlayerIndex profile (nba.player_profiles: this season's
        players, rookies included) or season stats from last season on.
        nba.players also holds everyone the game-history load brings in
        (2012-13 onward, migration 0031) and the All-Star celebrity game's
        rows; a lookup by name that means a current player uses this, so a
        retired namesake can never take his place.
        """
        since = previous_season(settings.nba_season)
        return cls.id.in_(SQL("(SELECT player_id FROM nba.player_profiles)")) | cls.id.in_(
            SQL("(SELECT player_id FROM nba.player_season_stats WHERE season >= %s)", (since,))
        )

    @classmethod
    def find_by_name(cls, name: str) -> "Player | None":
        """
        Find the one player in play with this normalized name.

        Args:
            name: Player name to search for

        Returns:
            Player instance, or None if no player in play has the name or more
            than one does (a guess between namesakes is worse than no match)
        """
        name_normalized = name.lower().strip()
        matches = list(
            cls.select().where((cls.name_normalized == name_normalized) & cls.in_play()).limit(2)
        )
        return matches[0] if len(matches) == 1 else None
