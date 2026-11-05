"""The AI layer's routes (docs/AI_LAYER_PLAN.md, docs/AI_AGENT_PLAN.md). Off unless AI_ENABLED is set."""

import asyncio
import json
from collections.abc import AsyncIterator

from typing import Optional

from fastapi import APIRouter, Body, Depends, Path, Query
from fastapi.responses import StreamingResponse

from api.deps import UserContext, get_db_user
from core.errors import NotFoundError
from schemas.ai import (
    AiAutopilotReq,
    AiAutopilotResp,
    AiCheckinResp,
    AiCheckinRunReq,
    AiCheckinsResp,
    AiConversationCreateReq,
    AiConversationListResp,
    AiConversationResp,
    AiDecisionVerdictReq,
    AiDecisionVerdictResp,
    AiFeedbackData,
    AiFeedbackReq,
    AiFeedbackResp,
    AiNoteCreateReq,
    AiNoteEditReq,
    AiNoteListResp,
    AiNoteResp,
    AiProposalDismissResp,
    AiProposalResp,
    AiTurnCommentData,
    AiTurnCommentReq,
    AiTurnCommentResp,
    AiTurnFeedbackData,
    AiTurnFeedbackResp,
    AiTurnReq,
    AiUsageResp,
    AskReq,
    AskResp,
    RouteReq,
    RouteResp,
)
from schemas.common import ApiStatus, BaseResponse
from services.ai import questions
from services.ai.agent import Emitter
from services.ai.agent_service import AgentService
from services.ai.service import AiService

router = APIRouter(prefix="/ai", tags=["AI"])


@router.post(
    "/ask",
    response_model=AskResp,
    summary="Ask the assistant a question about NBA players",
    description=(
        "Answers by looking players up through Court Vision's own services; every number "
        "in the answer comes from one of those lookups, listed in `tool_calls`. Each caller "
        "has a daily allowance of questions, and the route is off unless AI_ENABLED is set."
    ),
    responses={
        422: {"description": "Invalid question, or the assistant declined it (AI_DECLINED)"},
        429: {"description": "The caller's daily allowance is used up (AI_QUOTA_EXCEEDED)"},
        502: {"description": "The model provider failed (AI_UNAVAILABLE, AI_BUSY, AI_INCOMPLETE)"},
        503: {"description": "Turned off (AI_DISABLED) or today's global budget is spent (AI_DAILY_BUDGET_REACHED)"},
        504: {"description": "The assistant took too long (AI_TIMEOUT)"},
    },
)
async def ask(req: AskReq, user: UserContext = Depends(get_db_user)) -> AskResp:
    return await AiService.ask(req.question, user_id=user.user_id)


@router.post(
    "/route",
    response_model=RouteResp,
    summary="Route a question to the place that answers it",
    description=(
        "Returns one of three kinds. `show`: a Court Vision view, as a terminal or page `target` "
        "the client applies. `statmuse`: an NBA stat question no view covers, with a server-built "
        "`statmuse_url` the user opens. `cannot`: neither covers it, with `suggestions`. Every ID "
        "in a target has been checked, and a fantasy team is always one of the caller's. Shares "
        "the daily allowance with /ai/ask and is off unless AI_ENABLED is set."
    ),
    responses={
        422: {"description": "Invalid question or context, or the assistant declined it (AI_DECLINED)"},
        429: {"description": "The caller's daily allowance is used up (AI_QUOTA_EXCEEDED)"},
        502: {"description": "The model provider failed (AI_UNAVAILABLE, AI_BUSY, AI_INCOMPLETE)"},
        503: {"description": "Turned off (AI_DISABLED) or today's global budget is spent (AI_DAILY_BUDGET_REACHED)"},
        504: {"description": "The assistant took too long (AI_TIMEOUT)"},
    },
)
async def route(req: RouteReq, user: UserContext = Depends(get_db_user)) -> RouteResp:
    return await AiService.route(req.question, req.context, user_id=user.user_id)


