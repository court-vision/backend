"""Import a finished ESPN draft into a session.

The spike settled what this can and cannot be: ESPN's read API writes the picks
into `mDraftDetail` only when the draft completes, atomically with the `drafted`
flag, so mid-draft every slot reads `playerId: -1`. Live sync is the tap's job
(`draft_sync_service`); this is the other end — one request after the fact that
turns a draft somebody ran without the room into a recap.

It is the same fold as the INIT sync, over a different source: `apply_header`
writes the room's shape onto an empty session and `fold_picks` records what was
picked, so a re-import skips what is already held and reports what disagrees
rather than overwriting it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterator

from peewee import IntegrityError

from core.errors import BadRequestError, ConflictError
from db.base import db, db_operation
from db.models.drafts import DraftPick, DraftSession
from schemas.common import ApiStatus, FantasyProvider, LeagueInfo
from schemas.draft import DraftImportResp, DraftImportResponse
from services.draft_service import DraftService, _session_resp, lock_room
from services.draft_sync_service import (
    ESPN_AFTER_DRAFT,
    DraftSyncService,
    IncomingPick,
    RoomHeader,
    apply_header,
    fold_picks,
)
from services.espn_service import EspnService
from services.providers.http import provider_get
from utils.constants import ESPN_FANTASY_ENDPOINT


def made_picks(detail: dict) -> Iterator[IncomingPick]:
    """The completed draft's picks, ascending.

    `playerId: -1` is ESPN's empty slot — present in the skeleton before the
    draft and, if one ever survives into a completed payload, not a pick.
    """
    picks = [p for p in (detail.get("picks") or []) if int(p.get("playerId") or -1) > 0]
    for pick in sorted(picks, key=lambda p: int(p["overallPickNumber"])):
        yield IncomingPick(
            pick_number=int(pick["overallPickNumber"]),
            espn_player_id=int(pick["playerId"]),
            espn_team_id=int(pick["teamId"]),
            is_keeper=bool(pick.get("keeper")),
            bid_amount=pick.get("bidAmount"),
        )


def header_from_detail(payload: dict, espn_team_id: int) -> RoomHeader:
    """The session-shaping facts a completed draft carries.

    `draftSettings.pickOrder` is the league's own first-round order, which is
    the same list INIT's pick skeleton yields; where a payload lacks it, the
    round-1 picks say it just as well. The draft is over by the time this is
    called, so the state is ESPN's own "after".
    """
    detail = payload.get("draftDetail") or {}
    settings = (payload.get("settings") or {}).get("draftSettings") or {}
    picks = [p for p in (detail.get("picks") or []) if int(p.get("playerId") or -1) > 0]

    order = [int(team) for team in (settings.get("pickOrder") or [])]
    if not order:
        first_round = sorted(
            (p for p in picks if int(p.get("roundId") or 0) == 1),
            key=lambda p: int(p.get("roundPickNumber") or 0),
        )
        order = [int(p["teamId"]) for p in first_round]

    numbers = [int(p["overallPickNumber"]) for p in picks]
    rounds = max((int(p.get("roundId") or 0) for p in picks), default=0)
    return RoomHeader(
        espn_league_id=int(payload["id"]),
        espn_team_id=espn_team_id,
        draft_state=ESPN_AFTER_DRAFT,
        draft_type="auction" if str(settings.get("type") or "").upper() == "AUCTION" else "snake",
        pick_order=order,
        my_slot=(order.index(espn_team_id) + 1) if espn_team_id in order else None,
        rounds=rounds or None,
        espn_front=(max(numbers) + 1) if numbers else 1,
    )


def my_team_id(payload: dict, team_name: str) -> int:
    """The caller's ESPN team id, by the rule `get_roster` already uses.

    A pick's `memberId` is the SWID of whoever clicked, but ESPN only sets it on
    picks a human made — two thirds of a real draft are autopicks — so the team
    name is the identity that is always there.
    """
    wanted = (team_name or "").strip()
    teams = payload.get("teams") or []
    for team in teams:
        if wanted == (team.get("name") or "").strip():
            return int(team["id"])
    names = ", ".join(str(team.get("name")) for team in teams)
    raise BadRequestError(
        "TEAM_NAME_NOT_IN_LEAGUE",
        f"Team '{wanted}' is not in this league; teams: {names}" if names
        else f"Team '{wanted}' is not in this league",
    )


class DraftImportService:

    @staticmethod
    async def import_draft(session_id: int, league_info: LeagueInfo) -> DraftImportResponse:
        """Fetch the completed draft and fold it into the session."""
        if league_info.provider != FantasyProvider.ESPN:
            # Yahoo's draft results are a different endpoint and a different id
            # space (`nba.players` has no `yahoo_id`), so it is its own piece of
            # work rather than a branch in this one.
            raise BadRequestError(
                "IMPORT_PROVIDER_UNSUPPORTED",
                f"Importing a {league_info.provider.value} draft is not supported yet",
            )
        payload = await DraftImportService._fetch(league_info)
        return await DraftImportService._record(session_id, payload, league_info)

    @staticmethod
    async def _fetch(league_info: LeagueInfo) -> dict:
        endpoint = ESPN_FANTASY_ENDPOINT.format(int(league_info.year), int(league_info.league_id))
        # `mTeam` rides along for one reason: it names the teams, which is how a
        # pick becomes `by_me`.
        return await provider_get(
            "espn", endpoint,
            params={"view": ["mDraftDetail", "mTeam"]},
            cookies=EspnService._cookies(league_info),
            expect_key="draftDetail",
        )

    @staticmethod
    @db_operation("drafts.import")
    def _record(session_id: int, payload: dict, league_info: LeagueInfo) -> DraftImportResponse:
        detail = payload.get("draftDetail") or {}
        if not detail.get("drafted"):
            # Nothing to import yet, and polling for it is pointless: the read
            # API is blind until the flag flips.
            raise ConflictError(
                "DRAFT_NOT_COMPLETE",
                "That ESPN draft has not finished — its picks are not readable until it does",
                data={"in_progress": bool(detail.get("inProgress"))},
            )

        espn_team_id = my_team_id(payload, league_info.team_name)
        header = header_from_detail(payload, espn_team_id)

        # Which ESPN draft this room follows, decided before anything takes a
        # lock and again from the locked row — the sync's rule, for the same
        # reason: a concurrent request can link the room in between.
        session = DraftService._session_or_404(session_id)
        DraftSyncService._link_decision(session, header)

        with db.atomic():
            lock_room(session_id)
            session = DraftService._session_or_404(session_id)
            link = DraftSyncService._link_decision(session, header)

            if DraftPick.select().where(
                (DraftPick.session == session_id) & (DraftPick.source == "mock")
            ).exists():
                # Simulated picks already spent these numbers, so every imported
                # pick would come back a conflict. A room plays a mock or
                # records a real draft, never both.
                raise ConflictError(
                    "DRAFT_ROOM_IS_SIMULATED",
                    "This room holds simulated picks; open a fresh room to import a draft",
                )

            existing = DraftService._picks_of(session_id)
            header_applied, warnings = apply_header(session, header, existing)

            folded = fold_picks(
                session,
                made_picks(detail),
                existing,
                my_espn_team_id=espn_team_id,
                default_source="import",
                league_size=len(session.pick_order or []) or None,
            )
            warnings = warnings + folded.warnings

            model_fields = []
            if folded.keeper_additions:
                session.keepers = (session.keepers or []) + folded.keeper_additions
                model_fields.append(DraftSession.keepers)
            if link is not None:
                session.espn_league_id = link
                model_fields.append(DraftSession.espn_league_id)
            if folded.inserted and session.started_at is None:
                session.started_at = datetime.utcnow()
                model_fields.append(DraftSession.started_at)
            # ESPN says the draft is over, so the room is too — however many of
            # its picks we were able to record.
            if session.status == "active":
                model_fields += DraftService._complete(session)
            if model_fields:
                session.updated_at = datetime.utcnow()
                try:
                    session.save(only=model_fields + [DraftSession.updated_at])
                except IntegrityError as exc:  # the one-room-per-draft index
                    raise DraftService._link_conflict(exc) from exc

        picks = DraftService._picks_of(session_id)
        made = len(list(made_picks(detail)))
        return DraftImportResponse(
            status=ApiStatus.SUCCESS,
            message=f"Draft imported: {folded.inserted} recorded, {folded.skipped} already held",
            data=DraftImportResp(
                session=_session_resp(
                    session,
                    used_picks=[p.overall_pick for p in picks],
                    picks=picks,
                    keeper_count=DraftService._keeper_count_of(session),
                    keeper_picks=[p.overall_pick for p in picks if p.source == "keeper"],
                ),
                espn_league_id=header.espn_league_id,
                espn_team_id=espn_team_id,
                draft_type=session.draft_type,
                made=made,
                inserted=folded.inserted,
                skipped=folded.skipped,
                conflicts=folded.conflicts,
                warnings=warnings,
                header_applied=header_applied,
            ),
        )
