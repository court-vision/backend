"""
Saved teams: the usr.teams rows holding a user's league credentials.

Failures raise `core.errors` AppErrors — 404 TEAM_NOT_FOUND, 400
LEAGUE_VALIDATION_FAILED / TEAM_NAME_NOT_IN_LEAGUE when the provider cannot
validate the league — and provider problems propagate from EspnService /
YahooService untouched (403 PROVIDER_AUTH_EXPIRED, 400 LEAGUE_NOT_FOUND,
502/504). Database errors reach the global handlers (503 DATABASE_UNAVAILABLE).
"""

import json
from typing import NamedTuple

from peewee import JOIN

from core.errors import BadRequestError, NotFoundError
from core.logging import get_logger
from db.models import Team, League
from db.base import db_operation, db, run_db
from schemas.common import ApiStatus, LeagueInfo, LeagueInfoPublic, FantasyProvider
from schemas.espn import ValidateLeagueResp
from schemas.team import TeamGetResp, TeamAddResp, TeamRemoveResp, TeamUpdateResp, TeamResponse, TeamViewResp
from services import credential_service
from services.espn_team_match import find_espn_team
from services.league_service import LeagueService
from services.providers import get_provider_adapter
from schemas.common import ProviderCapabilitiesResp

LEAGUE_VALIDATION_FAILED = "LEAGUE_VALIDATION_FAILED"


class SavedTeam(NamedTuple):
    """What `_create_or_find` did: found the team, created it, or moved it to a new season."""
    team_id: int
    created: bool
    has_league: bool
    # The season the team was on before it moved to the requested one
    moved_from_season: int | None = None