@router.post(
    "/questions/{question_id}/feedback",
    response_model=AiFeedbackResp,
    summary="Rate a routed answer",
    description="Thumbs up or down on one of the caller's own routed questions; null clears it.",
    responses={404: {"description": "No such question for this caller (AI_QUESTION_NOT_FOUND)"}},
)
async def question_feedback(
    question_id: int = Path(..., gt=0),
    req: AiFeedbackReq = Body(...),
    user: UserContext = Depends(get_db_user),
) -> AiFeedbackResp:
    if not await questions.set_feedback(question_id, user.user_id, req.feedback):
        raise NotFoundError("AI_QUESTION_NOT_FOUND", "Question not found")
    return AiFeedbackResp(
        status=ApiStatus.SUCCESS,
        message="Feedback saved",
        data=AiFeedbackData(question_id=question_id, feedback=req.feedback),
    )


# ---- The agent: the AI desk (docs/AI_AGENT_PLAN.md). Invite-only. ----

_AGENT_ERRORS = {
    403: {"description": "The caller is not on the invite list (AI_INVITE_ONLY)"},
    503: {"description": "Turned off (AI_DISABLED)"},
}
# Silence longer than this gets a comment line, so proxies keep the stream open
# while the model thinks.
KEEPALIVE_SECONDS = 15.0


@router.get(
    "/usage",
    response_model=AiUsageResp,
    summary="The caller's access to the agent and this month's spend",
    description="Open to any signed-in user, so the desk can say whether the agent is available to them.",
    responses={503: _AGENT_ERRORS[503]},
)
async def agent_usage(user: UserContext = Depends(get_db_user)) -> AiUsageResp:
    return AiUsageResp(status=ApiStatus.SUCCESS, message="Usage", data=await AgentService.usage(user.user_id))


@router.post(
    "/conversations",
    response_model=AiConversationResp,
    summary="Start a conversation with the agent",
    responses={**_AGENT_ERRORS, 404: {"description": "team_id is not one of the caller's teams (TEAM_NOT_FOUND)"}},
)
async def create_conversation(
    req: AiConversationCreateReq = Body(default_factory=AiConversationCreateReq),
    user: UserContext = Depends(get_db_user),
) -> AiConversationResp:
    data = await AgentService.create(user.user_id, req.team_id)
    return AiConversationResp(status=ApiStatus.SUCCESS, message="Conversation started", data=data)


@router.get(
    "/conversations",
    response_model=AiConversationListResp,
    summary="The caller's conversations, most recent first",
    responses=_AGENT_ERRORS,
)
async def list_conversations(user: UserContext = Depends(get_db_user)) -> AiConversationListResp:
    return AiConversationListResp(status=ApiStatus.SUCCESS, message="Conversations",
                                  data=await AgentService.list(user.user_id))


@router.get(
    "/conversations/{conversation_id}",
    response_model=AiConversationResp,
    summary="One conversation with every turn",
    responses={**_AGENT_ERRORS, 404: {"description": "No such conversation for this caller"}},
)
async def get_conversation(
    conversation_id: int = Path(..., gt=0),
    user: UserContext = Depends(get_db_user),
) -> AiConversationResp:
    data = await AgentService.detail(conversation_id, user.user_id)
    return AiConversationResp(status=ApiStatus.SUCCESS, message="Conversation", data=data)


@router.delete(
    "/conversations/{conversation_id}",
    response_model=BaseResponse,
    summary="Delete a conversation and its turns",
    responses={**_AGENT_ERRORS, 404: {"description": "No such conversation for this caller"}},
)
async def delete_conversation(
    conversation_id: int = Path(..., gt=0),
    user: UserContext = Depends(get_db_user),
) -> BaseResponse:
    await AgentService.delete(conversation_id, user.user_id)
    return BaseResponse(status=ApiStatus.SUCCESS, message="Conversation deleted")


async def _events(emit: Emitter) -> AsyncIterator[str]:
    """Server-sent events until the turn closes the stream. A client that goes
    away only stops this generator; the turn finishes and is written anyway."""
    while True:
        try:
            item = await asyncio.wait_for(emit.queue.get(), KEEPALIVE_SECONDS)
        except TimeoutError:
            yield ": keepalive\n\n"
            continue
        if item is None:
            return
        event, data = item
        yield f"event: {event}\ndata: {json.dumps(data, default=str, ensure_ascii=False)}\n\n"


