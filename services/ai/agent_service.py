"""
What the AI desk's routes call (docs/AI_AGENT_PLAN.md § 1, § 6).

Access comes first on every call: the kill switch (`AI_ENABLED`), then the
allowlist (`AI_AGENT_USER_IDS`). A turn then checks, in order: the
conversation is the caller's, no other turn holds it, it isn't full, and one
turn's ceiling can be reserved under both monthly caps. Anything that fails
after the claim releases it (and the reservation) before the error goes out.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

from api.deps import ensure_team_owned, load_owned_league_info
from core.errors import AuthorizationError, ConflictError, NotFoundError
from core.settings import settings
from db.base import run_db
from schemas.ai import (
    AiActing,
    AiAutopilotActionItem,
    AiAutopilotTeam,
    AiCheckinsData,
    AiCheckinSummary,
    AiConversationDetail,
    AiConversationSummary,
    AiDecisionVerdictData,
    AiFeatures,
    AiNote,
    AiProposalCheckData,
    AiProposalChip,
    AiTurnView,
    AiUsageData,
)
from services.ai import (
    acting,
    agent,
    ai_settings,
    autonomy,
    checkins,
    conversations,
    decisions,
    guards,
    levels,
    memory,
    proposals,
    spend,
)
from services.steering import enabled as steering_enabled


def acting_info() -> dict:
    """Whether Dime can act on its own, and the policy it acts within (the autopilot plan, step 5)."""
    return {
        "available": autonomy.acting_available(),
        "steering": steering_enabled(),
        "writes": settings.roster_writes_enabled,
        "policy": {
            "adds_per_day": settings.autopilot_adds_per_day,
            "adds_per_matchup": settings.autopilot_adds_per_matchup,
            "adds_reserve": settings.autopilot_adds_reserve,
            "protected_top": settings.autopilot_protected_top,
            "churn_hours": settings.autopilot_churn_hours,
            "pause_refused": settings.autopilot_pause_refused,
            "pause_same_block": settings.autopilot_pause_same_block,
        },
    }


def has_access(user_id: int) -> bool:
    return user_id in settings.ai_agent_user_ids


def ensure_access(user_id: int) -> None:
    guards.ensure_enabled()
    if not has_access(user_id):
        raise AuthorizationError("AI_INVITE_ONLY", "The assistant is invite-only for now")


def _summary(row, turns: int) -> AiConversationSummary:
    return AiConversationSummary(
        id=row.id,
        title=row.title,
        team_id=row.team_id_id,
        kind=_kind(row),
        created_at=row.created_at,
        updated_at=row.updated_at,
        turns=turns,
    )


def _kind(row) -> str:
    return getattr(row, "kind", None) or conversations.CHAT


def _turn_view(row, parts) -> AiTurnView:
    return AiTurnView(
        id=row.id,
        created_at=row.created_at,
        message=row.message,
        parts=parts,
        outcome=row.outcome,
        feedback=row.feedback,
        comment=row.comment,
        cost_usd=row.cost_micros / 1e6,
    )


async def _conversation(conversation_id: int, user_id: int):
    row = await conversations.get(conversation_id, user_id)
    if row is None:
        raise NotFoundError("AI_CONVERSATION_NOT_FOUND", "Conversation not found")
    return row


class AgentService:

    @staticmethod
    async def create(user_id: int, team_id: Optional[int]) -> AiConversationDetail:
        ensure_access(user_id)
        if team_id is not None and await run_db("ai.owned_team", ensure_team_owned, team_id, user_id) is None:
            raise NotFoundError("TEAM_NOT_FOUND", "Team not found")
        row = await conversations.create(user_id, team_id)
        return AiConversationDetail(**_summary(row, 0).model_dump(), full=False, history=[])

    @staticmethod
    async def list(user_id: int) -> list[AiConversationSummary]:
        ensure_access(user_id)
        return [_summary(item["conversation"], item["turns"]) for item in await conversations.list_for(user_id)]

    @staticmethod
    async def detail(conversation_id: int, user_id: int) -> AiConversationDetail:
        ensure_access(user_id)
        row = await _conversation(conversation_id, user_id)
        turns = await conversations.turns_of(conversation_id, user_id)
        history = await conversations.history(conversation_id, user_id)
        # The chips and a check-in's decisions are shown as they stand now, not as the turn stored them
        parts = await proposals.refresh_history([t.parts or [] for t in turns], conversation_id, user_id)
        checkin = None
        if _kind(row) == conversations.CHECKIN:
            parts = await decisions.refresh_history(parts, conversation_id, user_id)
            found = await checkins.of_conversation(conversation_id, user_id)
            checkin = AiCheckinSummary(**found) if found is not None else None
        return AiConversationDetail(
            **_summary(row, len(turns)).model_dump(),
            full=history.full,
            history=[_turn_view(t, p) for t, p in zip(turns, parts)],
            checkin=checkin,
        )

    @staticmethod
    async def delete(conversation_id: int, user_id: int) -> None:
        ensure_access(user_id)
        if not await conversations.delete(conversation_id, user_id):
            raise NotFoundError("AI_CONVERSATION_NOT_FOUND", "Conversation not found")

    @staticmethod
    async def start_turn(conversation_id: int, user_id: int, message: str,
                         build: Optional[str] = None) -> agent.Emitter:
        """Check everything a turn needs, claim the conversation, and start the turn.
        Errors raised here are ordinary HTTP errors: nothing has streamed yet."""
        ensure_access(user_id)
        row = await _conversation(conversation_id, user_id)
        # Held a little past the turn's own deadline; the turn's row releases it sooner
        hold = timedelta(seconds=settings.ai_agent_turn_timeout_seconds + 30)
        if not await conversations.claim(conversation_id, user_id, hold):
            raise ConflictError("AI_CONVERSATION_BUSY", "This conversation is still answering; wait for it to finish")
        reservation_id = None
        try:
            history = await conversations.history(conversation_id, user_id)
            if history.full:
                raise ConflictError("AI_CONVERSATION_FULL", "This conversation is full; start a new one")
            reservation_id = await spend.reserve(user_id)
            header_line = await agent.header(user_id, row.team_id_id, conversation_id=conversation_id)
        except BaseException:
            # Nothing has been spent: hand back the reservation and the claim
            if reservation_id is not None:
                await spend.cancel(reservation_id)
            await conversations.release(conversation_id)
            raise
        # A follow-up in a check-in's conversation keeps that conversation's tool list, so its replayed
        # history matches; it is the user's own turn, so it logs no decisions and its chips can be sent
        checkin = _kind(row) == conversations.CHECKIN
        tools = agent.CHECKIN_TOOLS if checkin else agent.TOOLS
        allowed = agent.CHECKIN_ALLOWED if checkin else agent.ALLOWED
        turn = agent.Turn(conversation_id=conversation_id, user_id=user_id, message=message, history=history,
                          reservation_id=reservation_id, build=build, tools=tools, allowed=allowed)
        return agent.start(turn, header_line)

    @staticmethod
    async def proposal(proposal_id: int, user_id: int) -> AiProposalCheckData:
        """One chip as it stands, checked again against the board and the pool: what the
        desk reads before it stages the chip into the tray."""
        ensure_access(user_id)
        row = await proposals.get(proposal_id, user_id)
        team = await run_db("ai.owned_team", ensure_team_owned, proposals.team_of(row), user_id)
        if team is None:
            raise NotFoundError("AI_PROPOSAL_NOT_FOUND", "Proposal not found")
        league_info = await load_owned_league_info(team)
        chip, problems = await proposals.check(proposal_id, user_id, team, league_info)
        return AiProposalCheckData(chip=AiProposalChip(**chip), problems=problems)

    @staticmethod
    async def dismiss_proposal(proposal_id: int, user_id: int) -> AiProposalChip:
        ensure_access(user_id)
        return AiProposalChip(**await proposals.dismiss(proposal_id, user_id))

    @staticmethod
    async def usage(user_id: int) -> AiUsageData:
        guards.ensure_enabled()
        month = await spend.month_spend(user_id)
        return AiUsageData(
            access=has_access(user_id),
            month_usd=month.user / 1e6,
            cap_usd=month.user_cap / 1e6 if has_access(user_id) else 0.0,
            model=settings.ai_agent_model,
            effort=settings.ai_agent_effort,
        )

    @staticmethod
    async def comment(turn_id: int, user_id: int, text: Optional[str]) -> Optional[str]:
        """Save (or clear) the user's note on one of their answers; returns what was stored."""
        ensure_access(user_id)
        value = text.strip() if text else None
        if not await conversations.set_comment(turn_id, user_id, value or None):
            raise NotFoundError("AI_TURN_NOT_FOUND", "Turn not found")
        return value or None

    @staticmethod
    async def feedback(turn_id: int, user_id: int, value: Optional[str]) -> None:
        ensure_access(user_id)
        if not await conversations.set_feedback(turn_id, user_id, value):
            raise NotFoundError("AI_TURN_NOT_FOUND", "Turn not found")

    # ---- Dime's check-ins (services/ai/checkins.py) and its automation levels ----

    @staticmethod
    async def checkins(user_id: int, team_id: Optional[int]) -> AiCheckinsData:
        ensure_access(user_id)
        await AgentService._owned_team_or_404(team_id, user_id)
        teams = await checkins.teams_of(user_id)
        rows = await checkins.inbox(user_id, team_id)
        spent, cap = await spend.autopilot_month_spend()
        features = await ai_settings.get(user_id)
        return AiCheckinsData(teams=[AiAutopilotTeam(**t) for t in teams],
                              checkins=[AiCheckinSummary(**r) for r in rows],
                              month_usd=spent / 1e6, cap_usd=cap / 1e6, acting=AiActing(**acting_info()),
                              features=AiFeatures(**features.view()))

    @staticmethod
    async def run_checkin(user_id: int, team_id: int, kind: Optional[str]) -> AiCheckinSummary:
        ensure_access(user_id)
        row = await checkins.run_now(user_id, team_id, kind)
        return AiCheckinSummary(**await checkins.summary(row.id))

    @staticmethod
    async def set_autopilot(user_id: int, team_id: int, change: levels.Change) -> AiAutopilotTeam:
        ensure_access(user_id)
        await checkins.set_autopilot(user_id, team_id, change)
        teams = {t["team_id"]: t for t in await checkins.teams_of(user_id)}
        return AiAutopilotTeam(**teams[team_id])

    @staticmethod
    async def actions(user_id: int, team_id: Optional[int], limit: int) -> list[AiAutopilotActionItem]:
        ensure_access(user_id)
        await AgentService._owned_team_or_404(team_id, user_id)
        return [AiAutopilotActionItem(**a) for a in await acting.list_actions(user_id, team_id, limit)]

    # ---- the account's AI features (services/ai/ai_settings.py) ----

    @staticmethod
    async def features(user_id: int) -> AiFeatures:
        ensure_access(user_id)
        return AiFeatures(**(await ai_settings.get(user_id)).view())

    @staticmethod
    async def set_features(user_id: int, *, memory: Optional[bool], draft_rooms: Optional[bool]) -> AiFeatures:
        ensure_access(user_id)
        return AiFeatures(**(await ai_settings.update(user_id, memory=memory, draft_rooms=draft_rooms)).view())

    @staticmethod
    async def decision_verdict(decision_id: int, user_id: int, verdict: Optional[str]) -> AiDecisionVerdictData:
        ensure_access(user_id)
        block = await decisions.set_verdict(decision_id, user_id, verdict)
        return AiDecisionVerdictData(decision_id=block["decision_id"], verdict=block["verdict"])

    # ---- the agent's notes, as the user sees and edits them ----

    @staticmethod
    async def _owned_team_or_404(team_id: Optional[int], user_id: int) -> None:
        if team_id is not None and await run_db("ai.owned_team", ensure_team_owned, team_id, user_id) is None:
            raise NotFoundError("TEAM_NOT_FOUND", "Team not found")

    @staticmethod
    async def notes(user_id: int, team_id: Optional[int], include_retired: bool = False) -> list[AiNote]:
        ensure_access(user_id)
        await AgentService._owned_team_or_404(team_id, user_id)
        return [AiNote(**n) for n in await memory.list_for_user(user_id, team_id, include_retired)]

    @staticmethod
    async def add_note(user_id: int, *, kind: str, text: str, team_id: Optional[int], recheck_on) -> AiNote:
        ensure_access(user_id)
        await AgentService._owned_team_or_404(team_id, user_id)
        return AiNote(**await memory.add_for_user(user_id, kind=kind, text=text, team_id=team_id,
                                                  recheck_on=recheck_on))

    @staticmethod
    async def edit_note(user_id: int, note_id: int, *, text: Optional[str], kind: Optional[str], recheck_on,
                        clear_recheck: bool) -> AiNote:
        ensure_access(user_id)
        return AiNote(**await memory.edit_for_user(user_id, note_id, text=text, kind=kind, recheck_on=recheck_on,
                                                   clear_recheck=clear_recheck))

    @staticmethod
    async def delete_note(user_id: int, note_id: int) -> None:
        ensure_access(user_id)
        await memory.delete_for_user(user_id, note_id)
