"""
Notification API Routes

Endpoints for managing notification preferences and checking lineup issues.
Uses Clerk authentication (same as teams, matchups, etc.).
"""

import json
from datetime import date
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Query

from api.deps import OwnedTeamContext, UserContext, get_db_user, get_owned_team, load_owned_league_info
from core.nba_calendar import nba_date_et
from db.base import db_operation, run_db
from services.providers.blocking import run_blocking_provider
from db.models.teams import Team
from db.models.nba.games import Game
from db.models.notifications import NotificationPreference, NotificationLog, NotificationTeamPreference
from services import credential_service
from services.lineup_check_service import LineupCheckService
from services.lineup_editor_service import move_results, planner_players, slot_counts_of, unfilled_results
from services.lineup_planner import plan_fill
from services.lineup_read_service import LineupReadService
from services.notification_service import NotificationService
from services.providers.http import provider_get
from schemas.common import ApiStatus
from schemas.notifications import (
    NotificationPreferenceReq,
    NotificationPreferenceResp,
    NotificationPreferenceResponse,
    NotificationTeamPreferenceReq,
    NotificationTeamPreferenceResp,
    NotificationTeamPreferenceListResponse,
    NotificationTeamPreferenceSingleResponse,
    LineupIssueResp,
    LineupCheckResp,
    LineupCheckResponse,
)
from utils.constants import ESPN_FANTASY_ENDPOINT
from utils.espn_helpers import POSITION_MAP, PRO_TEAM_MAP, TEAM_ABBREV_CORRECTIONS


router = APIRouter(prefix="/notifications", tags=["notifications"])

lineup_checker = LineupCheckService()


def _parse_espn_roster_with_slots(data: dict, team_name: str) -> list[dict] | None:
    """Pure parser for the notification-specific roster representation."""
    target = next(
        (team for team in data.get("teams", []) if team.get("name", "").strip() == team_name.strip()),
        None,
    )
    if target is None:
        return None
    roster = []
    for entry in target.get("roster", {}).get("entries", []):
        player = entry.get("playerPoolEntry", {}).get("player", {}) or entry.get("player", {})
        if not player:
            continue
        team = PRO_TEAM_MAP.get(player.get("proTeamId", 0), "FA")
        roster.append({
            "name": player.get("fullName", "Unknown"),
            "team": TEAM_ABBREV_CORRECTIONS.get(team, team),
            "lineup_slot": POSITION_MAP.get(entry.get("lineupSlotId", 0), ""),
            "injured": player.get("injured", False),
            "injury_status": player.get("injuryStatus"),
        })
    return roster


async def _fetch_espn_roster_with_slots(league_info: dict) -> list[dict] | None:
    """Fetch notification roster data through the shared async ESPN boundary."""
    from core.settings import settings

    year = league_info.get("year", settings.espn_year)
    data = await provider_get(
        "espn",
        ESPN_FANTASY_ENDPOINT.format(year, league_info["league_id"]),
        params={"view": ["mTeam", "mRoster"]},
        cookies={"espn_s2": league_info.get("espn_s2", ""), "SWID": league_info.get("swid", "")},
        expect_key="teams",
    )
    return _parse_espn_roster_with_slots(data, league_info.get("team_name", ""))


def _lineup_check_context(user_id: int, team_id: int) -> dict | None:
    """Materialize every DB input needed by the on-demand notification flows."""
    team = Team.get_or_none((Team.team_id == team_id) & (Team.user_id == user_id))
    if team is None:
        return None
    league_info = credential_service.hydrate(team, json.loads(team.league_info))
    today = nba_date_et()
    prefs = NotificationPreference.get_or_none(NotificationPreference.user == user_id)
    return {
        "league_info": league_info,
        "team_json": team.league_info,
        "teams_playing": set(Game.get_teams_playing_on_date(today)),
        "earliest_game_time": Game.get_earliest_game_time_on_date(today),
        "prefs": {
            "email": getattr(prefs, "email", None),
            "alert_benched_starters": getattr(prefs, "alert_benched_starters", True),
            "alert_active_non_playing": getattr(prefs, "alert_active_non_playing", True),
            "alert_injured_active": getattr(prefs, "alert_injured_active", True),
        },
    }


