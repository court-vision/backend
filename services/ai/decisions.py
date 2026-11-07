"""
What Dime's check-ins decide, and what the user thinks of it (usr.ai_decisions,
migration 0042; the Dime autopilot plan, step 3).

In shadow a check-in sends nothing. It reads what the automations are about to
do (`get_automations`), and every call it makes is logged with `log_decision`,
holds included: keep auto-lineup's fill, change a lane's next pickup, add a
player no automation would. A change to the lineup or the roster is proposed
first (`propose_lineup` / `propose_move`, which in a check-in store a `shadow`
proposal: checked like a chip, never sendable) and the decision cites it, so
what Dime would have done is a move the server checked, not prose.

The user marks each decision agree or disagree in the AI desk, which is the
measure the step closes on ("you agree with most of what it would have done").
`get_decisions` reads them back, verdicts and the user's comment on the
check-in included, for the night follow-up and the weekly retro.

`log_decision` exists only in a check-in's own turn (the check-in's tool list,
services/ai/checkins.py); a follow-up the user asks in that conversation is an
ordinary turn and is refused.

A change that cites its proposal is also the commitment (the Dime autopilot plan,
step 5; services/ai/acting.py): when every automation the move counts under is at AI
on the team (services/ai/autonomy.py) the server sends it within the policy, and the
decision carries what became of it (`action`); otherwise it stays logged as a review,
what Dime would have done. With DIME_ACTING_ENABLED off the tool reads as before:
every change is a review then.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal, Optional

from peewee import JOIN
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.errors import AppError, NotFoundError
from db.base import db_operation
from db.models.ai_conversations import AiProposal, AiTurn
from db.models.autopilot import AiCheckin, AiDecision
from core.settings import settings
from services.ai import acting, autonomy, proposals

LOG_DECISION_NAME = "log_decision"
GET_DECISIONS_NAME = "get_decisions"
DECISION_KEY = "decision"
AREAS = ("lineup", "streaming", "adds_drops", "other")
AUTOMATIONS = ("auto_lineup", "stream_lane", "scheduled_pickup", "none")
CALLS = ("hold", "change")
VERDICTS = ("agree", "disagree")
SUMMARY_MAX = 200
REASON_MAX = 400
# A change here is a move the server can check, so it must cite its proposal
NEEDS_PROPOSAL = frozenset({"lineup", "adds_drops"})
# Decisions one check-in may log
PER_CHECKIN = 15


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clean(value: str) -> str:
    text = " ".join(value.split())
    if len(text) < 3:
        raise ValueError("write a few words")
    return text


class LogDecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team_id: int = Field(..., gt=0, lt=2**31)
    area: Literal["lineup", "streaming", "adds_drops", "other"]
    automation: Literal["auto_lineup", "stream_lane", "scheduled_pickup", "none"] = "none"
    call: Literal["hold", "change"]
    summary: str = Field(..., min_length=3, max_length=SUMMARY_MAX)
    reason: str = Field(..., min_length=3, max_length=REASON_MAX)
    proposal_id: Optional[int] = Field(None, gt=0)

    _summary = field_validator("summary")(_clean)
    _reason = field_validator("reason")(_clean)

    @model_validator(mode="after")
    def _change_cites_its_move(self) -> "LogDecisionInput":
        if self.proposal_id is not None and self.call != "change":
            raise ValueError("only a change cites a proposal; a hold changes nothing")
        if self.call == "change" and self.area in NEEDS_PROPOSAL and self.proposal_id is None:
            raise ValueError(f"a change to the {'lineup' if self.area == 'lineup' else 'roster'} cites the proposal "
                             "that checked it: call propose_lineup or propose_move first, then pass its proposal_id")
        return self


class GetDecisionsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team_id: int = Field(..., gt=0, lt=2**31)
    since: Optional[date] = None
    call: Optional[Literal["hold", "change"]] = None
    limit: int = Field(20, ge=1, le=50)


LOG_DECISION: dict[str, Any] = {
    "name": LOG_DECISION_NAME,
    "description": (
        "Log one call this check-in makes, holds included: nothing is sent, the log is what you would "
        "have done. area: lineup (the day's slots), streaming (lanes and stream pickups), adds_drops "
        "(your own adds and drops), other. automation: whose plan you reviewed (auto_lineup's fill, a "
        "stream_lane's next decision, a scheduled_pickup, or none for a move no automation is making). "
        "call: hold (keep the plan, or do nothing) or change (you'd do something else). A change to the "
        "lineup or the roster cites the proposal_id propose_lineup or propose_move returned in this "
        "check-in; any other change cites the proposal a tool made for it in this check-in, if one did, "
        "and is otherwise said in the summary. summary: what you "
        "would do, in a line ('Keep auto-lineup's fill for tonight', 'Stream Herb Jones in lane 2's "
        "spot on Thu instead of Kris Dunn'). reason: why, with the number behind it. The user reads "
        "every one and marks it agree or disagree. Only in a check-in."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "team_id": {"type": "integer", "description": "The check-in's team_id"},
            "area": {"type": "string", "enum": list(AREAS)},
            "automation": {"type": "string", "enum": list(AUTOMATIONS),
                           "description": "Whose plan this is about (default none)"},
            "call": {"type": "string", "enum": list(CALLS)},
            "summary": {"type": "string", "maxLength": SUMMARY_MAX, "description": "What you would do, one line"},
            "reason": {"type": "string", "maxLength": REASON_MAX, "description": "Why, with the number behind it"},
            "proposal_id": {"type": "integer",
                            "description": "The proposal behind a change, made in this check-in"},
        },
        "required": ["team_id", "area", "call", "summary", "reason"],
        "additionalProperties": False,
    },
}

# With acting switched on (DIME_ACTING_ENABLED, the plan's step 5), a logged change is also the
# commitment, so the tool says so; off, the check-in prefix is exactly step 3's
LOG_DECISION_ACTING: dict[str, Any] = {
    **LOG_DECISION,
    "description": (
        "Log one call this check-in makes, holds included. area: lineup (the day's slots), streaming (lanes "
        "and stream pickups), adds_drops (your own adds and drops), other. automation: whose plan you reviewed "
        "(auto_lineup's fill, a stream_lane's next decision, a scheduled_pickup, or none for a move no "
        "automation is making). call: hold (keep the plan, or do nothing) or change (do something else). A "
        "change to the lineup or the roster cites the proposal_id propose_lineup or propose_move returned in "
        "this check-in; any other change cites the chip a tool made for it in this check-in, if one did. "
        "Logging a change that cites its proposal commits that move: when every automation it counts under is "
        "at AI (the brief says which are) it is sent now through the user's own write routes, if the policy "
        "allows (get_automations' autopilot section has the caps and what you have used); anywhere else it "
        "is a review, kept as what you would have done and never sent. The result's `action`, present when "
        "Dime handled the move, says what happened: sent, blocked (the rule; the move is left as a chip for "
        "the user), refused (by ESPN or the route) or failed. Never say a move was made unless its action "
        "says sent, and log as a change only what you'd do. summary: what you do or would do, in a line. reason: why, with the number behind it. The user "
        "reads every one and marks it agree or disagree. Only in a check-in."
    ),
}
CHECKIN_LOG_DECISION = LOG_DECISION_ACTING if settings.dime_acting_enabled else LOG_DECISION

GET_DECISIONS: dict[str, Any] = {
    "name": GET_DECISIONS_NAME,
    "description": (
        "What your check-ins decided on a team, newest first: the check-in (kind, date), area, the "
        "automation whose plan it reviewed, hold or change, your summary and reason, the move behind "
        "a change (logged in shadow, never sent), and the user's verdict (agree, disagree, or none "
        "yet) with any comment they left on that check-in. summary counts calls and verdicts over "
        "every matching decision. Read it in a follow-up or a retro to see what you would have done, "
        "then compare it with what happened (get_move_outcomes, get_game_log)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "team_id": {"type": "integer", "description": "One of the user's team_ids"},
            "since": {"type": "string", "format": "date", "description": "Only check-ins on or after this day"},
            "call": {"type": "string", "enum": list(CALLS)},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Default 20"},
        },
        "required": ["team_id"],
        "additionalProperties": False,
    },
}


# ------------------------------- shapes ------------------------------- #


def block_of(row: Any, action: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The decision as a block in the check-in's parts (an `AiDecisionBlock` without its block id).
    `action` is what Dime did about it on its own (services/ai/acting.py), when it acted."""
    return {
        "decision_id": row.id,
        "area": row.area,
        "automation": row.automation,
        "call": row.call,
        "summary": row.summary,
        "reason": row.reason,
        "proposal_id": row.proposal_id_id if hasattr(row, "proposal_id_id") else row.proposal_id,
        "verdict": row.verdict,
        "action": action,
    }


