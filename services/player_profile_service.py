"""Dimension-backed player search and profile reads."""

from datetime import datetime, timezone

from peewee import Case, JOIN, fn

from core.errors import NotFoundError
from db.base import db_operation
from db.models.nba.player_profiles import PlayerProfile
from db.models.nba.players import Player
from schemas.common import ApiStatus
from schemas.player_profiles import (
    PlayerProfileData,
    PlayerProfileDetails,
    PlayerProfileResp,
    PlayerSearchData,
    PlayerSearchItem,
    PlayerSearchResp,
)


def _utc(value: datetime) -> datetime:
    """Expose the database's legacy naive UTC timestamps unambiguously."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _height_inches(height: str | None) -> int | None:
    if not height or "-" not in height:
        return None
    try:
        feet, inches = height.split("-", 1)
        return int(feet) * 12 + int(inches)
    except (TypeError, ValueError):
        return None


class PlayerProfileService:
    """Read player identities without requiring a season-stat fact row."""

    @staticmethod
    @db_operation("players.search")
    def search_players(
        q: str,
        limit: int = 10,
        offset: int = 0,
    ) -> PlayerSearchResp:
        term = q.strip()
        limit = min(max(1, limit), 50)
        offset = max(0, offset)

        normalized_name = fn.LOWER(fn.unaccent(Player.name))
        normalized_term = term.lower()
        unaccented_term = fn.unaccent(normalized_term)
        predicate = normalized_name.contains(unaccented_term)
        if term.isdecimal():
            identifier = int(term)
            predicate = predicate | (Player.id == identifier) | (Player.espn_id == identifier)

        query = (
            Player.select(
                Player.id,
                Player.espn_id,
                Player.name,
                Player.position,
                Player.updated_at,
                PlayerProfile.team,
                PlayerProfile.updated_at,
            )
            .join(
                PlayerProfile,
                JOIN.LEFT_OUTER,
                on=(Player.id == PlayerProfile.player),
            )
            .where(predicate)
        )

        total = query.count()
        relevance = Case(
            None,
            (
                (normalized_name == unaccented_term, 0),
                (normalized_name.startswith(unaccented_term), 1),
            ),
            2,
        )
        rows = (
            query.order_by(relevance, Player.name.asc(), Player.id.asc())
            .offset(offset)
            .limit(limit)
            .tuples()
        )

        players = [
            PlayerSearchItem(
                id=player_id,
                espn_id=espn_id,
                name=name,
                position=position,
                player_updated_at=_utc(player_updated_at),
                team=team,
                profile_updated_at=_utc(profile_updated_at) if profile_updated_at else None,
            )
            for player_id, espn_id, name, position, player_updated_at, team, profile_updated_at in rows
        ]
        return PlayerSearchResp(
            status=ApiStatus.SUCCESS,
            message=f"Found {total} players",
            data=PlayerSearchData(
                query=term,
                players=players,
                total=total,
                limit=limit,
                offset=offset,
            ),
        )

    @staticmethod
    @db_operation("players.profile")
    def get_profile(player_id: int) -> PlayerProfileResp:
        row = (
            Player.select(
                Player.id.alias("id"),
                Player.espn_id.alias("espn_id"),
                Player.name.alias("name"),
                Player.position.alias("player_position"),
                Player.created_at.alias("player_created_at"),
                Player.updated_at.alias("player_updated_at"),
                PlayerProfile.player.alias("profile_player_id"),
                PlayerProfile.first_name,
                PlayerProfile.last_name,
                PlayerProfile.birthdate,
                PlayerProfile.height,
                PlayerProfile.weight,
                PlayerProfile.position.alias("profile_position"),
                PlayerProfile.jersey_number,
                PlayerProfile.team.alias("team"),
                PlayerProfile.draft_year,
                PlayerProfile.draft_round,
                PlayerProfile.draft_number,
                PlayerProfile.season_exp,
                PlayerProfile.country,
                PlayerProfile.school,
                PlayerProfile.from_year,
                PlayerProfile.to_year,
                PlayerProfile.updated_at.alias("profile_updated_at"),
            )
            .join(
                PlayerProfile,
                JOIN.LEFT_OUTER,
                on=(Player.id == PlayerProfile.player),
            )
            .where(Player.id == player_id)
            .dicts()
            .first()
        )
        if row is None:
            raise NotFoundError("PLAYER_NOT_FOUND", "Player not found")

        profile = None
        if row["profile_player_id"] is not None:
            profile = PlayerProfileDetails(
                first_name=row["first_name"],
                last_name=row["last_name"],
                birthdate=row["birthdate"],
                height=row["height"],
                height_inches=_height_inches(row["height"]),
                weight=row["weight"],
                position=row["profile_position"],
                jersey_number=row["jersey_number"],
                team=row["team"],
                draft_year=row["draft_year"],
                draft_round=row["draft_round"],
                draft_number=row["draft_number"],
                season_exp=row["season_exp"],
                country=row["country"],
                school=row["school"],
                from_year=row["from_year"],
                to_year=row["to_year"],
                updated_at=_utc(row["profile_updated_at"]),
            )

        return PlayerProfileResp(
            status=ApiStatus.SUCCESS,
            message="Player profile",
            data=PlayerProfileData(
                id=row["id"],
                espn_id=row["espn_id"],
                name=row["name"],
                position=row["player_position"],
                created_at=_utc(row["player_created_at"]),
                updated_at=_utc(row["player_updated_at"]),
                profile=profile,
            ),
        )
