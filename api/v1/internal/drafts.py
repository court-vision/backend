"""
Draft Lab routes: draft sessions, their picks, and the board scored for either.

A session (`usr.draft_sessions` + `usr.draft_picks`) is the room's state; the
board is derived from it on every read and never stored. Two board routes exist
on purpose:

- `GET /drafts/board?team_id=` — stateless: the caller passes pick state as
  query params. Useful as a pre-draft big board, and what the room was built
  against before sessions existed.
- `GET /drafts/{session_id}/board` — the room's board: picks come from the
  session, so undo and corrections are a single source of truth.

Route order matters: `/board` is declared before `/{session_id}` so the literal
path is not parsed as a session id.
"""

from fastapi import APIRouter, Depends, Query

from api.deps import (
    OwnedDraftSessionContext,
    OwnedTeamContext,
    UserContext,
    get_db_user,
    get_owned_session,
    get_owned_team,
)
from core.responses import respond
from schemas.draft import (
    DraftBoardResp,
    DraftInitSyncRequest,
    DraftInitSyncResponse,
    DraftPickCreate,
    DraftPickDeleteResponse,
    DraftPickResponse,
    DraftSessionCreate,
    DraftSessionDeleteResponse,
    DraftSessionListResponse,
    DraftSessionResponse,
    DraftSessionUpdate,
)
from services.draft_board_service import BoardSession, DraftBoardService
from services.draft_service import DraftService
from services.draft_sync_service import DraftSyncService
from services.scoring.resolver import resolve_scoring

router = APIRouter(prefix="/drafts", tags=["Drafts"])


@router.get(
    "/board",
    response_model=DraftBoardResp,
    summary="Get the draft board under a team's league scoring",
    description=(
        "Every draftable player, valued by the scoring settings of the league this team belongs "
        "to (point weights, or the fpts-scale category value for a category league), from ESPN's "
        "published projections where available and last season's per-game baseline otherwise. "
        "Rows carry the latest ESPN market rank/ADP and a `market_rank − cv_rank` delta, and are "
        "flagged `cap_blocked` when the league's hard position caps leave no room for them on the "
        "caller's roster.\n\n"
        "`cv_rank` is computed over the full pool before `picked`/`mine` are removed, so it stays "
        "stable and market-comparable throughout a draft.\n\n"
        "Category leagues also carry `fit_value`/`fit_rank`: the same board re-scored for the "
        "caller's own roster, with any `punt` categories weighing zero.\n\n"
        "Stateless: pick state rides in the query params, so there is no slot to count from and "
        "rows carry no availability. Use the session board once a draft room is open."
    ),
    responses={
        200: {"description": "Board retrieved successfully (empty data with a message before any season data exists)"},
        404: {"description": "No such team, or it does not belong to the caller"},
        422: {"description": "Invalid picked/mine player ids"},
    },
)
async def get_draft_board(
    picked: list[int] = Query(
        default=[],
        description="NBA player ids already drafted by anyone; removed from the board",
    ),
    mine: list[int] = Query(
        default=[],
        description=(
            "NBA player ids drafted by the caller (no need to repeat them in `picked`); "
            "also removed, and counted against the league's position caps"
        ),
    ),
    punt: list[str] = Query(
        default=[],
        description=(
            "Category keys to concede, e.g. `punt=ft_pct&punt=tov`. They weigh zero in "
            "`fit_value`; unknown keys are ignored here (the room stores validated punts on "
            "the session instead). No effect on a points league."
        ),
    ),
    team: OwnedTeamContext = Depends(get_owned_team),
):
    # `get_owned_team` already loaded the league, so resolving its scoring is
    # pure — no second trip to the database (the rankings pattern).
    scoring = resolve_scoring(team.league)
    # No session, so no picks and no slot: BoardSession carries only the view
    # knobs the query string sets.
    return respond(await DraftBoardService.get_board(
        scoring, picked_ids=picked, my_ids=mine, session=BoardSession(punts=tuple(punt))
    ))


