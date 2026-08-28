"""
Saved teams: the usr.teams rows holding a user's league credentials.

Failures raise `core.errors` AppErrors — 404 TEAM_NOT_FOUND, 400
LEAGUE_VALIDATION_FAILED / TEAM_NAME_NOT_IN_LEAGUE when the provider cannot
validate the league — and provider problems propagate from EspnService /
YahooService untouched (403 PROVIDER_AUTH_EXPIRED, 400 LEAGUE_NOT_FOUND,
502/504). Database errors reach the global handlers (503 DATABASE_UNAVAILABLE).
"""

import json

from peewee import JOIN

from core.errors import BadRequestError, NotFoundError
from core.logging import get_logger
from db.models import Team, League
from db.base import db_operation, db, run_db
from schemas.common import ApiStatus, LeagueInfo, LeagueInfoPublic, FantasyProvider
from schemas.espn import ValidateLeagueResp
from schemas.team import TeamGetResp, TeamAddResp, TeamRemoveResp, TeamUpdateResp, TeamResponse, TeamViewResp
from services import credential_service
from services.league_service import LeagueService
from services.espn_service import EspnService
from services.yahoo_service import YahooService

LEAGUE_VALIDATION_FAILED = "LEAGUE_VALIDATION_FAILED"


class TeamService:

    @staticmethod
    @db_operation("teams.list")
    def get_teams(user_id: int) -> TeamGetResp:
        teams_query = Team.select(Team, League).join(League, JOIN.LEFT_OUTER).where(Team.user_id == user_id)
        teams: list[TeamResponse] = [TeamService._to_team_response(team) for team in teams_query]
        return TeamGetResp(status=ApiStatus.SUCCESS, message="Teams fetched successfully", data=teams)

    @staticmethod
    def _to_team_response(team: Team, league_info: LeagueInfo | None = None) -> TeamResponse:
        """The client-facing view of a team.

        Deliberately does **not** hydrate: credentials never travel to the
        browser. `LeagueInfoPublic` cannot hold them, and `has_credentials`
        answers "are they on file" from the connection link without decrypting.
        """
        league = team.league if team.league_id is not None else None
        stored = json.loads(team.league_info)
        info = league_info or TeamService.deserialize_league_info(stored)
        public = LeagueInfoPublic.from_league_info(
            info, has_credentials=credential_service.has_credentials(team, stored)
        )
        return TeamResponse(
            team_id=team.team_id,
            league_info=public,
            league=LeagueService.summary_for_team(league, info),
        )

    @staticmethod
    def serialize_league_info(league_info: LeagueInfo) -> str:
        """Serialize LeagueInfo to JSON string, preserving provider-specific fields."""
        data = {
            "provider": league_info.provider.value if hasattr(league_info.provider, 'value') else str(league_info.provider),
            "league_id": league_info.league_id,
            "team_name": league_info.team_name,
            "league_name": league_info.league_name if league_info.league_name else "N/A",
            "year": league_info.year,
            # ESPN-specific
            "espn_s2": league_info.espn_s2,
            "swid": league_info.swid,
            # Yahoo-specific
            "yahoo_access_token": league_info.yahoo_access_token,
            "yahoo_refresh_token": league_info.yahoo_refresh_token,
            "yahoo_token_expiry": league_info.yahoo_token_expiry,
            "yahoo_team_key": league_info.yahoo_team_key,
            # Per-team display override (see LeagueInfo.scoring_preview)
            "scoring_preview": league_info.scoring_preview,
        }
        return json.dumps(data)

    @staticmethod
    def deserialize_league_info(league_info: dict, team=None) -> LeagueInfo:
        """Deserialize JSON dict to LeagueInfo, defaulting to ESPN for backward compatibility.

        Pass `team` wherever the result will be used to call a provider: the
        credentials may live in the encrypted store rather than in this dict.
        Omit it on paths that only need the non-secret fields.
        """
        if team is not None:
            league_info = credential_service.hydrate(team, league_info)
        # Default to ESPN for existing records without provider field
        provider_str = league_info.get('provider', 'espn')
        try:
            provider = FantasyProvider(provider_str)
        except ValueError:
            provider = FantasyProvider.ESPN

        return LeagueInfo(
            provider=provider,
            league_id=league_info.get('league_id'),
            team_name=league_info.get('team_name'),
            league_name=league_info.get('league_name'),
            year=league_info.get('year'),
            # ESPN-specific
            espn_s2=league_info.get('espn_s2', ''),
            swid=league_info.get('swid', ''),
            # Yahoo-specific
            yahoo_access_token=league_info.get('yahoo_access_token'),
            yahoo_refresh_token=league_info.get('yahoo_refresh_token'),
            yahoo_token_expiry=league_info.get('yahoo_token_expiry'),
            yahoo_team_key=league_info.get('yahoo_team_key'),
            scoring_preview=league_info.get('scoring_preview') or None,
        )

    # ---- validation -----------------------------------------------------------

    @staticmethod
    async def _validate_league(league_info: LeagueInfo, team_id: int | None = None) -> ValidateLeagueResp:
        """Ask the provider whether these credentials reach this team.

        `valid=False` means the credentials work but the team name isn't in the
        league; provider failures (rejected cookies/token, unknown league, outage)
        raise typed AppErrors from the provider services.
        """
        if league_info.provider == FantasyProvider.YAHOO:
            return await YahooService.check_league(league_info, team_id)
        return await EspnService.check_league(league_info)

    @staticmethod
    def _rejected(validation: ValidateLeagueResp) -> BadRequestError:
        return BadRequestError(
            validation.error_code or LEAGUE_VALIDATION_FAILED,
            validation.message or "Invalid league information",
        )

    # ---- CRUD -----------------------------------------------------------------

    @staticmethod
    def _create_or_find(user_id: int, team_identifier: str, league_info: LeagueInfo) -> tuple[int, bool, bool]:
        with db.atomic():
            existing = Team.get_or_none(
                (Team.user_id == user_id) & (Team.team_identifier == team_identifier)
            )
            if existing is not None:
                return existing.team_id, True, existing.league_id is not None
            serialized = TeamService.serialize_league_info(league_info)
            team = Team.create(user_id=user_id, team_identifier=team_identifier, league_info=serialized)
            credential_service.persist(user_id, team, json.loads(serialized))
            return team.team_id, False, False

    @staticmethod
    def _response_for(team_id: int, league_info: LeagueInfo) -> TeamResponse:
        team = Team.select(Team, League).join(League, JOIN.LEFT_OUTER).where(Team.team_id == team_id).get()
        return TeamService._to_team_response(team, league_info)

    @staticmethod
    def _persist_update(user_id: int, team_id: int, league_info: LeagueInfo) -> None:
        serialized = TeamService.serialize_league_info(league_info)
        with db.atomic():
            team = Team.get_or_none((Team.user_id == user_id) & (Team.team_id == team_id))
            if team is None:
                raise NotFoundError("TEAM_NOT_FOUND", "Team not found")
            team.league_info = serialized
            team.save(only=[Team.league_info])
            credential_service.persist(user_id, team, json.loads(serialized))

    @staticmethod
    def _resolve_connection_handle(user_id: int, league_info: LeagueInfo) -> LeagueInfo:
        """Swap an opaque `yahoo_connection_id` for the tokens it refers to.

        The OAuth callback stores Yahoo tokens server-side and hands the browser
        only this id, so the add-team request cannot carry the credentials that
        `_validate_league` needs. Resolution is scoped to the caller: another
        user's connection id resolves to nothing.
        """
        if not league_info.yahoo_connection_id:
            return league_info
        secrets = credential_service.load_provider_tokens(user_id, league_info.yahoo_connection_id)
        if not secrets:
            raise BadRequestError("YAHOO_CONNECTION_NOT_FOUND",
                                  "Yahoo connection not found; reconnect your account")
        merged = league_info.model_copy()
        for field, value in secrets.items():
            if value:
                setattr(merged, field, value)
        return merged

    @staticmethod
    def _merge_stored_credentials(user_id: int, team_id: int, incoming: LeagueInfo) -> LeagueInfo:
        """Fill in any credential the caller left blank from the one on file.

        The edit form no longer receives secrets, so it cannot send them back. A
        blank credential field therefore means "keep what you have" — under the
        previous full-overwrite behaviour it would have wiped a user's ESPN
        cookies the first time they renamed a team.

        Doubles as the ownership check: raises TEAM_NOT_FOUND for a team that is
        not the caller's, in the same read.
        """
        team = Team.get_or_none((Team.user_id == user_id) & (Team.team_id == team_id))
        if team is None:
            raise NotFoundError("TEAM_NOT_FOUND", "Team not found")

        stored = credential_service.hydrate(team, json.loads(team.league_info))
        merged = incoming.model_copy()
        for field in credential_service.ALL_SECRET_FIELDS:
            if not getattr(merged, field, None) and stored.get(field):
                setattr(merged, field, stored[field])
        return merged

    @staticmethod
    async def add_team(user_id: int, league_info: LeagueInfo) -> TeamAddResp:
        league_info = await run_db(
            "teams.resolve_connection", TeamService._resolve_connection_handle,
            user_id, league_info,
        )
        validation_result = await TeamService._validate_league(league_info)
        if not validation_result.valid:
            # Once more with the team name stripped (mobile clients send trailing whitespace)
            league_info.team_name = league_info.team_name.strip(" \t\n\r")
            validation_result = await TeamService._validate_league(league_info)
            if not validation_result.valid:
                raise TeamService._rejected(validation_result)
        team_identifier = str(league_info.league_id) + league_info.team_name
        team_id, already_exists, has_league = await run_db(
            "teams.create_or_find", TeamService._create_or_find,
            user_id, team_identifier, league_info,
        )
        if not has_league:
            await LeagueService.sync_for_team(team_id, league_info, espn_payload=validation_result.league_payload)
        response = await run_db("teams.response", TeamService._response_for, team_id, league_info)
        return TeamAddResp(
            status=ApiStatus.SUCCESS,
            message="Team already exists" if already_exists else "Team added successfully",
            data=response,
            team_id=team_id,
            already_exists=already_exists,
        )

    @staticmethod
    @db_operation("teams.remove")
    def remove_team(user_id: int, team_id: int) -> TeamRemoveResp:
        team = Team.select().where(Team.user_id == user_id).where(Team.team_id == team_id).first()
        if not team:
            raise NotFoundError("TEAM_NOT_FOUND", "Team not found")

        team.delete_instance()
        return TeamRemoveResp(status=ApiStatus.SUCCESS, message="Team removed successfully", data=team.team_id)

    @staticmethod
    async def update_team(user_id: int, team_id: int, league_info: LeagueInfo) -> TeamUpdateResp:
        # Must precede validation: _validate_league calls the provider, and the
        # caller may have sent none of the credentials it needs.
        league_info = await run_db(
            "teams.merge_credentials", TeamService._merge_stored_credentials,
            user_id, team_id, league_info,
        )
        validation_result = await TeamService._validate_league(league_info, team_id)
        if not validation_result.valid:
            raise TeamService._rejected(validation_result)
        await run_db("teams.persist_update", TeamService._persist_update, user_id, team_id, league_info)
        await LeagueService.sync_for_team(team_id, league_info, espn_payload=validation_result.league_payload)
        response = await run_db("teams.response", TeamService._response_for, team_id, league_info)
        return TeamUpdateResp(status=ApiStatus.SUCCESS, message="Team updated successfully",
                              data=response)

    @staticmethod
    @db_operation("teams.view")
    def view_team(team_id: int) -> TeamViewResp:
        """The stored team (league info + synced league summary). Raises TEAM_NOT_FOUND."""
        team = Team.select(Team, League).join(League, JOIN.LEFT_OUTER).where(Team.team_id == team_id).first()
        if not team:
            raise NotFoundError("TEAM_NOT_FOUND", "Team not found")

        # This will be passed to the ESPN/Yahoo service to get the roster data
        return TeamViewResp(status=ApiStatus.SUCCESS, message="Team fetched successfully", data=TeamService._to_team_response(team))

    @staticmethod
    @db_operation("teams.update_yahoo_tokens")
    def update_yahoo_tokens(
        team_id: int,
        access_token: str,
        refresh_token: str,
        token_expiry: str
    ) -> bool:
        """
        Update only the Yahoo OAuth tokens for a team.

        Used when tokens are automatically refreshed during API calls. Best-effort:
        a failure to persist is logged and the refreshed token still serves this request.

        Args:
            team_id: The team's ID
            access_token: New Yahoo access token
            refresh_token: New Yahoo refresh token
            token_expiry: New token expiry datetime (ISO format)

        Returns:
            True if update succeeded, False otherwise
        """
        log = get_logger()
        try:
            team = Team.select().where(Team.team_id == team_id).first()
            if not team:
                log.warning("yahoo_token_update_skipped", team_id=team_id, reason="team not found")
                return False

            # Refreshed tokens follow the credentials: the encrypted store when
            # this team has been migrated, league_info when it has not.
            if credential_service.update_yahoo_tokens(team, access_token, refresh_token, token_expiry):
                return True

            league_info_dict = json.loads(team.league_info)
            league_info_dict["yahoo_access_token"] = access_token
            league_info_dict["yahoo_refresh_token"] = refresh_token
            league_info_dict["yahoo_token_expiry"] = token_expiry

            Team.update(league_info=json.dumps(league_info_dict)).where(
                Team.team_id == team_id
            ).execute()

            return True

        except Exception:
            log.exception("yahoo_token_update_failed", team_id=team_id)
            return False