@router.get("/preferences", response_model=NotificationPreferenceResponse)
@db_operation("notifications.get_preferences")
def get_preferences(user: UserContext = Depends(get_db_user)):
    """Get the current user's notification preferences (or defaults)."""
    user_id = user.user_id

    prefs = (
        NotificationPreference.select()
        .where(NotificationPreference.user == user_id)
        .first()
    )

    if prefs:
        data = NotificationPreferenceResp(
            lineup_alerts_enabled=prefs.lineup_alerts_enabled,
            alert_benched_starters=prefs.alert_benched_starters,
            alert_active_non_playing=prefs.alert_active_non_playing,
            alert_injured_active=prefs.alert_injured_active,
            alert_minutes_before=prefs.alert_minutes_before,
            auto_lineup_enabled=prefs.auto_lineup_enabled,
            email=prefs.email,
        )
    else:
        # Return defaults
        data = NotificationPreferenceResp()

    return NotificationPreferenceResponse(
        status=ApiStatus.SUCCESS,
        message="Notification preferences retrieved",
        data=data,
    )


@router.put("/preferences", response_model=NotificationPreferenceResponse)
@db_operation("notifications.update_preferences")
def update_preferences(
    req: NotificationPreferenceReq,
    user: UserContext = Depends(get_db_user),
):
    """Create or update notification preferences for the current user."""
    user_id = user.user_id

    prefs = (
        NotificationPreference.select()
        .where(NotificationPreference.user == user_id)
        .first()
    )

    if prefs:
        prefs.lineup_alerts_enabled = req.lineup_alerts_enabled
        prefs.alert_benched_starters = req.alert_benched_starters
        prefs.alert_active_non_playing = req.alert_active_non_playing
        prefs.alert_injured_active = req.alert_injured_active
        prefs.alert_minutes_before = req.alert_minutes_before
        prefs.auto_lineup_enabled = req.auto_lineup_enabled
        prefs.email = req.email
        prefs.save()
    else:
        prefs = NotificationPreference.create(
            user=user_id,
            lineup_alerts_enabled=req.lineup_alerts_enabled,
            alert_benched_starters=req.alert_benched_starters,
            alert_active_non_playing=req.alert_active_non_playing,
            alert_injured_active=req.alert_injured_active,
            alert_minutes_before=req.alert_minutes_before,
            auto_lineup_enabled=req.auto_lineup_enabled,
            email=req.email,
        )

    data = NotificationPreferenceResp(
        lineup_alerts_enabled=prefs.lineup_alerts_enabled,
        alert_benched_starters=prefs.alert_benched_starters,
        alert_active_non_playing=prefs.alert_active_non_playing,
        alert_injured_active=prefs.alert_injured_active,
        alert_minutes_before=prefs.alert_minutes_before,
        auto_lineup_enabled=prefs.auto_lineup_enabled,
        email=prefs.email,
    )

    return NotificationPreferenceResponse(
        status=ApiStatus.SUCCESS,
        message="Notification preferences updated",
        data=data,
    )


@router.get("/team-preferences", response_model=NotificationTeamPreferenceListResponse)
@db_operation("notifications.team_preferences")
def get_team_preferences(user: UserContext = Depends(get_db_user)):
    """List all team-level notification preference overrides for the current user."""
    user_id = user.user_id

    rows = list(
        NotificationTeamPreference.select()
        .where(NotificationTeamPreference.user == user_id)
    )

    data = [
        NotificationTeamPreferenceResp(
            team_id=row.team_id,
            has_override=True,
            lineup_alerts_enabled=row.lineup_alerts_enabled,
            alert_benched_starters=row.alert_benched_starters,
            alert_active_non_playing=row.alert_active_non_playing,
            alert_injured_active=row.alert_injured_active,
            alert_minutes_before=row.alert_minutes_before,
            auto_lineup_enabled=row.auto_lineup_enabled,
            email=row.email,
        )
        for row in rows
    ]

    return NotificationTeamPreferenceListResponse(
        status=ApiStatus.SUCCESS,
        message=f"Found {len(data)} team preference override(s)",
        data=data,
    )