def _season_of(info: dict) -> int | None:
    try:
        return int(info.get("year"))
    except (TypeError, ValueError):
        return None


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
            capabilities=ProviderCapabilitiesResp(
                **get_provider_adapter(info.provider).capabilities(info).as_dict()
            ),
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
            "espn_team_id": league_info.espn_team_id,
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
            espn_team_id=league_info.get('espn_team_id'),
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
        return await get_provider_adapter(league_info.provider).validate_league(league_info, team_id)

    @staticmethod
    def _rejected(validation: ValidateLeagueResp) -> BadRequestError:
        return BadRequestError(
            validation.error_code or LEAGUE_VALIDATION_FAILED,
            validation.message or "Invalid league information",
        )

    # ---- CRUD -----------------------------------------------------------------

    @staticmethod
    def _create_or_find(
        user_id: int, team_identifier: str, league_info: LeagueInfo, league_teams: list[dict] | None = None,
    ) -> SavedTeam:
        """The user's team for `league_info`: the one already saved, or a new one.

        A team saved for an earlier season of the same ESPN league is the same
        team -- ESPN renews a league under the same id and team ids -- so it
        moves to this season instead of answering with last season's league.
        `league_teams` (the league's `teams` in this season's payload) lets a
        team saved before its ESPN id was known be recognized after a rename.
        """
        with db.atomic():
            existing = TeamService._saved_copy(user_id, team_identifier, league_info, league_teams)
            if existing is not None:
                stored = json.loads(existing.league_info)
                season = _season_of(stored)
                if league_info.provider == FantasyProvider.ESPN and season is not None and season < int(league_info.year):
                    TeamService._move_to_season(user_id, existing, stored, team_identifier, league_info)
                    return SavedTeam(existing.team_id, created=False, has_league=False, moved_from_season=season)
                return SavedTeam(existing.team_id, created=False, has_league=existing.league_id is not None)
            serialized = TeamService.serialize_league_info(league_info)
            team = Team.create(user_id=user_id, team_identifier=team_identifier, league_info=serialized)
            credential_service.persist(user_id, team, json.loads(serialized))
            return SavedTeam(team.team_id, created=True, has_league=False)

    @staticmethod
    def _saved_copy(
        user_id: int, team_identifier: str, league_info: LeagueInfo, league_teams: list[dict] | None,
    ) -> Team | None:
        """The user's saved team that is this one: by identifier (league + name),
        else, for ESPN, by ESPN's team id in the same league, any season."""
        existing = Team.get_or_none((Team.user_id == user_id) & (Team.team_identifier == team_identifier))
        if league_info.provider != FantasyProvider.ESPN or league_info.espn_team_id is None:
            return existing
        if existing is not None:
            # The same name on a different ESPN team (a leaguemate took it) is not this team
            known = json.loads(existing.league_info).get("espn_team_id")
            if known is None or int(known) == int(league_info.espn_team_id):
                return existing
        same_league = []
        for team in Team.select().where(Team.user_id == user_id).order_by(Team.team_id):
            stored = json.loads(team.league_info)
            if stored.get("provider", "espn") == "espn" and str(stored.get("league_id")) == str(league_info.league_id):
                same_league.append((team, stored))
        # The newest season first: it is the one to move or to answer with
        same_league.sort(key=lambda pair: _season_of(pair[1]) or 0, reverse=True)
        for team, stored in same_league:
            if TeamService._is_espn_team(stored, league_info, league_teams):
                return team
        return None

    @staticmethod
    def _is_espn_team(stored: dict, league_info: LeagueInfo, league_teams: list[dict] | None) -> bool:
        """Whether a saved ESPN team (same league) is the ESPN team `league_info` resolved to.

        By the stored ESPN team id when one was learned. Otherwise the stored
        team is looked up in this season's teams the way every read finds it
        (find_espn_team: its name, else the one team the SWID owns), which is
        what recognizes a team renamed before its id was learned.
        """
        stored_id = stored.get("espn_team_id")
        if stored_id is not None:
            return int(stored_id) == int(league_info.espn_team_id)
        if not league_teams:
            return False
        found = find_espn_team(league_teams, team_name=stored.get("team_name"), swid=league_info.swid)
        return found is not None and found.get("id") == int(league_info.espn_team_id)

    @staticmethod
    def _move_to_season(user_id: int, team: Team, stored: dict, team_identifier: str, league_info: LeagueInfo) -> None:
        """Rewrite a saved team as `league_info`'s season of it, keeping what the add does not carry.

        The league link is cleared: it is last season's league, and the
        caller's sync links this season's.
        """
        data = json.loads(TeamService.serialize_league_info(league_info))
        for field in credential_service.ALL_SECRET_FIELDS:
            if not data.get(field) and stored.get(field):
                data[field] = stored[field]
        if data.get("league_name") in (None, "", "N/A") and stored.get("league_name"):
            data["league_name"] = stored["league_name"]
        if data.get("scoring_preview") is None and stored.get("scoring_preview"):
            data["scoring_preview"] = stored["scoring_preview"]
        team.league_info = json.dumps(data)
        team.team_identifier = team_identifier
        team.league = None
        team.save(only=[Team.league_info, Team.team_identifier, Team.league])
        credential_service.persist(user_id, team, data)
        get_logger().info("team_moved_to_season", team_id=team.team_id,
                          from_season=_season_of(stored), to_season=int(league_info.year))

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
            # The identifier follows the league and the name ESPN answered with,
            # or adding the team again would not find it
            team.team_identifier = str(league_info.league_id) + league_info.team_name
            team.save(only=[Team.league_info, Team.team_identifier])
            credential_service.persist(user_id, team, json.loads(serialized))

    @staticmethod
    def _resolve_connection_handle(
        user_id: int, league_info: LeagueInfo, *, default_espn: bool = True
    ) -> LeagueInfo:
        """Swap an opaque connection id for the credentials it refers to.

        Yahoo: the OAuth callback stores the tokens server-side and hands the
        browser only `yahoo_connection_id`, so the add-team request cannot carry
        the credentials that `_validate_league` needs.

        ESPN: cookies are stored once per ESPN account, so a team on an account
        already connected needs none pasted. Cookies in the request win; then
        `espn_connection_id`; then, when `default_espn`, the user's only ESPN
        connection. With none or several, the request goes on as sent.

        Resolution is scoped to the caller: another user's connection id
        resolves to nothing.
        """
        if league_info.yahoo_connection_id:
            secrets = credential_service.load_provider_tokens(user_id, league_info.yahoo_connection_id)
            if not secrets:
                raise BadRequestError("YAHOO_CONNECTION_NOT_FOUND",
                                      "Yahoo connection not found; reconnect your account")
            return TeamService._with_secrets(league_info, secrets)

        if league_info.provider != FantasyProvider.ESPN or (league_info.espn_s2 and league_info.swid):
            return league_info

        connection_id = league_info.espn_connection_id
        if connection_id is None:
            candidates = credential_service.connection_ids(user_id, "espn") if default_espn else []
            if len(candidates) != 1:
                return league_info
            connection_id = candidates[0]

        secrets = credential_service.load_provider_tokens(user_id, connection_id, provider="espn")
        if not secrets:
            raise BadRequestError("ESPN_CONNECTION_NOT_FOUND",
                                  "ESPN connection not found; connect your ESPN account in Manage Teams")
        return TeamService._with_secrets(league_info, secrets)

    @staticmethod
    def _with_secrets(league_info: LeagueInfo, secrets: dict) -> LeagueInfo:
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
        # The edit form does not know ESPN's team id either; keep what was learned,
        # but only for the same league and season: ids are per league, and the
        # same number in another league is somebody else's team.
        same_league = (str(stored.get("league_id")) == str(merged.league_id)
                       and str(stored.get("year")) == str(merged.year))
        if merged.espn_team_id is None and stored.get("espn_team_id") and same_league:
            merged.espn_team_id = stored["espn_team_id"]
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
        saved = await run_db(
            "teams.create_or_find", TeamService._create_or_find,
            user_id, team_identifier, league_info, (validation_result.league_payload or {}).get("teams"),
        )
        if not saved.has_league:
            await LeagueService.sync_for_team(saved.team_id, league_info, espn_payload=validation_result.league_payload)
        response = await run_db("teams.response", TeamService._response_for, saved.team_id, league_info)
        already_exists = not saved.created and saved.moved_from_season is None
        if saved.moved_from_season is not None:
            message = f"Team moved from season {saved.moved_from_season} to {league_info.year}"
        else:
            message = "Team already exists" if already_exists else "Team added successfully"
        return TeamAddResp(
            status=ApiStatus.SUCCESS,
            message=message,
            data=response,
            team_id=saved.team_id,
            already_exists=already_exists,
            moved_from_season=saved.moved_from_season,
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
        # An `espn_connection_id` (moving the team to another stored ESPN
        # account) resolves before the merge, so it wins over the cookies on
        # file. No default here: a team with none on file keeps having none.
        league_info = await run_db(
            "teams.resolve_connection", TeamService._resolve_connection_handle,
            user_id, league_info, default_espn=False,
        )
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
    @db_operation("teams.credentials")
    def credentials_for(team_id: int) -> LeagueInfo:
        """The team's league info **with** credentials, for calling a provider.

        Deliberately separate from `view_team`, which returns the client-facing
        shape and carries no secrets. Anything that talks to ESPN or Yahoo needs
        this; anything that answers an HTTP request needs that. Conflating them
        is what broke production on 2026-08-28 — two services were using
        `view_team` as a way to load credentials.
        """
        team = Team.select().where(Team.team_id == team_id).first()
        if team is None:
            raise NotFoundError("TEAM_NOT_FOUND", f"Team with ID {team_id} not found")
        return TeamService.deserialize_league_info(json.loads(team.league_info), team)

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