def _move_of(p: Any, now: datetime) -> Optional[dict[str, Any]]:
    if p is None or p.id is None:
        return None
    view = proposals.view_of(p, now)
    # A steering chip (steer, lane, cancel_pickup) carries its `action` in place of add, drop and moves
    keys = ("proposal_id", "state", "kind", "date", "action", "summary") if "action" in view else \
        ("proposal_id", "state", "kind", "date", "add", "drop", "moves", "summary", "projection")
    return {k: view.get(k) for k in keys}


# ------------------------------- DB ------------------------------- #


@db_operation("ai.decision_checkin")
def _checkin(checkin_id: int, user_id: int) -> Optional[AiCheckin]:
    return AiCheckin.get_or_none(AiCheckin.id == checkin_id, AiCheckin.user_id == user_id)


@db_operation("ai.decision_count")
def _count(checkin_id: int) -> int:
    return AiDecision.select().where(AiDecision.checkin_id == checkin_id).count()


@db_operation("ai.decision_proposal")
def _proposal(proposal_id: int, conversation_id: int) -> Optional[AiProposal]:
    return AiProposal.get_or_none(AiProposal.id == proposal_id, AiProposal.conversation_id == conversation_id)


@db_operation("ai.decision_insert")
def _insert(**fields: Any) -> AiDecision:
    return AiDecision.create(**fields)