@router.post(
    "/conversations/{conversation_id}/turns",
    summary="Send the agent a message; the answer streams back",
    description=(
        "A `text/event-stream` of the turn as it happens:\n\n"
        "- `status` `{text, kind, tool?}`: what the agent is doing (`kind` is `tool` for a lookup, "
        "`note` for the agent's own progress note, `step` for the start of each step after the "
        "first, when whatever the last status said is done);\n"
        "- `text` `{delta}`: the answer's prose, as it is written;\n"
        "- `block` (an `AiTable`, `AiChart`, `AiProposal`, `AiPlace` or, in a check-in, `AiDecisionBlock`): a "
        "table, chart, proposed move, place in the app or logged call the server built from the agent's "
        "lookups;\n"
        "- `error` `{code, message}`: the turn failed (AI_DECLINED, AI_TIMEOUT, AI_INCOMPLETE, AI_BUSY, ...);\n"
        "- `done` `{turn_id, outcome, cost_usd, model_calls, duration_ms, spend}`: always last.\n\n"
        "Text and blocks arrive in the order the user should see them. Lines starting with `:` are "
        "keepalives. Errors before the stream starts are ordinary JSON errors."
    ),
    responses={
        200: {"content": {"text/event-stream": {}}, "description": "The turn's events"},
        **_AGENT_ERRORS,
        404: {"description": "No such conversation for this caller (AI_CONVERSATION_NOT_FOUND)"},
        409: {"description": "Another turn is running (AI_CONVERSATION_BUSY), or the conversation "
                             "is full (AI_CONVERSATION_FULL)"},
        429: {"description": "The caller's monthly budget is used up (AI_USER_BUDGET_REACHED)"},
    },
)
async def send_turn(
    req: AiTurnReq,
    conversation_id: int = Path(..., gt=0),
    user: UserContext = Depends(get_db_user),
) -> StreamingResponse:
    emit = await AgentService.start_turn(conversation_id, user.user_id, req.message, build=req.build)
    return StreamingResponse(
        _events(emit),
        media_type="text/event-stream",
        # No caching, and no proxy buffering (nginx-style proxies honour X-Accel-Buffering)
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/turns/{turn_id}/feedback",
    response_model=AiTurnFeedbackResp,
    summary="Rate one of the agent's answers",
    description="Thumbs up or down on one of the caller's own turns; null clears it.",
    responses={**_AGENT_ERRORS, 404: {"description": "No such turn for this caller (AI_TURN_NOT_FOUND)"}},
)
async def turn_feedback(
    turn_id: int = Path(..., gt=0),
    req: AiFeedbackReq = Body(...),
    user: UserContext = Depends(get_db_user),
) -> AiTurnFeedbackResp:
    await AgentService.feedback(turn_id, user.user_id, req.feedback)
    return AiTurnFeedbackResp(status=ApiStatus.SUCCESS, message="Feedback saved",
                              data=AiTurnFeedbackData(turn_id=turn_id, feedback=req.feedback))


@router.post(
    "/turns/{turn_id}/comment",
    response_model=AiTurnCommentResp,
    summary="Leave a note on one of the agent's answers",
    description="Free text beside the 👍 / 👎, read back when answers are reviewed; null or blank clears it.",
    responses={**_AGENT_ERRORS, 404: {"description": "No such turn for this caller (AI_TURN_NOT_FOUND)"}},
)
async def turn_comment(
    turn_id: int = Path(..., gt=0),
    req: AiTurnCommentReq = Body(...),
    user: UserContext = Depends(get_db_user),
) -> AiTurnCommentResp:
    stored = await AgentService.comment(turn_id, user.user_id, req.comment)
    return AiTurnCommentResp(status=ApiStatus.SUCCESS, message="Note saved",
                             data=AiTurnCommentData(turn_id=turn_id, comment=stored))


@router.get(
    "/proposals/{proposal_id}",
    response_model=AiProposalResp,
    summary="One of the agent's proposed moves, checked again",
    description="The chip as it stands and a fresh check against the board and the player pool: what the "
                "desk reads before it stages the chip into the tray. `problems` says why it can't be staged.",
    responses={**_AGENT_ERRORS, 404: {"description": "No such proposal for this caller (AI_PROPOSAL_NOT_FOUND)"}},
)
async def get_proposal(
    proposal_id: int = Path(..., gt=0),
    user: UserContext = Depends(get_db_user),
) -> AiProposalResp:
    data = await AgentService.proposal(proposal_id, user.user_id)
    return AiProposalResp(status=ApiStatus.SUCCESS, message="Proposal", data=data)