@router.post(
    "",
    response_model=DraftSessionResponse,
    summary="Start a draft session",
    description=(
        "Creates a draft room. With a `team_id`, everything the provider already told us is "
        "prefilled from that team's synced league: draft type, pick order, rounds (the league's "
        "draftable roster size) and the keeper allowance. Omit `team_id` for generic settings.\n\n"
        "`kind` says where the picks come from. `live` follows the team's own ESPN draft and is "
        "linked to it at once — one active live room per league, so a second is refused with the "
        "first's id. `mock` follows an ESPN mock lobby and links to the first one it reconciles "
        "with. `manual` follows nothing.\n\n"
        "`my_slot` is the one thing the caller must supply: `pick_order` holds ESPN team ids that "
        "nothing maps back to our teams, so the room asks which seat is yours."
    ),
    responses={
        200: {"description": "Session created"},
        400: {"description": "Slot outside the draft, or a live room without a synced ESPN league"},
        404: {"description": "No such team, or it does not belong to the caller"},
        409: {"description": "An active room already follows that ESPN draft (`existing_session_id`)"},
        422: {"description": "Invalid session fields"},
    },
)
async def create_draft_session(req: DraftSessionCreate, user: UserContext = Depends(get_db_user)):
    return respond(await DraftService.create_session(user.user_id, req))


@router.get(
    "",
    response_model=DraftSessionListResponse,
    summary="List the caller's draft sessions",
    description="Newest first, each with its pick count and whose turn is next. Picks themselves are on the detail route.",
    responses={200: {"description": "Sessions retrieved (empty list before the first draft)"}},
)
async def list_draft_sessions(user: UserContext = Depends(get_db_user)):
    return respond(await DraftService.list_sessions(user.user_id))


@router.get(
    "/{session_id}",
    response_model=DraftSessionResponse,
    summary="Get one draft session with its picks",
    responses={
        200: {"description": "Session retrieved"},
        404: {"description": "No such session, or it does not belong to the caller"},
    },
)
async def get_draft_session(session: OwnedDraftSessionContext = Depends(get_owned_session)):
    return respond(await DraftService.get_session(session.session_id))


@router.patch(
    "/{session_id}",
    response_model=DraftSessionResponse,
    summary="Update a draft session",
    description=(
        "Partial update — only the fields present in the body are written. Used for confirming or "
        "correcting `my_slot`, editing pre-designated `keepers`, setting the `punts` this room is "
        "drafting around, and closing the room (`status: completed`, which stamps `completed_at` "
        "the first time).\n\n"
        "`punts` must be categories this league actually scores; a points-scored room has none, "
        "and an empty list clears the build.\n\n"
        "Changing `draft_type` or `pick_order` re-derives the round and slot of every recorded "
        "pick. A change that would leave the session inconsistent — a slot outside the new pick "
        "order, or a draft resized shorter than the picks already recorded — is refused."
        "\n\n`name` renames the room. `espn_league_id` links it to one ESPN draft (a real league's "
        "id, or a mock lobby's): exclusive — one active room per draft, refused with the other "
        "room's id otherwise — and an explicit null unlinks."
    ),
    responses={
        200: {"description": "Session updated"},
        400: {
            "description": (
                "Slot outside the draft, a draft resized shorter than its recorded picks, or "
                "punts this league cannot score"
            )
        },
        404: {"description": "No such session, or it does not belong to the caller"},
        409: {"description": "Another active room already follows that ESPN draft (`existing_session_id`)"},
        422: {"description": "Empty or invalid update"},
    },
)
async def update_draft_session(
    req: DraftSessionUpdate, session: OwnedDraftSessionContext = Depends(get_owned_session)
):
    return respond(await DraftService.update_session(session.session_id, req))


@router.delete(
    "/{session_id}",
    response_model=DraftSessionDeleteResponse,
    summary="Delete a draft session",
    description=(
        "Removes the room and every pick recorded in it. There is no undo: a finished draft is "
        "better closed (`status: completed`) than deleted."
    ),
    responses={
        200: {"description": "Session deleted"},
        404: {"description": "No such session, or it does not belong to the caller"},
    },
)
async def delete_draft_session(session: OwnedDraftSessionContext = Depends(get_owned_session)):
    return respond(await DraftService.delete_session(session.session_id))


