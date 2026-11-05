"""
Dime in the Draft desk (the Dime autopilot plan, step 7; services/ai/draft.py).

`/drafts/{session_id}/dime` is Dime's side of one draft room: its notes (a plan, a
note at the user's pick, a recap), the switch for a note at every pick, and the
user's seat in a mock, which Dime can draft to the end. Every note is an agent turn
started here by the user, on the AI desk's spend line. Off unless AI_ENABLED and
DIME_DRAFT_ENABLED are on; the GET never refuses, so the room can hide Dime.
"""

from fastapi import APIRouter, Depends

from api.deps import OwnedDraftSessionContext, get_owned_session
from schemas.common import ApiStatus
from schemas.dime_draft import (
    DimeDraftNote,
    DimeDraftNoteReq,
    DimeDraftNoteResp,
    DimeDraftRoom,
    DimeDraftRoomReq,
    DimeDraftRoomResp,
    DimeDraftSeatReq,
)
from services.ai import draft as dime

router = APIRouter(prefix="/drafts", tags=["Drafts"])

_ERRORS = {
    403: {"description": "The caller is not on the AI desk's invite list (AI_INVITE_ONLY)"},
    404: {"description": "No such room, or it is not the caller's (DRAFT_SESSION_NOT_FOUND)"},
    503: {"description": "Turned off (AI_DISABLED, AI_DRAFT_DISABLED)"},
}


@router.get(
    "/{session_id}/dime",
    response_model=DimeDraftRoomResp,
    summary="Dime in a draft room: its notes, the notes switch and the mock seat",
    description=(
        "What Dime has written in the room, newest first (a plan, a note at each of the user's picks, a "
        "recap; each with the board's numbers the server read for it in `facts`), whether a note is being "
        "written now, the switch for a note whenever the user's pick is `lead` picks away or nearer, and "
        "whether Dime holds the user's seat in a mock. Never refuses for a switched-off server or a user "
        "off the AI desk's list: `available` says whether the room shows Dime at all."
    ),
    responses={404: _ERRORS[404]},
)
async def get_dime(session: OwnedDraftSessionContext = Depends(get_owned_session)) -> DimeDraftRoomResp:
    data = await dime.room_view(session.user_id, session)
    return DimeDraftRoomResp(status=ApiStatus.SUCCESS, message="Dime in this room", data=DimeDraftRoom(**data))


@router.put(
    "/{session_id}/dime",
    response_model=DimeDraftRoomResp,
    summary="Switch Dime's note at each of the user's picks on or off",
    description=(
        "With `notes` on, the room asks Dime for a note whenever the user's pick is `lead` picks away or "
        "nearer, one per pick. Each note is a turn on the AI desk's budget."
    ),
    responses=_ERRORS,
)
async def set_dime(
    req: DimeDraftRoomReq, session: OwnedDraftSessionContext = Depends(get_owned_session)
) -> DimeDraftRoomResp:
    data = await dime.set_notes(session.user_id, session, req.notes)
    message = "Notes at your picks on" if req.notes else "Notes at your picks off"
    return DimeDraftRoomResp(status=ApiStatus.SUCCESS, message=message, data=DimeDraftRoom(**data))


@router.post(
    "/{session_id}/dime/notes",
    response_model=DimeDraftNoteResp,
    summary="Ask Dime for a note in the room: a plan, a note at the user's pick, or a recap",
    description=(
        "Starts the note and returns it `running`; it is written after the response (poll the GET, or open "
        "its conversation). `plan` sets the plan the draft is played to: its concessions become the room's "
        "punts, and in a real draft on a team the plan is saved to the team's notes. `pick` is about the "
        "user's coming pick, read off the room's board; one per pick, so asking again returns the one "
        "already written. `recap` reads the room's recap and, in a real draft, saves what the season starts "
        "from and league-mates' habits to Dime's notes."
    ),
    responses={
        **_ERRORS,
        409: {"description": "A note is already being written (AI_DRAFT_BUSY), the draft is finished "
                             "(AI_DRAFT_FINISHED), no pick of the user's is left (AI_DRAFT_NO_PICK), or nothing "
                             "has been drafted yet (AI_DRAFT_NO_PICKS)"},
        429: {"description": "The AI desk's monthly budget is used up"},
    },
)
async def ask_dime(
    req: DimeDraftNoteReq, session: OwnedDraftSessionContext = Depends(get_owned_session)
) -> DimeDraftNoteResp:
    started = await dime.start(session.user_id, session, req.kind)
    note = await dime._note(started.note_id)
    message = "Note started" if started.task is not None else "Already written for this pick"
    return DimeDraftNoteResp(status=ApiStatus.SUCCESS, message=message, data=DimeDraftNote(**dime.note_view(note)))


@router.post(
    "/{session_id}/dime/seat",
    response_model=DimeDraftRoomResp,
    summary="Hand Dime the user's seat in a mock, or take it back",
    description=(
        "`on`: Dime drafts the user's seat to the end of the mock: the autopicker runs the other seats to "
        "the user's turn, Dime makes the pick (recorded `dime`, with its reason as a note), and so on. "
        "Mock rooms following nothing only; a live ESPN draft stays the user's to pick in. `off`: Dime "
        "hands the seat back after the pick it is making. Each pick is a turn on the AI desk's budget."
    ),
    responses={
        **_ERRORS,
        409: {"description": "Not a room Dime can draft in: not a mock, linked to an ESPN lobby, finished, "
                             "no seat or shape set (AI_DRAFT_NO_SEAT)"},
    },
)
async def set_seat(
    req: DimeDraftSeatReq, session: OwnedDraftSessionContext = Depends(get_owned_session)
) -> DimeDraftRoomResp:
    data = await dime.set_seat(session.user_id, session, req.on)
    message = "Dime has your seat" if req.on else "Your seat is yours again"
    return DimeDraftRoomResp(status=ApiStatus.SUCCESS, message=message, data=DimeDraftRoom(**data))
