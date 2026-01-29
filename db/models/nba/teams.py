"""
NBA Team Dimension Table

Master data for NBA teams. This is a dimension table that stores
static team information.
"""

from datetime import datetime

from peewee import (
    CharField,
    DateTimeField,
)

from db.base import BaseModel


class NBATeam(BaseModel):
    """
    NBA Team master data.

    This dimension table stores the 30 NBA teams.
    Uses the standard 3-letter abbreviation as the primary key.

    Attributes:
        id: Team abbreviation (e.g., 'LAL', 'BOS', 'GSW')
        name: Full team name (e.g., 'Los Angeles Lakers')
        conference: 'East' or 'West'
        division: Team's division
        created_at: When this record was first created
        updated_at: When this record was last modified
    """

    id = CharField(max_length=3, primary_key=True)  # Team abbreviation
    name = CharField(max_length=50)
    conference = CharField(max_length=4)  # East or West
    division = CharField(max_length=20)
    created_at = DateTimeField(default=datetime.utcnow)
    updated_at = DateTimeField(default=datetime.utcnow)

    class Meta:
        table_name = "teams"
        schema = "nba"

    def __repr__(self) -> str:
        return f"<NBATeam(id='{self.id}', name='{self.name}')>"

    @classmethod
    def seed_teams(cls) -> int:
        """
        Seed the teams table with all 30 NBA teams.

        Returns:
            Number of teams inserted
        """
        teams_data = [
            # Eastern Conference - Atlantic
            ("BOS", "Boston Celtics", "East", "Atlantic"),
            ("BKN", "Brooklyn Nets", "East", "Atlantic"),
            ("NYK", "New York Knicks", "East", "Atlantic"),
            ("PHI", "Philadelphia 76ers", "East", "Atlantic"),
            ("TOR", "Toronto Raptors", "East", "Atlantic"),
            # Eastern Conference - Central
            ("CHI", "Chicago Bulls", "East", "Central"),
            ("CLE", "Cleveland Cavaliers", "East", "Central"),
            ("DET", "Detroit Pistons", "East", "Central"),
            ("IND", "Indiana Pacers", "East", "Central"),
            ("MIL", "Milwaukee Bucks", "East", "Central"),
            # Eastern Conference - Southeast
            ("ATL", "Atlanta Hawks", "East", "Southeast"),
            ("CHA", "Charlotte Hornets", "East", "Southeast"),
            ("MIA", "Miami Heat", "East", "Southeast"),
            ("ORL", "Orlando Magic", "East", "Southeast"),
            ("WAS", "Washington Wizards", "East", "Southeast"),
            # Western Conference - Northwest
            ("DEN", "Denver Nuggets", "West", "Northwest"),
            ("MIN", "Minnesota Timberwolves", "West", "Northwest"),
            ("OKC", "Oklahoma City Thunder", "West", "Northwest"),
            ("POR", "Portland Trail Blazers", "West", "Northwest"),
            ("UTA", "Utah Jazz", "West", "Northwest"),
            # Western Conference - Pacific
            ("GSW", "Golden State Warriors", "West", "Pacific"),
            ("LAC", "Los Angeles Clippers", "West", "Pacific"),
            ("LAL", "Los Angeles Lakers", "West", "Pacific"),
            ("PHX", "Phoenix Suns", "West", "Pacific"),
            ("SAC", "Sacramento Kings", "West", "Pacific"),
            # Western Conference - Southwest
            ("DAL", "Dallas Mavericks", "West", "Southwest"),
            ("HOU", "Houston Rockets", "West", "Southwest"),
            ("MEM", "Memphis Grizzlies", "West", "Southwest"),
            ("NOP", "New Orleans Pelicans", "West", "Southwest"),
            ("SAS", "San Antonio Spurs", "West", "Southwest"),
        ]

        inserted = 0
        for abbrev, name, conference, division in teams_data:
            _, created = cls.get_or_create(
                id=abbrev,
                defaults={
                    "name": name,
                    "conference": conference,
                    "division": division,
                },
            )
            if created:
                inserted += 1

        return inserted