@router.post(
    "/proposals/{proposal_id}/dismiss",
    response_model=AiProposalDismissResp,
    summary="Let one of the agent's proposed moves go",
    description="The chip leaves the tray for good; a move that was already sent can't be dismissed (409 "
                "AI_PROPOSAL_SENT). Sending a chip is the Week desk's roster transaction / scheduled pickup "
                "with `proposal_id` on the request, never a route here.",
    responses={**_AGENT_ERRORS, 404: {"description": "No such proposal for this caller (AI_PROPOSAL_NOT_FOUND)"},
               409: {"description": "Already sent (AI_PROPOSAL_SENT)"}},
)
async def dismiss_proposal(
    proposal_id: int = Path(..., gt=0),
    user: UserContext = Depends(get_db_user),
) -> AiProposalDismissResp:
    chip = await AgentService.dismiss_proposal(proposal_id, user.user_id)
    return AiProposalDismissResp(status=ApiStatus.SUCCESS, message="Proposal dismissed", data=chip)


_NOTE_404 = {404: {"description": "No such live note, or team_id is not one of the caller's teams"}}


@router.get(
    "/notes",
    response_model=AiNoteListResp,
    summary="The agent's notes on the caller's teams (\"What Dime knows\")",
    description=(
        "The notes the agent keeps between conversations, newest first: with `team_id`, that team's and the "
        "ones about the caller; without, every one. `include_retired` adds the revised and forgotten ones."
    ),
    responses={**_AGENT_ERRORS, **_NOTE_404},
)
async def list_notes(
    team_id: Optional[int] = Query(None, gt=0, le=2_147_483_647),
    include_retired: bool = Query(False),
    user: UserContext = Depends(get_db_user),
) -> AiNoteListResp:
    data = await AgentService.notes(user.user_id, team_id, include_retired)
    return AiNoteListResp(status=ApiStatus.SUCCESS, message="Notes", data=data)


@router.post(
    "/notes",
    response_model=AiNoteResp,
    summary="Write a note for the agent",
    description="A note of the caller's own (author `user`): the agent reads it and may not change or retire it.",
    responses={**_AGENT_ERRORS, **_NOTE_404},
)
async def add_note(req: AiNoteCreateReq, user: UserContext = Depends(get_db_user)) -> AiNoteResp:
    data = await AgentService.add_note(user.user_id, kind=req.kind, text=req.text, team_id=req.team_id,
                                       recheck_on=req.recheck_on)
    return AiNoteResp(status=ApiStatus.SUCCESS, message="Note saved", data=data)


@router.patch(
    "/notes/{note_id}",
    response_model=AiNoteResp,
    summary="Change one of the agent's notes",
    description="Any live note, the agent's or the caller's: it is retired and replaced by the caller's version, "
                "which the agent may no longer change.",
    responses={**_AGENT_ERRORS, **_NOTE_404},
)
async def edit_note(
    req: AiNoteEditReq,
    note_id: int = Path(..., gt=0),
    user: UserContext = Depends(get_db_user),
) -> AiNoteResp:
    data = await AgentService.edit_note(user.user_id, note_id, text=req.text, kind=req.kind,
                                        recheck_on=req.recheck_on, clear_recheck=req.clear_recheck)
    return AiNoteResp(status=ApiStatus.SUCCESS, message="Note saved", data=data)


@router.delete(
    "/notes/{note_id}",
    response_model=BaseResponse,
    summary="Retire one of the agent's notes",
    description="The note stops reaching the agent. It is kept as retired by the caller, not erased.",
    responses={**_AGENT_ERRORS, **_NOTE_404},
)
async def delete_note(note_id: int = Path(..., gt=0), user: UserContext = Depends(get_db_user)) -> BaseResponse:
    await AgentService.delete_note(user.user_id, note_id)
    return BaseResponse(status=ApiStatus.SUCCESS, message="Note retired")


# ---- Dime's check-ins, in shadow (the Dime autopilot plan, step 3) ----

_CHECKIN_ERRORS = {
    **_AGENT_ERRORS,
    403: {"description": "Not on the invite list (AI_INVITE_ONLY), or check-ins aren't open for this team "
                         "(AI_AUTOPILOT_NOT_ALLOWED)"},
    404: {"description": "team_id is not one of the caller's teams (TEAM_NOT_FOUND)"},
    503: {"description": "Turned off (AI_DISABLED, AI_AUTOPILOT_DISABLED)"},
}


