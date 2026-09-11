"""
Provider connections as something a user manages: list them, connect (or
refresh) an ESPN account, check that its cookies still work, list the teams on
it, remove one.

Storage and encryption stay in `credential_service`. This module is what
surrounds them: the ESPN check that runs before a cookie pair is saved -- the
row is shared by every team on the account, so a bad paste must never reach
it -- and the client-facing views, which never carry a credential.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal, Optional

from core import crypto
from core.errors import BadRequestError, NotFoundError, ProviderAuthError, ServiceUnavailableError
from core.logging import get_logger
from db.base import run_db
from schemas.common import ApiStatus
from schemas.connections import (
    ConnectionTeamInfo,
    EspnAccountTeam,
    EspnAccountTeamsResp,
    ProviderConnectionDeleteData,
    ProviderConnectionDeleteResp,
    ProviderConnectionInfo,
    ProviderConnectionListResp,
    ProviderConnectionResp,
)
from services import credential_service
from services.espn_service import ESPN_ACCOUNT_NOT_FOUND, EspnService
from services.providers.http import LEAGUE_NOT_FOUND_CODE

log = get_logger("connections")

CONNECTION_NOT_FOUND = "CONNECTION_NOT_FOUND"

# Leagues read, at most, to confirm a cookie pair (see _check_espn_cookies)
MAX_LEAGUE_PROBES = 3

UNCONFIRMED = "none of this account's leagues is private, so ESPN couldn't confirm the cookies"

CookieVerdict = Literal["accepted", "refused", "unconfirmed"]


def connection_status(
    verified_at: Optional[datetime], auth_failed_at: Optional[datetime]
) -> Literal["ok", "expired", "unknown"]:
    """What the last verdicts on record say about a connection's credentials.

    A failure no older than the last success means expired. Replacing the
    credentials clears both verdicts, so a fresh pair reads unknown until it is
    checked -- or ok, when it was checked on the way in.
    """
    if auth_failed_at is not None and (verified_at is None or auth_failed_at >= verified_at):
        return "expired"
    if verified_at is not None:
        return "ok"
    return "unknown"


def _account_hint(provider: str, external_account_id: str) -> Optional[str]:
    """Four characters of the ESPN account id: enough to tell two accounts apart
    in a list, too few to be the SWID."""
    guid = (external_account_id or "").strip("{}")
    if provider != "espn" or len(guid) < 4:
        return None
    return "…" + guid[-4:]


# ---- the account as ESPN's fan API describes it ------------------------------

def _fan_entries(payload: dict) -> list[dict]:
    """The account's fantasy basketball entries -- one per team -- in a fan payload.

    Each preference of type "fantasy" is a team; `abbrev` "FBA" is fantasy
    basketball (the `fba` of the league endpoint), and its one group is the
    league. `lobby` marks ESPN's public draft-lobby leagues.
    """
    entries = []
    for preference in payload.get("preferences") or []:
        entry = ((preference.get("metaData") or {}).get("entry")) or {}
        if (preference.get("type") or {}).get("code") != "fantasy" or str(entry.get("abbrev", "")).upper() != "FBA":
            continue
        meta = entry.get("entryMetadata") or {}
        group = (entry.get("groups") or [None])[0] or {}
        try:
            league_id, season, espn_team_id = int(group["groupId"]), int(entry["seasonId"]), int(entry["entryId"])
        except (KeyError, TypeError, ValueError):
            continue
        entries.append({
            "league_id": league_id,
            "season": season,
            "espn_team_id": espn_team_id,
            "team_name": meta.get("teamName") or "",
            "team_abbrev": meta.get("teamAbbrev"),
            "league_name": group.get("groupName"),
            "league_size": group.get("groupSize"),
            "scoring_type": meta.get("scoringTypeName"),
            "lobby": meta.get("leagueSubTypeName") == "DRAFT_LOBBY",
        })
    return entries


def espn_fan_teams(payload: dict) -> list[EspnAccountTeam]:
    """The account's fantasy basketball teams, newest season first."""
    teams = [
        EspnAccountTeam(**{k: v for k, v in entry.items() if k != "lobby"})
        for entry in _fan_entries(payload)
    ]
    return sorted(teams, key=lambda t: (-t.season, t.league_name or "", t.espn_team_id))


def _probe_leagues(payload: dict) -> list[tuple[int, int]]:
    """(season, league_id) pairs to read when confirming cookies: one per league,
    the account's own leagues before public draft lobbies, newest season first."""
    seen: set[tuple[int, int]] = set()
    order: list[tuple[int, int]] = []
    for entry in sorted(_fan_entries(payload), key=lambda e: (e["lobby"], -e["season"])):
        key = (entry["season"], entry["league_id"])
        if key not in seen:
            seen.add(key)
            order.append(key)
    return order[:MAX_LEAGUE_PROBES]