@router.put("/team-preferences/{team_id}", response_model=NotificationTeamPreferenceSingleResponse)
@db_operation("notifications.upsert_team_preference")
def upsert_team_preference(
    team_id: int,
    req: NotificationTeamPreferenceReq,
    user: UserContext = Depends(get_db_user),
):
    """Create or update a team-level notification preference override."""
    user_id = user.user_id

    # Verify team belongs to user
    team = (
        Team.select()
        .where((Team.team_id == team_id) & (Team.user_id == user_id))
        .first()
    )
    if not team:
        return NotificationTeamPreferenceSingleResponse(
            status=ApiStatus.NOT_FOUND,
            message="Team not found",
            data=None,
        )

    # Upsert
    existing = (
        NotificationTeamPreference.select()
        .where(
            (NotificationTeamPreference.user == user_id)
            & (NotificationTeamPreference.team_id == team_id)
        )
        .first()
    )

    # Only the fields the caller actually sent are written, as the request model promises: an
    # omitted field keeps whatever the override already held, while an explicit null clears it
    # back to the global preference.
    supplied = req.model_dump(exclude_unset=True)

    if existing:
        for field, value in supplied.items():
            setattr(existing, field, value)
        existing.save()
        row = existing
    else:
        row = NotificationTeamPreference.create(user=user_id, team_id=team_id, **supplied)

    data = NotificationTeamPreferenceResp(
        team_id=row.team_id,
        has_override=True,
        lineup_alerts_enabled=row.lineup_alerts_enabled,
        alert_benched_starters=row.alert_benched_starters,
        alert_active_non_playing=row.alert_active_non_playing,
        alert_injured_active=row.alert_injured_active,
        alert_minutes_before=row.alert_minutes_before,
        auto_lineup_enabled=row.auto_lineup_enabled,
        email=row.email,
    )

    return NotificationTeamPreferenceSingleResponse(
        status=ApiStatus.SUCCESS,
        message="Team preference override saved",
        data=data,
    )


@router.delete("/team-preferences/{team_id}")
@db_operation("notifications.delete_team_preference")
def delete_team_preference(
    team_id: int,
    user: UserContext = Depends(get_db_user),
):
    """Delete a team-level override, reverting that team to global defaults."""
    user_id = user.user_id

    # Verify team belongs to user
    team = (
        Team.select()
        .where((Team.team_id == team_id) & (Team.user_id == user_id))
        .first()
    )
    if not team:
        return {"status": ApiStatus.NOT_FOUND.value, "message": "Team not found"}

    deleted = (
        NotificationTeamPreference.delete()
        .where(
            (NotificationTeamPreference.user == user_id)
            & (NotificationTeamPreference.team_id == team_id)
        )
        .execute()
    )

    if deleted:
        return {"status": ApiStatus.SUCCESS.value, "message": "Team preference override deleted"}
    else:
        return {"status": ApiStatus.NOT_FOUND.value, "message": "No override found for this team"}


@router.get("/check-lineup/{team_id}", response_model=LineupCheckResponse)
async def check_lineup(
    team_id: int,
    user: UserContext = Depends(get_db_user),
):
    """
    Manually check lineup issues for a specific team.

    Returns lineup issues without sending a notification.
    Useful for on-demand checking from the frontend.
    """
    context = await run_db("notifications.check_context", _lineup_check_context, user.user_id, team_id)
    if context is None:
        return LineupCheckResponse(
            status=ApiStatus.NOT_FOUND,
            message="Team not found",
            data=None,
        )

    league_info = context["league_info"]
    provider = league_info.get("provider", "espn")

    if provider != "espn":
        return LineupCheckResponse(
            status=ApiStatus.ERROR,
            message="Only ESPN teams are supported for lineup checks",
            data=None,
        )

    # Get teams playing today
    teams_playing = context["teams_playing"]
    earliest_game_time = context["earliest_game_time"]

    # Fetch roster through the shared async ESPN client.
    roster = await _fetch_espn_roster_with_slots(league_info)

    if not roster:
        return LineupCheckResponse(
            status=ApiStatus.ERROR,
            message="Failed to fetch roster from ESPN",
            data=None,
        )

    prefs = SimpleNamespace(**context["prefs"])

    # Check lineup
    issues = lineup_checker.check_lineup(
        roster=roster,
        teams_playing_today=teams_playing,
        prefs=prefs,
    )

    team_name = league_info.get("team_name", "Your Team")

    data = LineupCheckResp(
        team_name=team_name,
        issues=[
            LineupIssueResp(
                issue_type=issue.issue_type.value,
                player_name=issue.player_name,
                player_team=issue.player_team,
                current_slot=issue.current_slot,
                suggested_action=issue.suggested_action,
                injury_status=issue.injury_status,
            )
            for issue in issues
        ],
        first_game_time=str(earliest_game_time) if earliest_game_time else None,
        teams_playing_today=sorted(teams_playing),
    )

    return LineupCheckResponse(
        status=ApiStatus.SUCCESS,
        message=f"Found {len(issues)} lineup issue(s)" if issues else "No lineup issues found",
        data=data,
    )


