"""Detached authentication and ownership dependencies.

All Peewee work is materialized in the bounded DB executor. Request handlers
receive immutable contexts, never live ORM instances or lazy relationships.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

import structlog
from fastapi import Depends, Request
from peewee import JOIN

from core.clerk_auth import get_current_user
from core.errors import NotFoundError
from db.base import run_db
from db.models.users import User
from db.models.teams import Team
from db.models.lineups import Lineup
from db.models.leagues import League
from schemas.common import LeagueInfo
from schemas.league import LeagueDetail
from services.league_service import LeagueService
from services.team_service import TeamService
from services.user_sync_service import UserSyncService


@dataclass(frozen=True)
class UserContext:
    user_id: int


@dataclass(frozen=True)
class OwnedTeamContext:
    team_id: int
    user_id: int
    league_info_json: str
    league_id: Optional[int]
    league: Optional[LeagueDetail]


@dataclass(frozen=True)
class OwnedLineupContext:
    lineup_id: int
    team_id: int
    user_id: int


def _get_or_create_user_context(clerk_user_id: str | None, email: str | None) -> UserContext:
    user = UserSyncService.get_or_create_user(clerk_user_id, email)
    return UserContext(user_id=user.user_id)


async def get_db_user(request: Request, current_user: dict = Depends(get_current_user)) -> UserContext:
    user = await run_db(
        "auth.resolve_user",
        _get_or_create_user_context,
        current_user.get("clerk_user_id"),
        current_user.get("email"),
    )
    structlog.contextvars.bind_contextvars(user_id=user.user_id)
    request.state.user_id = user.user_id
    return user


def _owned_team(team_id: int, user_id: int) -> Optional[OwnedTeamContext]:
    team = (
        Team.select(Team, League)
        .join(League, JOIN.LEFT_OUTER)
        .where((Team.team_id == team_id) & (Team.user_id == user_id))
        .first()
    )
    if team is None:
        return None
    league = None
    if team.league_id is not None:
        league = LeagueService.to_detail(team.league, LeagueService.preview_of(team.league_info))
    return OwnedTeamContext(
        team_id=team.team_id,
        user_id=user_id,
        league_info_json=team.league_info,
        league_id=team.league_id,
        league=league,
    )


def ensure_team_owned(team_id: int, user_id: int) -> Optional[OwnedTeamContext]:
    """Synchronous repository primitive; call it through `run_db` from async code."""
    return _owned_team(team_id, user_id)


def _as_owned_team_context(team: object, user_id: int) -> OwnedTeamContext:
    """Detach legacy repository/test rows at the dependency boundary."""
    if isinstance(team, OwnedTeamContext):
        return team
    raw_info = getattr(team, "league_info_json", getattr(team, "league_info", "{}"))
    league_id = getattr(team, "league_id", None)
    league_value = getattr(team, "league", None)
    if league_value is not None and not isinstance(league_value, LeagueDetail):
        league_value = LeagueService.to_detail(league_value, LeagueService.preview_of(raw_info))
    return OwnedTeamContext(
        team_id=int(getattr(team, "team_id")),
        user_id=user_id,
        league_info_json=raw_info,
        league_id=league_id,
        league=league_value,
    )


async def get_owned_team(team_id: int, user: UserContext = Depends(get_db_user)) -> OwnedTeamContext:
    team = await run_db("auth.owned_team", ensure_team_owned, team_id, user.user_id)
    if team is None:
        raise NotFoundError("TEAM_NOT_FOUND", "Team not found")
    return _as_owned_team_context(team, user.user_id)


def _owned_lineup(lineup_id: int, user_id: int) -> Optional[OwnedLineupContext]:
    row = (
        Lineup.select(Lineup.lineup_id, Lineup.team_id, Team.user_id)
        .join(Team, on=(Lineup.team_id == Team.team_id))
        .where((Lineup.lineup_id == lineup_id) & (Team.user_id == user_id))
        .first()
    )
    if row is None:
        return None
    return OwnedLineupContext(lineup_id=row.lineup_id, team_id=row.team_id, user_id=user_id)


async def get_owned_lineup(lineup_id: int, user: UserContext = Depends(get_db_user)) -> OwnedLineupContext:
    lineup = await run_db("auth.owned_lineup", _find_owned_lineup, lineup_id, user.user_id)
    if lineup is None:
        raise NotFoundError("LINEUP_NOT_FOUND", "Lineup not found")
    if isinstance(lineup, OwnedLineupContext):
        return lineup
    return OwnedLineupContext(
        lineup_id=int(getattr(lineup, "lineup_id")),
        team_id=int(getattr(lineup, "team_id", 0)),
        user_id=user.user_id,
    )


# Kept as the repository seam used by ownership tests and small maintenance
# scripts; the returned value is detached by `get_owned_lineup`.
_find_owned_lineup = _owned_lineup


def _hydrate_owned_league_info(team_id: int, user_id: int) -> LeagueInfo:
    team = Team.get_or_none((Team.team_id == team_id) & (Team.user_id == user_id))
    if team is None:
        raise NotFoundError("TEAM_NOT_FOUND", "Team not found")
    return TeamService.deserialize_league_info(json.loads(team.league_info), team)


async def load_owned_league_info(team: OwnedTeamContext) -> LeagueInfo:
    return await run_db(
        "teams.hydrate_provider_credentials",
        _hydrate_owned_league_info,
        team.team_id,
        team.user_id,
    )
