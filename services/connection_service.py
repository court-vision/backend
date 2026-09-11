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
from typing import Any, Literal, Optional

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


def _adopted_note(n: int) -> str:
    return f"linked {n} {'team' if n == 1 else 'teams'} you already track"


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


def _team_keys(league_id: Any, espn_team_id: Any, team_name: Optional[str]) -> list[tuple]:
    """How a Court Vision team and an ESPN team are recognized as the same one.

    ESPN's team id inside the league when known -- it survives renames -- and
    the name otherwise. No season: Court Vision's own identity for a team is
    league id + name (`team_identifier`), so a team saved last season is the
    same team ESPN now lists under the league's renewal.
    """
    keys: list[tuple] = []
    if espn_team_id is not None:
        keys.append(("id", league_id, espn_team_id))
    if team_name and team_name.strip():
        keys.append(("name", league_id, team_name.strip()))
    return keys


async def _check_espn_cookies(espn_s2: str, swid: str) -> tuple[CookieVerdict, dict]:
    """Whether ESPN accepts a cookie pair, and the account's fan payload.

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
        return "refused", {}
    if fan.get("anon") is True:
        return "refused", fan

    for season, league_id in _probe_leagues(fan):
        try:
            settings = await EspnService.fetch_league_settings(espn_s2, swid, season, league_id)
        except ProviderAuthError:
            return "refused", fan
        except BadRequestError as exc:
            if exc.error_code != LEAGUE_NOT_FOUND_CODE:
                raise
            continue
        if settings.get("isPublic") is False:
            return "accepted", fan
    return "unconfirmed", fan


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
    """Mark each ESPN team with the Court Vision team already tracking it
    (matched by `_team_keys`, ESPN's team id before the name)."""
    index: dict[tuple, int] = {}
    for team_id, info in tracked:
        for key in _team_keys(info.get("league_id"), info.get("espn_team_id"), info.get("team_name")):
            index.setdefault(key, team_id)
    return [
        team.model_copy(update={"tracked_team_id": next(
            (index[key] for key in _team_keys(team.league_id, team.espn_team_id, team.team_name) if key in index),
            None,
        )})
        for team in teams
    ]


def _adopt_account_teams(user_id: int, connection_id: int, account_teams: list[EspnAccountTeam]) -> list[int]:
    """Link the user's unlinked ESPN teams that are on this account to its connection.

    A team saved without cookies (a public league), or whose cookies were never
    moved into a connection (an environment the 0005 backfill did not reach),
    would otherwise be left out: the account card would not count it and a
    cookie refresh would not reach it. ESPN's own list says which teams are the
    account's; a team linked to another account is left where it is. Any copy
    of the cookies an adopted team still holds is dropped -- the connection's
    pair has just been accepted. Returns the adopted team ids.
    """
    from db.models.teams import Team

    account_keys = {
        key for team in account_teams for key in _team_keys(team.league_id, team.espn_team_id, team.team_name)
    }
    adopted: list[int] = []
    unlinked = (
        Team.select()
        .where((Team.user_id == user_id) & Team.provider_connection.is_null())
        .order_by(Team.team_id)
    )
    for team in unlinked:
        info = json.loads(team.league_info)
        if info.get("provider", "espn") != "espn":
            continue
        if not account_keys.intersection(_team_keys(info.get("league_id"), info.get("espn_team_id"), info.get("team_name"))):
            continue
        public, _ = credential_service.split_secrets(info)
        team.league_info = json.dumps(public)
        team.provider_connection = connection_id
        team.save()
        adopted.append(team.team_id)
    return adopted


async def _adopt(user_id: int, connection_id: int, fan: dict) -> list[int]:
    adopted = await run_db(
        "connections.adopt", _adopt_account_teams, user_id, connection_id, espn_fan_teams(fan)
    )
    if adopted:
        log.info("espn_connection_adopted_teams", connection_id=connection_id, team_ids=adopted)
    return adopted


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
        An accepted pair also takes over the user's unlinked teams on the account.
        """
        _require_store()
        espn_s2 = (espn_s2 or "").strip()
        swid = credential_service.normalize_swid(swid)
        if not espn_s2 or not swid:
            raise BadRequestError("ESPN_COOKIES_REQUIRED", "Paste both espn_s2 and SWID")

        verdict, fan = await _check_espn_cookies(espn_s2, swid)
        if verdict == "refused":
            # Not the league-read wording ("check ... the season"): no league was named
            raise ProviderAuthError(
                "espn", "ESPN rejected these cookies — copy espn_s2 and SWID again while logged in to ESPN"
            )

        connection_id, created = await run_db(
            "connections.store_espn", credential_service.store_espn_cookies,
            user_id, espn_s2, swid, verified=verdict == "accepted",
        )
        adopted = await _adopt(user_id, connection_id, fan) if verdict == "accepted" else []
        (view,) = await run_db("connections.view", _views, user_id, connection_id)
        log.info("espn_connection_saved", connection_id=connection_id, created=created,
                 verdict=verdict, adopted=len(adopted), teams=len(view.teams))
        message = "ESPN account connected" if created else "ESPN cookies updated"
        if adopted:
            message = f"{message} — {_adopted_note(len(adopted))}"
        if verdict == "unconfirmed":
            message = f"{message} — {UNCONFIRMED}"
        return ProviderConnectionResp(status=ApiStatus.SUCCESS, message=message, data=view, created=created)

    @staticmethod
    async def verify(user_id: int, connection_id: int) -> ProviderConnectionResp:
        """Ask ESPN whether a stored connection's cookies still work, and record the answer.

        A refusal is the answer, not a failure of the request: it comes back as
        a success whose connection reads "expired". An outage raises and records
        nothing, as does an account with no private league to check against --
        neither says anything about the cookies. Accepted cookies take over the
        user's unlinked teams on the account, as on connect.
        """
        _require_store()
        secrets = await run_db(
            "connections.load", credential_service.load_provider_tokens,
            user_id, connection_id, provider="espn",
        )
        if secrets is None:
            raise NotFoundError(CONNECTION_NOT_FOUND, "No ESPN connection with that id")

        try:
            verdict, fan = await _check_espn_cookies(secrets.get("espn_s2", ""), secrets.get("swid", ""))
        except BadRequestError as exc:
            if exc.error_code != ESPN_ACCOUNT_NOT_FOUND:
                raise
            verdict, fan = "refused", {}

        if verdict != "unconfirmed":
            await run_db("connections.mark_checked", credential_service.mark_checked,
                         connection_id, verdict == "accepted")
        adopted = await _adopt(user_id, connection_id, fan) if verdict == "accepted" else []
        (view,) = await run_db("connections.view", _views, user_id, connection_id)
        log.info("espn_connection_checked", connection_id=connection_id, verdict=verdict, adopted=len(adopted))
        message = {
            "accepted": "ESPN accepted these cookies",
            "refused": "ESPN rejected these cookies",
            "unconfirmed": UNCONFIRMED[0].upper() + UNCONFIRMED[1:],
        }[verdict]
        if adopted:
            message = f"{message} — {_adopted_note(len(adopted))}"
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