@router.post("/send-test/{team_id}")
async def send_test_alert(
    team: OwnedTeamContext = Depends(get_owned_team),
    email: str = Query(..., description="Email address to send the test alert to"),
):
    """
    Force-send today's lineup alert for a team to an address of your choice.

    Bypasses the pipeline's tip-off window and daily dedup. The content is the
    same fill plan the pipeline would mail (services.lineup_planner): the
    suggested moves, or a "nothing to fill" confirmation when the lineup is set.
    """
    from datetime import datetime as _dt

    league_info = await load_owned_league_info(team)
    if league_info.provider != "espn":
        return {"status": "error", "message": "Only ESPN teams are supported"}

    state = await LineupReadService.read(team.team_id, league_info,
                                         fallback_slot_counts=dict(getattr(team.league, "roster_slots", None) or {}))
    plan = plan_fill(planner_players(state), slot_counts_of(state))
    moves = [m.model_dump() for m in move_results(list(plan.moves), state)]
    unfilled = [u.model_dump() for u in unfilled_results(plan)]
    first_game_time = _dt.strptime(state.first_game_time_et, "%H:%M").time() if state.first_game_time_et else None
    team_context = SimpleNamespace(league_info=team.league_info_json)
    user_obj = SimpleNamespace(user_id=team.user_id, email=email)
    prefs = SimpleNamespace(email=email)
    notification_svc = NotificationService()

    if not moves:
        result = await run_blocking_provider(
            "email", "notification_test_email", notification_svc._send_email,
            to=email, subject=f"Court Vision Test: nothing to fill for {state.team_name}",
            body=f"Team: {state.team_name}\nFirst game today: {first_game_time or 'No games today'}\n\n"
                 f"{plan.summary}\n\n-- Court Vision",
        )
    else:
        result = await run_blocking_provider(
            "email", "notification_test_alert", notification_svc.send_lineup_alert,
            user=user_obj, team=team_context, moves=moves, unfilled=unfilled,
            first_game_time=first_game_time, prefs=prefs,
        )

    return {
        "status": "sent" if result.success else "failed",
        "message": (f"Alert sent with {len(moves)} move(s)" if moves else "Nothing to fill; sent confirmation email")
                   if result.success else f"Send failed: {result.error}",
        "moves": moves,
        "unfilled": unfilled,
        "summary": plan.summary,
        "email_result": {"success": result.success, "message_id": result.message_id, "error": result.error},
        "first_game_time": str(first_game_time) if first_game_time else None,
        "scoring_period_id": state.scoring_period_id,
    }


@router.get("/history")
@db_operation("notifications.history")
def get_notification_history(
    user: UserContext = Depends(get_db_user),
    limit: int = Query(default=10, ge=1, le=50),
):
    """Get recent notification history for the current user."""
    user_id = user.user_id

    logs = (
        NotificationLog.select()
        .where(NotificationLog.user == user_id)
        .order_by(NotificationLog.created_at.desc())
        .limit(limit)
    )

    return {
        "status": ApiStatus.SUCCESS.value,
        "message": f"Found {len(logs)} notifications",
        "data": [
            {
                "id": str(log.id),
                "team_id": log.team_id,
                "notification_type": log.notification_type,
                "notification_date": str(log.notification_date),
                "status": log.status,
                "alert_data": json.loads(log.alert_data) if log.alert_data else None,
                "created_at": str(log.created_at),
                "sent_at": str(log.sent_at) if log.sent_at else None,
            }
            for log in logs
        ],
    }