@db_operation("ai.decisions_read")
def _read(user_id: int, team_id: int, since: Optional[date], call: Optional[str], limit: int):
    where = (AiDecision.user_id == user_id) & (AiDecision.team_id == team_id)
    if since is not None:
        where &= AiCheckin.nba_date >= since
    if call is not None:
        where &= AiDecision.call == call
    base = (AiDecision.select(AiDecision, AiCheckin, AiProposal)
            .join(AiCheckin, on=(AiDecision.checkin_id == AiCheckin.id), attr="ci")
            .switch(AiDecision)
            .join(AiProposal, JOIN.LEFT_OUTER, on=(AiDecision.proposal_id == AiProposal.id), attr="prop")
            .where(where))
    rows = list(base.order_by(AiDecision.id.desc()).limit(limit))
    totals = {"decisions": 0, "hold": 0, "change": 0, "agree": 0, "disagree": 0, "unrated": 0}
    for d in (AiDecision.select(AiDecision.call, AiDecision.verdict)
              .join(AiCheckin, on=(AiDecision.checkin_id == AiCheckin.id)).where(where)):
        totals["decisions"] += 1
        totals[d.call] += 1
        totals[d.verdict or "unrated"] += 1
    turn_ids = {r.ci.turn_id_id for r in rows if r.ci.turn_id_id}
    comments = dict(AiTurn.select(AiTurn.id, AiTurn.comment).where(AiTurn.id.in_(turn_ids)).tuples()) if turn_ids else {}
    return rows, totals, comments


@db_operation("ai.decision_verdict")
def _set_verdict(decision_id: int, user_id: int, verdict: Optional[str], now: datetime) -> Optional[AiDecision]:
    moved = (AiDecision.update(verdict=verdict, verdict_at=now if verdict else None)
             .where(AiDecision.id == decision_id, AiDecision.user_id == user_id).execute())
    return AiDecision.get_or_none(AiDecision.id == decision_id) if moved else None


@db_operation("ai.decisions_of_conversation")
def _of_conversation(conversation_id: int, user_id: int) -> list[AiDecision]:
    return list(AiDecision.select(AiDecision)
                .join(AiCheckin, on=(AiDecision.checkin_id == AiCheckin.id))
                .where(AiCheckin.conversation_id == conversation_id, AiDecision.user_id == user_id))


# ------------------------------- the tools ------------------------------- #