async def _check_espn_cookies(espn_s2: str, swid: str) -> CookieVerdict:
    """Whether ESPN accepts a cookie pair.

    The fan API answers 200 for any SWID it knows whatever the cookies, so it
    proves the account exists, not that the cookies work -- beyond flagging a
    read it treated as logged out (`anon: true`), which is a refusal. The proof
    is a private league: ESPN refuses to show one (401/403) unless the cookies
    are good. The fan payload names the account's leagues, and the first
    private one settles it; an account with only public leagues leaves the pair
    unconfirmed.

    Raises BadRequestError ESPN_ACCOUNT_NOT_FOUND for a SWID ESPN does not know,
    and the usual provider errors for an outage, which prove nothing.
    """
    try:
        fan = await EspnService.fetch_fan(espn_s2, swid)
    except ProviderAuthError:
        return "refused"
    if fan.get("anon") is True:
        return "refused"

    for season, league_id in _probe_leagues(fan):
        try:
            settings = await EspnService.fetch_league_settings(espn_s2, swid, season, league_id)
        except ProviderAuthError:
            return "refused"
        except BadRequestError as exc:
            if exc.error_code != LEAGUE_NOT_FOUND_CODE:
                raise
            continue
        if settings.get("isPublic") is False:
            return "accepted"
    return "unconfirmed"


# ---- repository functions (run through run_db) --------------------------------

def _views(user_id: int, connection_id: Optional[int] = None) -> list[ProviderConnectionInfo]:
    """The client-facing rows for a user's connections, or one of them. Never decrypts."""
    from db.models.provider_connections import ProviderConnection
    from db.models.teams import Team

    query = ProviderConnection.select().where(ProviderConnection.user == user_id)
    if connection_id is not None:
        query = query.where(ProviderConnection.id == connection_id)
    connections = list(query.order_by(ProviderConnection.provider, ProviderConnection.created_at))
    if not connections:
        return []

    teams: dict[int, list[ConnectionTeamInfo]] = {}
    linked = (
        Team.select(Team.team_id, Team.league_info, Team.provider_connection)
        .where((Team.user_id == user_id) & Team.provider_connection.in_([c.id for c in connections]))
        .order_by(Team.team_id)
    )
    for team in linked:
        info = json.loads(team.league_info)
        teams.setdefault(team.provider_connection_id, []).append(ConnectionTeamInfo(
            team_id=team.team_id,
            team_name=info.get("team_name") or "",
            league_name=info.get("league_name"),
            league_id=info.get("league_id"),
            year=info.get("year"),
        ))

    return [
        ProviderConnectionInfo(
            id=c.id,
            provider=c.provider,
            account_hint=_account_hint(c.provider, c.external_account_id),
            status=connection_status(c.verified_at, c.auth_failed_at),
            verified_at=c.verified_at,
            auth_failed_at=c.auth_failed_at,
            created_at=c.created_at,
            updated_at=c.updated_at,
            teams=teams.get(c.id, []),
        )
        for c in connections
    ]


def _tracked_espn_teams(user_id: int) -> list[tuple[int, dict]]:
    """(team_id, league_info) for the user's ESPN teams -- non-secret fields only."""
    from db.models.teams import Team

    tracked = []
    for team in Team.select(Team.team_id, Team.league_info).where(Team.user_id == user_id).order_by(Team.team_id):
        info = json.loads(team.league_info)
        if info.get("provider", "espn") == "espn":
            tracked.append((team.team_id, info))
    return tracked


def _mark_tracked(teams: list[EspnAccountTeam], tracked: list[tuple[int, dict]]) -> list[EspnAccountTeam]:
    """Mark each ESPN team with the Court Vision team already tracking it.

    ESPN's team id identifies a team through renames, but Court Vision learns it
    only on a first roster read, so an unmatched id falls back to the name.
    """
    by_id: dict[tuple, int] = {}
    by_name: dict[tuple, int] = {}
    for team_id, info in tracked:
        league_id, year = info.get("league_id"), info.get("year")
        if info.get("espn_team_id") is not None:
            by_id[(league_id, year, info["espn_team_id"])] = team_id
        if info.get("team_name"):
            by_name[(league_id, year, info["team_name"].strip())] = team_id
    return [
        team.model_copy(update={"tracked_team_id": (
            by_id.get((team.league_id, team.season, team.espn_team_id))
            or by_name.get((team.league_id, team.season, team.team_name.strip()))
        )})
        for team in teams
    ]


def _require_store() -> None:
    if not crypto.is_enabled():
        raise ServiceUnavailableError(
            "CREDENTIAL_STORE_UNAVAILABLE",
            "Connections can't be saved right now: the credential store is not configured",
        )


