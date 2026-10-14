"""
Adopt the ESPN league a league-pending draft room connected to.

A live room started without a team (`league_pending`) exists for one moment: a
public league joined minutes before its draft, with no time to add the team to
Court Vision first. The room opens on placeholder settings; the Draft Tap links
it to whatever ESPN draft it connects to (`DraftSyncService`); and this service
turns that link into a real team and league — the same rows the add-team flow
writes, from the same ESPN read — and attaches them to the room, whose board
then values the league's own scoring and roster.

Runs after every INIT that leaves the room pending, so a failure (ESPN down, a
private league with no ESPN connection on the account, a spectator's INIT that
names no team) is reported, costs the draft nothing — the picks still synced —
and is retried on the next connect.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from core.errors import BadRequestError
from core.logging import get_logger
from core.settings import settings
from db.base import DB_RUNTIME_ERRORS, db, run_db
from db.models.drafts import DraftSession
from db.models.teams import Team
from schemas.common import FantasyProvider, LeagueInfo
from schemas.draft import DraftInitSyncResponse, DraftLeagueAdoption
from services.draft_service import DraftService, league_pending, lock_room
from services.espn_service import EspnService
from services.league_service import LeagueService
from services.team_service import TeamService

log = get_logger(__name__)


class DraftAdoptionService:

    @staticmethod
    async def adopt_after_init(
        session_id: int, user_id: int, resp: DraftInitSyncResponse
    ) -> DraftInitSyncResponse:
        """Fill `league_adopted` / `league_adoption_error` on a pending room's INIT response.

        A room that is not pending (or an INIT that linked nothing) passes
        through untouched, which keeps this a no-op for every other room.
        """
        data = resp.data
        if data is None or not data.session.league_pending or data.espn_league_id is None:
            return resp
        try:
            adopted = await DraftAdoptionService.adopt(
                session_id, user_id, data.espn_league_id, data.espn_team_id
            )
        except DB_RUNTIME_ERRORS:
            raise
        except Exception as exc:  # provider errors (typed AppErrors included) never fail the sync
            message = getattr(exc, "message", None) or str(exc)
            log.warning(
                "draft_league_adoption_failed", session_id=session_id,
                espn_league_id=data.espn_league_id, espn_team_id=data.espn_team_id,
                error=message, error_code=getattr(exc, "error_code", None),
            )
            data.league_adoption_error = message
            return resp
        data.league_adopted = adopted
        # The room now carries its team and league; say so in the same reply.
        data.session = (await DraftService.get_session(session_id)).data
        return resp

    @staticmethod
    async def adopt(
        session_id: int, user_id: int, espn_league_id: int, espn_team_id: int
    ) -> DraftLeagueAdoption:
        """Turn the ESPN draft the room linked to into the user's team and league on it."""
        # Cookies: the account's ESPN connection when it has exactly one; none
        # otherwise, which reads a public league fine and fails a private one
        # with ESPN's own refusal.
        probe = LeagueInfo(
            provider=FantasyProvider.ESPN, league_id=int(espn_league_id), team_name="?",
            year=int(settings.espn_year), espn_team_id=int(espn_team_id),
        )
        league_info = await run_db(
            "drafts.adopt_connection", TeamService._resolve_connection_handle, user_id, probe
        )

        # One read of the league: its teams, for the name ESPN gives yours, and
        # its settings, for the sync — the same views the add-team check reads.
        data = await EspnService.fetch_league(league_info, ["mTeam", "mSettings"])
        mine = next(
            (t for t in data.get("teams", []) if _int(t.get("id")) == int(espn_team_id)), None
        )
        name = str((mine or {}).get("name") or "").strip()
        if not name:
            raise BadRequestError(
                "DRAFT_ADOPT_TEAM_NOT_FOUND",
                f"ESPN team {espn_team_id} is not in league {espn_league_id}; connect from the "
                "ESPN account that owns a team in it",
            )
        league_info.team_name = name
        league_name = (data.get("settings") or {}).get("name")
        if league_name:
            league_info.league_name = str(league_name)

        # The rows the add-team flow writes, by the same identifier, so a team
        # already on the account is found rather than duplicated.
        identifier = str(league_info.league_id) + league_info.team_name
        team_id, already_exists, has_league = await run_db(
            "drafts.adopt_team", TeamService._create_or_find, user_id, identifier, league_info
        )
        if not has_league:
            await LeagueService.sync_for_team(team_id, league_info, espn_payload=data)

        league_id = await run_db("drafts.adopt_attach", DraftAdoptionService._attach, session_id, team_id)
        log.info(
            "draft_league_adopted", session_id=session_id, team_id=team_id, league_id=league_id,
            espn_league_id=espn_league_id, espn_team_id=espn_team_id, team_created=not already_exists,
        )
        return DraftLeagueAdoption(
            team_id=team_id, league_id=league_id,
            espn_league_id=int(espn_league_id), espn_team_id=int(espn_team_id),
            team_name=name, league_name=league_info.league_name if league_name else None,
            team_created=not already_exists,
        )

    @staticmethod
    def _attach(session_id: int, team_id: int) -> Optional[int]:
        """Give the room its team and league; the league answers for scoring from here on."""
        with db.atomic():
            lock_room(session_id)
            session = DraftService._session_or_404(session_id)
            team = Team.get_by_id(team_id)
            if not league_pending(session):
                # A concurrent INIT adopted first; its answer stands.
                return session.league_id
            session.team = team.team_id
            session.league = team.league_id
            session.scoring_format = None     # exclusive with a team (migration 0021's CHECK)
            session.updated_at = datetime.utcnow()
            session.save(only=[
                DraftSession.team, DraftSession.league, DraftSession.scoring_format, DraftSession.updated_at,
            ])
            return team.league_id


def _int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