@router.get(
    "/{session_id}/board",
    response_model=DraftBoardResp,
    summary="Get the draft board for a session",
    description=(
        "The room's board: the same valuation as `/drafts/board`, with pick state taken from the "
        "session rather than the query string, and with recommendations for the caller's next "
        "pick — every component of the score visible (`season_value`, `vorp`, `scarcity`, "
        "`flexibility`, `injury`, `category_fit`).\n\n"
        "Players the league's hard position caps have made undraftable for the caller are flagged "
        "`cap_blocked` (shown greyed, never hidden) and are excluded from the recommendations.\n\n"
        "`roster` lists the caller's drafted players with primary position, eligible slots and NBA "
        "team — what the roster zone needs to fill lineup slots, count caps and flag stacking.\n\n"
        "Category leagues additionally carry `fit_value`/`fit_rank` — the board re-scored for this "
        "roster, with the session's `punts` at zero weight — a `category_fit` component on every "
        "recommendation, and `meta.category_need`, which says how far the roster trails an average "
        "team in each category.\n\n"
        "Rows with market data carry `availability` (`likely`/`tossup`/`gone`) for the caller's "
        "next pick — the pick after that while the caller is on the clock."
    ),
    responses={
        200: {"description": "Board retrieved successfully (empty data with a message before any season data exists)"},
        404: {"description": "No such session, or it does not belong to the caller"},
    },
)
async def get_draft_session_board(session: OwnedDraftSessionContext = Depends(get_owned_session)):
    # `get_owned_session` already loaded the league, so scoring resolution is pure.
    scoring = resolve_scoring(session.league)
    return respond(await DraftBoardService.get_board(scoring, session=BoardSession.of(session)))


@router.post(
    "/{session_id}/picks",
    response_model=DraftPickResponse,
    summary="Record a pick",
    description=(
        "Appends a pick. `overall_pick` defaults to the session's lowest unused number, so the "
        "normal path is to post the player alone; passing it explicitly is how a correction lands "
        "in a hole an undo left. The player is resolved NBA id → ESPN id → normalized name, and a "
        "pick whose player is not in `nba.players` yet is still recorded with the provider identity. "
        "A player already in the session cannot be recorded a second time — undo the earlier pick "
        "to correct it.\n\n"
        "`source: keeper` records a keeper at the pick its round costs (the session's `keepers` "
        "carry that number as `overall_pick`). A keeper pick leaves the board like any other but "
        "never counts as the draft front: whose-turn arithmetic steps over it, so it is checked "
        "against the session's designated keepers rather than taken on trust. `source: mock` is "
        "rejected here — it claims the autopicker made the pick, which only the server may say."
    ),
    responses={
        200: {"description": "Pick recorded"},
        400: {"description": "Pick past the end of the draft"},
        404: {"description": "No such session/player, or the session does not belong to the caller"},
        409: {"description": "That pick number is already recorded, or that player already is"},
        422: {"description": "A pick that identifies no player"},
    },
)
async def add_draft_pick(
    req: DraftPickCreate, session: OwnedDraftSessionContext = Depends(get_owned_session)
):
    return respond(await DraftService.add_pick(session.session_id, req))


@router.delete(
    "/{session_id}/picks/{overall_pick}",
    response_model=DraftPickDeleteResponse,
    summary="Undo a pick",
    description="Removes one pick by its overall number. The number becomes the next default, so a mis-entered pick is re-recorded in place.",
    responses={
        200: {"description": "Pick undone"},
        404: {"description": "No such session or pick, or the session does not belong to the caller"},
    },
)
async def remove_draft_pick(
    overall_pick: int, session: OwnedDraftSessionContext = Depends(get_owned_session)
):
    return respond(await DraftService.remove_pick(session.session_id, overall_pick))


@router.post(
    "/{session_id}/sync/init",
    response_model=DraftInitSyncResponse,
    summary="Reconcile a session with the ESPN draft room's INIT frame",
    description=(
        "Decodes the ESPN draft room's INIT snapshot and folds it into this session. On a session "
        "with no picks yet it sets the pick order, slot, rounds and draft type from the room; then "
        "it records every pick the room has made, at that pick's ESPN number, skipping the ones "
        "already recorded and reporting (never overwriting) any that disagree.\n\n"
        "Idempotent by design: the room's tab posts this on every connect, so a reconnect re-posts "
        "the whole snapshot and only the newly-made picks are inserted."
    ),
    responses={
        200: {"description": "Reconciled — see `inserted`, `skipped`, `conflicts`, `warnings`"},
        400: {"description": "The payload is not a decodable INIT frame"},
        404: {"description": "No such session, or it does not belong to the caller"},
        409: {"description": "The ESPN room belongs to a different league than this session"},
    },
)
async def sync_draft_init(
    req: DraftInitSyncRequest, session: OwnedDraftSessionContext = Depends(get_owned_session)
):
    return respond(await DraftSyncService.sync_init(session.session_id, req))
