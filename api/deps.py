"""
Shared FastAPI dependencies for internal routes.

Centralizes Clerk-user -> local-user resolution and the ownership checks
that every team- and lineup-scoped endpoint must enforce. Ownership misses
return 404 (not 403) so foreign resource ids are indistinguishable from
nonexistent ones.

These are `async` on purpose: FastAPI runs sync dependencies in a worker
thread, where Peewee would open a second, never-released connection instead
of using the one `core.db_middleware` manages on the event-loop thread.
"""

from typing import Optional

import structlog
from fastapi import Depends, Request

from core.clerk_auth import get_current_user
from core.errors import NotFoundError
from db.models.users import User
from db.models.teams import Team
from db.models.lineups import Lineup
from services.user_sync_service import UserSyncService


async def get_db_user(request: Request, current_user: dict = Depends(get_current_user)) -> User:
    """Resolve the authenticated Clerk user to their usr.users row (created on first request)."""
    user = UserSyncService.get_or_create_user(
        current_user.get("clerk_user_id"),
        current_user.get("email"),
    )
    # Every log line in this request (and the http_request summary) carries the user id — never the email
    structlog.contextvars.bind_contextvars(user_id=user.user_id)
    request.state.user_id = user.user_id
    return user


async def get_owned_team(team_id: int, user: User = Depends(get_db_user)) -> Team:
    """Return the team only if it belongs to the caller.

    `team_id` binds from the path when the route declares {team_id},
    from the query string otherwise, so one dependency serves both styles.
    """
    team = ensure_team_owned(team_id, user.user_id)
    if team is None:
        raise NotFoundError("TEAM_NOT_FOUND", "Team not found")
    return team


def ensure_team_owned(team_id: int, user_id: int) -> Optional[Team]:
    """Fetch a team scoped to its owner; None when it doesn't exist or isn't theirs."""
    return Team.get_or_none((Team.team_id == team_id) & (Team.user_id == user_id))


async def get_owned_lineup(lineup_id: int, user: User = Depends(get_db_user)) -> Lineup:
    """Return the lineup only if its team belongs to the caller."""
    lineup = _find_owned_lineup(lineup_id, user.user_id)
    if lineup is None:
        raise NotFoundError("LINEUP_NOT_FOUND", "Lineup not found")
    return lineup


def _find_owned_lineup(lineup_id: int, user_id: int) -> Optional[Lineup]:
    return (
        Lineup.select()
        .join(Team, on=(Lineup.team_id == Team.team_id))
        .where((Lineup.lineup_id == lineup_id) & (Team.user_id == user_id))
        .first()
    )
