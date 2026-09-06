"""Compatibility import; rankings live in the normalized nba schema."""

from db.models.nba.rankings import Rankings, RankingsSource

__all__ = ["Rankings", "RankingsSource"]