async def log_decision(user_id: int, checkin_id: Optional[int], args: LogDecisionInput) -> dict[str, Any]:
    """Store one of the check-in's calls; returns the model's view with the frontend's block under
    `DECISION_KEY`. Refuses outside a check-in's own turn, a team that isn't the check-in's, a
    proposal from anywhere else, and a check-in past its decision cap."""
    if checkin_id is None:
        raise AppError("AI_NOT_A_CHECKIN", "Only a check-in logs decisions; this turn isn't one, so answer the "
                                           "user directly")
    checkin = await _checkin(checkin_id, user_id)
    if checkin is None or checkin.state != "running":
        raise AppError("AI_NOT_A_CHECKIN", "This check-in is no longer running")
    if args.team_id != checkin.team_id_id:
        raise AppError("AI_DECISION_INVALID", f"This check-in is about team_id {checkin.team_id_id}")
    if await _count(checkin_id) >= PER_CHECKIN:
        raise AppError("AI_DECISION_INVALID", "This check-in has logged as many decisions as it may; "
                                              "write the summary")
    proposal = None
    if args.proposal_id is not None:
        proposal = await _proposal(args.proposal_id, checkin.conversation_id_id)
        if proposal is None:
            raise AppError("AI_DECISION_INVALID", f"Proposal {args.proposal_id} wasn't made in this check-in")
        if proposal.team_id_id != checkin.team_id_id:
            # A check-in may propose for another of the user's teams, but its log is about its own
            raise AppError("AI_DECISION_INVALID", f"Proposal {args.proposal_id} is for another team; this "
                                                  f"check-in is about team_id {checkin.team_id_id}")
    row = await _insert(checkin_id=checkin_id, user_id=user_id, team_id=checkin.team_id_id, area=args.area,
                        automation=args.automation, call=args.call, summary=args.summary, reason=args.reason,
                        proposal_id=args.proposal_id, created_at=_now())
    # A change that cites its move commits it under the team's levels (a review: nothing happens)
    verdict = None
    if args.call == "change" and proposal is not None:
        verdict = await acting.commit(user_id, checkin, row.id, proposal.id)
        if verdict.level != autonomy.REVIEW:
            proposal = await _proposal(proposal.id, checkin.conversation_id_id) or proposal
    done = verdict is not None and verdict.outcome is not None
    block = block_of(row, {"outcome": verdict.outcome, "rule": verdict.rule, "message": verdict.message,
                           "at": _now().isoformat()} if done else None)
    out = {"decision_id": row.id, "logged": True, "call": args.call, "area": args.area,
           "automation": args.automation, "summary": args.summary,
           "move": _move_of(proposal, _now()), DECISION_KEY: block}
    if verdict is not None and verdict.level != autonomy.REVIEW:
        out["action"] = verdict.view()
    return out


async def get_decisions(user_id: int, args: GetDecisionsInput) -> dict[str, Any]:
    rows, totals, comments = await _read(user_id, args.team_id, args.since, args.call, args.limit)
    now = _now()
    out = []
    for d in rows:
        c = d.ci
        p = getattr(d, "prop", None) if d.proposal_id_id is not None else None
        out.append({
            "decision_id": d.id,
            "checkin": {"kind": c.kind, "date": c.nba_date.isoformat(), "manual": c.manual},
            "area": d.area, "automation": d.automation, "call": d.call,
            "summary": d.summary, "reason": d.reason,
            "move": _move_of(p, now),
            "verdict": d.verdict,
            "user_comment": comments.get(c.turn_id_id) if c.turn_id_id else None,
        })
    return {"team_id": args.team_id, "summary": totals, "decisions": out}


# ------------------------------- the desk ------------------------------- #


async def set_verdict(decision_id: int, user_id: int, verdict: Optional[str]) -> dict[str, Any]:
    """The user's agree / disagree on one decision; None clears it."""
    if verdict is not None and verdict not in VERDICTS:
        raise AppError("AI_DECISION_INVALID", "A verdict is agree or disagree", status_code=422)
    row = await _set_verdict(decision_id, user_id, verdict, _now())
    if row is None:
        raise NotFoundError("AI_DECISION_NOT_FOUND", "Decision not found")
    return block_of(row)


def _is_decision(part: dict[str, Any]) -> bool:
    return part.get("kind") == "block" and (part.get("block") or {}).get("type") == "decision"


async def refresh_history(history: list[list[dict[str, Any]]], conversation_id: int,
                          user_id: int) -> list[list[dict[str, Any]]]:
    """A check-in conversation's turns with each decision block as it stands now (the verdict the
    user set since). One read of the conversation's decisions covers every turn."""
    if not any(_is_decision(p) for parts in history for p in parts):
        return history
    rows = {row.id: row for row in await _of_conversation(conversation_id, user_id)}
    actions = await acting.actions_of(list(rows))

    def refresh(part: dict[str, Any]) -> dict[str, Any]:
        row = rows.get(part["block"].get("decision_id")) if _is_decision(part) else None
        if row is None:
            return part
        return {**part, "block": {**part["block"], **block_of(row, actions.get(row.id)), "id": part["block"].get("id")}}
    return [[refresh(p) for p in parts] for parts in history]