class ConnectionService:

    @staticmethod
    async def list_for_user(user_id: int) -> ProviderConnectionListResp:
        views = await run_db("connections.list", _views, user_id)
        return ProviderConnectionListResp(
            status=ApiStatus.SUCCESS, message=f"Found {len(views)} connections", data=views
        )

    @staticmethod
    async def connect_espn(user_id: int, espn_s2: str, swid: str) -> ProviderConnectionResp:
        """Connect an ESPN account, or refresh the cookies of one already connected.

        ESPN sees the pair before anything is written: a pair it refuses, or a
        SWID it has never heard of, raises and is not stored. One it can neither
        accept nor refuse (no private league to read) is stored, status unknown.
        """
        _require_store()
        espn_s2 = (espn_s2 or "").strip()
        swid = credential_service.normalize_swid(swid)
        if not espn_s2 or not swid:
            raise BadRequestError("ESPN_COOKIES_REQUIRED", "Paste both espn_s2 and SWID")

        verdict = await _check_espn_cookies(espn_s2, swid)
        if verdict == "refused":
            # Not the league-read wording ("check ... the season"): no league was named
            raise ProviderAuthError(
                "espn", "ESPN rejected these cookies — copy espn_s2 and SWID again while logged in to ESPN"
            )

        connection_id, created = await run_db(
            "connections.store_espn", credential_service.store_espn_cookies,
            user_id, espn_s2, swid, verified=verdict == "accepted",
        )
        (view,) = await run_db("connections.view", _views, user_id, connection_id)
        log.info("espn_connection_saved", connection_id=connection_id, created=created,
                 verdict=verdict, teams=len(view.teams))
        message = "ESPN account connected" if created else "ESPN cookies updated"
        if verdict == "unconfirmed":
            message = f"{message} — {UNCONFIRMED}"
        return ProviderConnectionResp(status=ApiStatus.SUCCESS, message=message, data=view, created=created)

    @staticmethod
    async def verify(user_id: int, connection_id: int) -> ProviderConnectionResp:
        """Ask ESPN whether a stored connection's cookies still work, and record the answer.

        A refusal is the answer, not a failure of the request: it comes back as
        a success whose connection reads "expired". An outage raises and records
        nothing, as does an account with no private league to check against --
        neither says anything about the cookies.
        """
        _require_store()
        secrets = await run_db(
            "connections.load", credential_service.load_provider_tokens,
            user_id, connection_id, provider="espn",
        )
        if secrets is None:
            raise NotFoundError(CONNECTION_NOT_FOUND, "No ESPN connection with that id")

        try:
            verdict = await _check_espn_cookies(secrets.get("espn_s2", ""), secrets.get("swid", ""))
        except BadRequestError as exc:
            if exc.error_code != ESPN_ACCOUNT_NOT_FOUND:
                raise
            verdict = "refused"

        if verdict != "unconfirmed":
            await run_db("connections.mark_checked", credential_service.mark_checked,
                         connection_id, verdict == "accepted")
        (view,) = await run_db("connections.view", _views, user_id, connection_id)
        log.info("espn_connection_checked", connection_id=connection_id, verdict=verdict)
        message = {
            "accepted": "ESPN accepted these cookies",
            "refused": "ESPN rejected these cookies",
            "unconfirmed": UNCONFIRMED[0].upper() + UNCONFIRMED[1:],
        }[verdict]
        return ProviderConnectionResp(status=ApiStatus.SUCCESS, message=message, data=view)

    @staticmethod
    async def espn_teams(user_id: int, connection_id: int) -> EspnAccountTeamsResp:
        """The fantasy basketball teams on a connected ESPN account, as ESPN lists
        them, each marked with the Court Vision team already tracking it."""
        _require_store()
        secrets = await run_db(
            "connections.load", credential_service.load_provider_tokens,
            user_id, connection_id, provider="espn",
        )
        if secrets is None:
            raise NotFoundError(CONNECTION_NOT_FOUND, "No ESPN connection with that id")

        fan = await EspnService.fetch_fan(secrets.get("espn_s2", ""), secrets.get("swid", ""))
        tracked = await run_db("connections.tracked_teams", _tracked_espn_teams, user_id)
        teams = _mark_tracked(espn_fan_teams(fan), tracked)
        return EspnAccountTeamsResp(
            status=ApiStatus.SUCCESS, message=f"Found {len(teams)} ESPN teams", data=teams
        )

    @staticmethod
    async def delete(user_id: int, connection_id: int) -> ProviderConnectionDeleteResp:
        unlinked = await run_db(
            "connections.delete", credential_service.delete_connection, user_id, connection_id
        )
        if unlinked is None:
            raise NotFoundError(CONNECTION_NOT_FOUND, "Connection not found")
        log.info("provider_connection_deleted", connection_id=connection_id, unlinked_teams=len(unlinked))
        return ProviderConnectionDeleteResp(
            status=ApiStatus.SUCCESS,
            message="Connection removed",
            data=ProviderConnectionDeleteData(id=connection_id, unlinked_team_ids=unlinked),
        )