@router.get(
    "/checkins",
    response_model=AiCheckinsResp,
    summary="Dime's check-ins: the teams it may run them on, and what it ran",
    description=(
        "The caller's teams on the check-ins' allowlist with each one's switch and next scheduled check-in, "
        "then the check-ins themselves, newest first (with `team_id`, that team's only), each with its "
        "decisions' tallies and the user's verdicts. A check-in's conversation opens like any other."
    ),
    responses={**_AGENT_ERRORS, 404: _CHECKIN_ERRORS[404]},
)
async def list_checkins(
    team_id: Optional[int] = Query(None, gt=0, le=2_147_483_647),
    user: UserContext = Depends(get_db_user),
) -> AiCheckinsResp:
    data = await AgentService.checkins(user.user_id, team_id)
    return AiCheckinsResp(status=ApiStatus.SUCCESS, message="Check-ins", data=data)


@router.post(
    "/checkins",
    response_model=AiCheckinResp,
    summary="Run one of Dime's check-ins now",
    description=(
        "Starts a check-in on the team now, off the schedule, and returns it `running`; it runs on after "
        "the response (poll GET /checkins, then open its conversation). Without `kind` the clock picks: "
        "morning (weekly on a matchup's first day), pre_lock in the hours before the first tip, night "
        "(yesterday's follow-up) after it. In shadow: nothing is sent to ESPN."
    ),
    responses={
        **_CHECKIN_ERRORS,
        409: {"description": "A check-in is already running on this team (AI_CHECKIN_RUNNING)"},
        429: {"description": "The team has had today's check-ins (AI_CHECKIN_DAILY_LIMIT)"},
        503: {"description": "Turned off (AI_DISABLED, AI_AUTOPILOT_DISABLED), or the check-ins' monthly budget "
                             "is used up (AI_AUTOPILOT_BUDGET_REACHED)"},
    },
)
async def run_checkin(req: AiCheckinRunReq, user: UserContext = Depends(get_db_user)) -> AiCheckinResp:
    data = await AgentService.run_checkin(user.user_id, req.team_id, req.kind)
    return AiCheckinResp(status=ApiStatus.SUCCESS, message="Check-in started", data=data)


@router.put(
    "/autopilot/{team_id}",
    response_model=AiAutopilotResp,
    summary="Dime's autopilot on a team: the check-ins' switch, and how much Dime may do",
    description=(
        "Any of: `on` starts (or resumes) the team's check-ins and off pauses them (a check-in whose window "
        "closed while they were off is not counted as missed; resuming also lifts a pause Dime made itself); "
        "`mode` sets how much Dime may do on the whole team (shadow logs, suggest makes chips, act sends within "
        "the policy); `own` sets a kind of move's own mode (lineup, streaming, adds_drops), null to follow the "
        "team's. Setting up a team that was never switched on leaves it paused unless `on` is true."
    ),
    responses={
        **_CHECKIN_ERRORS,
        409: {"description": "act while acting is switched off on the server (AI_ACTING_OFF)"},
        422: {"description": "Nothing to change, or an unknown mode or kind of move (AI_AUTOPILOT_INVALID)"},
    },
)
async def set_autopilot(
    req: AiAutopilotReq,
    team_id: int = Path(..., gt=0, le=2_147_483_647),
    user: UserContext = Depends(get_db_user),
) -> AiAutopilotResp:
    data = await AgentService.set_autopilot(user.user_id, team_id, req.on, req.mode,
                                            dict(req.own) if req.own is not None else None)
    message = ("Check-ins on" if data.on else "Check-ins paused") if req.on is not None else "Autopilot updated"
    return AiAutopilotResp(status=ApiStatus.SUCCESS, message=message, data=data)


@router.put(
    "/decisions/{decision_id}/verdict",
    response_model=AiDecisionVerdictResp,
    summary="Agree or disagree with one of Dime's calls",
    description="The caller's verdict on one decision a check-in logged; null clears it. The measure for "
                "whether Dime is ready to act: you agree with most of what it would have done.",
    responses={**_AGENT_ERRORS, 404: {"description": "No such decision for this caller (AI_DECISION_NOT_FOUND)"}},
)
async def decision_verdict(
    req: AiDecisionVerdictReq,
    decision_id: int = Path(..., gt=0),
    user: UserContext = Depends(get_db_user),
) -> AiDecisionVerdictResp:
    data = await AgentService.decision_verdict(decision_id, user.user_id, req.verdict)
    return AiDecisionVerdictResp(status=ApiStatus.SUCCESS, message="Verdict saved", data=data)
