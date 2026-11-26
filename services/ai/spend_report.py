"""
The AI spend report and the caps editor, for the data platform's AI spend page.

Every model call the backend makes is priced as it happens (services/ai/spend.py)
and lands in usr.ai_spend, the ledger the monthly caps sum. The report reads that
ledger back, split by what spent it, and beside it what each call's own record
says it used (the desk's turns in usr.ai_turns, the analyst's reads in
nba.analyst_reads): its model and its tokens.

What spent a ledger row comes from its turn: the conversation's kind (a chat, a
draft room, or a check-in's conversation, where the user's own reply is desk
spend and the check-in itself is autopilot spend) and, for a check-in, its kind
in usr.ai_checkins. A row whose turn is gone (its chat was deleted) or was never
written (the turn is running, or died) has no feature beyond its line.

Not in the ledger, so not here: the evals run from a laptop, and the old
`/ai/ask` and `/ai/route` routes, which keep their own daily quota.

The caps editor writes usr.ai_caps (migration 0054). A line's cap, the whole
app's (line `app`, migration 0056), or one user's cap on the desk or autopilot
line, set there replaces the environment's number from the next reservation on;
deleting it hands the number back.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from core.errors import BadRequestError, NotFoundError
from core.logging import get_logger
from core.settings import settings
from db.base import db, db_operation
from db.models.ai_conversations import AiCap
from db.models.teams import Team
from db.models.users import User
from schemas.ai_spend import (
    AiCapData, AiCapReq, AiSpendApp, AiSpendLine, AiSpendReport, AiSpendRow, AiSpendTeam, AiSpendUser, AiUsageRow,
    AiUserLine,
)
from services.ai import spend
from services.ai.spend import ANALYST, APP, AUTOPILOT, DESK, LINES

MAX_DAYS = 90

# What spent each ledger row (schemas/ai_spend.py names them)
_LEDGER_SQL = """
SELECT (s.created_at AT TIME ZONE 'UTC')::date AS day,
       s.line,
       CASE
           WHEN s.line = 'analyst' THEN 'analyst'
           WHEN s.line = 'autopilot' THEN 'checkin'
           WHEN c.kind = 'draft' THEN 'draft'
           WHEN c.kind = 'checkin' THEN 'checkin_reply'
           WHEN c.kind = 'chat' THEN 'chat'
           ELSE 'unlinked'
       END AS feature,
       ch.kind AS detail,
       s.user_id,
       COALESCE(c.team_id, ch.team_id) AS team_id,
       COALESCE(SUM(s.cost_micros), 0) AS cost_micros,
       COALESCE(SUM(s.cost_micros) FILTER (WHERE NOT s.settled), 0) AS open_micros,
       COUNT(*) AS entries
FROM usr.ai_spend s
LEFT JOIN usr.ai_turns t ON t.id = s.turn_id
LEFT JOIN usr.ai_conversations c ON c.id = t.conversation_id
LEFT JOIN usr.ai_checkins ch ON s.line = 'autopilot' AND ch.turn_id = s.turn_id
WHERE s.created_at >= %s
GROUP BY 1, 2, 3, 4, 5, 6
ORDER BY 1, 2, 3
"""

# What each call's record says it used. A turn in a check-in's conversation is the
# check-in's own when usr.ai_checkins points at it, and the user's reply otherwise.
_USAGE_SQL = """
SELECT (t.created_at AT TIME ZONE 'UTC')::date AS day,
       CASE
           WHEN ch.id IS NOT NULL THEN 'checkin'
           WHEN c.kind = 'checkin' THEN 'checkin_reply'
           WHEN c.kind = 'draft' THEN 'draft'
           ELSE 'chat'
       END AS feature,
       COALESCE(t.model, 'unknown') AS model,
       COUNT(*) AS records,
       COALESCE(SUM(t.model_calls), 0),
       COALESCE(SUM(t.input_tokens), 0),
       COALESCE(SUM(t.output_tokens), 0),
       COALESCE(SUM(t.cache_read_input_tokens), 0),
       COALESCE(SUM(t.cache_creation_input_tokens), 0),
       COALESCE(SUM(t.cost_micros), 0)
FROM usr.ai_turns t
JOIN usr.ai_conversations c ON c.id = t.conversation_id
LEFT JOIN usr.ai_checkins ch ON ch.turn_id = t.id
WHERE t.created_at >= %s
GROUP BY 1, 2, 3
UNION ALL
SELECT (r.created_at AT TIME ZONE 'UTC')::date,
       'analyst',
       COALESCE(r.model, 'unknown'),
       COUNT(*),
       COUNT(*) FILTER (WHERE r.input_tokens > 0 OR r.output_tokens > 0),
       COALESCE(SUM(r.input_tokens), 0),
       COALESCE(SUM(r.output_tokens), 0),
       0,
       0,
       COALESCE(SUM(r.cost_micros), 0)
FROM nba.analyst_reads r
WHERE r.created_at >= %s
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3
"""

# This month per line, and per user on the lines that have users
_MONTH_LINES_SQL = """
SELECT line,
       COALESCE(SUM(cost_micros) FILTER (WHERE settled), 0),
       COALESCE(SUM(cost_micros) FILTER (WHERE NOT settled), 0)
FROM usr.ai_spend
WHERE created_at >= %s
GROUP BY line
"""

_MONTH_USERS_SQL = """
SELECT user_id, line, COALESCE(SUM(cost_micros), 0)
FROM usr.ai_spend
WHERE created_at >= %s AND user_id IS NOT NULL
GROUP BY user_id, line
"""


def _start_of(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


def _team_name(team: Team) -> str:
    try:
        return json.loads(team.league_info).get("team_name") or f"Team {team.team_id}"
    except (json.JSONDecodeError, AttributeError, TypeError):
        return f"Team {team.team_id}"


def _view(row: Optional[AiCap], cap: Optional[int], default: Optional[int]) -> dict[str, Any]:
    return {
        "cap_micros": cap,
        "default_cap_micros": default,
        "cap_set": row is not None,
        "updated_at": row.updated_at if row is not None else None,
        "updated_by": row.updated_by if row is not None else None,
    }


def _line_defaults(rows: dict[tuple[str, Optional[int]], AiCap]) -> dict[str, tuple[int, int]]:
    """Each line's cap in force and its default, and the whole app's, in micros."""
    out = {}
    for line in (*LINES, APP):
        default = spend._micros(spend.default_line_usd(line))
        row = rows.get((line, None))
        out[line] = (spend._micros(row.monthly_usd) if row is not None else default, default)
    return out


def _user_line(line: str, user_id: int, spent: int, rows: dict[tuple[str, Optional[int]], AiCap],
               line_caps: dict[str, tuple[int, int]]) -> AiUserLine:
    # A user's default: an equal part of the desk's cap in force, or none on the autopilot line
    default = spend.desk_share(line_caps[DESK][0]) if line == DESK else None
    row = rows.get((line, user_id))
    cap = spend._micros(row.monthly_usd) if row is not None else default
    return AiUserLine(spent_micros=spent, **_view(row, cap, default))


@db_operation("ai.spend_report")
def _report(days: int, now: datetime) -> AiSpendReport:
    today = now.astimezone(timezone.utc).date()
    since = today - timedelta(days=days - 1)
    month = spend.month_start(now)
    window_start = _start_of(since)

    cap_rows = {(row.line, row.user_id_id): row for row in AiCap.select()}
    line_caps = _line_defaults(cap_rows)

    month_lines = {line: (settled, held) for line, settled, held in
                   db.execute_sql(_MONTH_LINES_SQL, (month,)).fetchall()}
    lines = []
    for line in LINES:
        settled, held = month_lines.get(line, (0, 0))
        cap, default = line_caps[line]
        lines.append(AiSpendLine(line=line, spent_micros=int(settled), open_micros=int(held),
                                 **_view(cap_rows.get((line, None)), cap, default)))
    app_cap, app_default = line_caps[APP]
    # Every line's spend, the lines above included, as the whole app's cap counts it
    app = AiSpendApp(spent_micros=sum(int(settled) for settled, _held in month_lines.values()),
                     open_micros=sum(int(held) for _settled, held in month_lines.values()),
                     **_view(cap_rows.get((APP, None)), app_cap, app_default))

    rows = [AiSpendRow(day=r[0], line=r[1], feature=r[2], detail=r[3], user_id=r[4], team_id=r[5],
                       cost_micros=int(r[6]), open_micros=int(r[7]), entries=int(r[8]))
            for r in db.execute_sql(_LEDGER_SQL, (window_start,)).fetchall()]
    usage = [AiUsageRow(day=r[0], feature=r[1], model=r[2], records=int(r[3]), model_calls=int(r[4]),
                        input_tokens=int(r[5]), output_tokens=int(r[6]), cache_read_input_tokens=int(r[7]),
                        cache_creation_input_tokens=int(r[8]), cost_micros=int(r[9]))
             for r in db.execute_sql(_USAGE_SQL, (window_start, window_start)).fetchall()]

    # Users: the allowlist, anyone who spent this month or in the window, anyone with a cap
    month_users: dict[int, dict[str, int]] = {}
    for user_id, line, total in db.execute_sql(_MONTH_USERS_SQL, (month,)).fetchall():
        month_users.setdefault(user_id, {})[line] = int(total)
    allowed = set(settings.ai_agent_user_ids)
    ids = (allowed | set(month_users) | {r.user_id for r in rows if r.user_id is not None}
           | {uid for (_line, uid) in cap_rows if uid is not None})
    users = []
    for user in User.select(User.user_id, User.email).where(User.user_id.in_(list(ids))).order_by(User.user_id):
        spent = month_users.get(user.user_id, {})
        users.append(AiSpendUser(
            user_id=user.user_id, email=user.email, allowed=user.user_id in allowed,
            desk=_user_line(DESK, user.user_id, spent.get(DESK, 0), cap_rows, line_caps),
            autopilot=_user_line(AUTOPILOT, user.user_id, spent.get(AUTOPILOT, 0), cap_rows, line_caps),
        ))

    team_ids = {r.team_id for r in rows if r.team_id is not None}
    teams = [AiSpendTeam(team_id=t.team_id, user_id=t.user_id_id, name=_team_name(t))
             for t in Team.select().where(Team.team_id.in_(list(team_ids))).order_by(Team.team_id)] if team_ids else []

    return AiSpendReport(
        as_of=now, month_start=month.date(), since=since, days=days,
        turn_max_micros=spend._micros(settings.ai_agent_turn_max_usd),
        read_max_micros=spend._micros(settings.analyst_read_max_usd),
        lines=lines, app=app, rows=rows, usage=usage, users=users, teams=teams,
    )


async def report(days: int = 30, now: Optional[datetime] = None) -> AiSpendReport:
    """The month so far against each cap, and the last `days` UTC days of spend and usage."""
    if not 1 <= days <= MAX_DAYS:
        raise BadRequestError("AI_SPEND_BAD_WINDOW", f"days must be 1 to {MAX_DAYS}")
    return await _report(days, now or datetime.now(timezone.utc))


# One cap write at a time: two saves of a cap nobody had set would otherwise both
# find no row and both insert, and the second would hit the unique index.
_CAPS_LOCK = 0x41_49_43_50


@db_operation("ai.set_cap")
def _set_cap(line: str, user_id: Optional[int], monthly_usd: Optional[Decimal], author: Optional[str],
             now: datetime) -> AiCapData:
    """Write (or delete) the row and read back the cap in force, in one transaction under
    one lock, so the answer is a single state. The change is logged once it commits."""
    with db.atomic():
        db.execute_sql("SELECT pg_advisory_xact_lock(%s)", (_CAPS_LOCK,))
        who = AiCap.user_id.is_null() if user_id is None else AiCap.user_id == user_id
        row = AiCap.select().where(AiCap.line == line, who).first()
        before = row.monthly_usd if row is not None else None
        if monthly_usd is None:
            if row is not None:
                row.delete_instance()
            row = None
        elif row is None:
            row = AiCap.create(line=line, user_id=user_id, monthly_usd=monthly_usd,
                               updated_at=now, updated_by=author)
        else:
            row.monthly_usd, row.updated_at, row.updated_by = monthly_usd, now, author
            row.save()

        caps = spend.caps_sync(line, user_id)
        if user_id is None:
            default = spend._micros(spend.default_line_usd(line))
            data = AiCapData(line=line, **_view(row, caps.line, default))
        else:
            default = spend.desk_share(caps.line) if line == DESK else None
            data = AiCapData(line=line, user_id=user_id, **_view(row, caps.user, default))

    get_logger().info("ai_cap_set", line=line, user_id=user_id,
                      before_usd=float(before) if before is not None else None,
                      after_usd=float(monthly_usd) if monthly_usd is not None else None, author=author)
    return data


@db_operation("ai.cap_user_exists")
def _user_exists(user_id: int) -> bool:
    return User.select().where(User.user_id == user_id).exists()


async def set_cap(req: AiCapReq, now: Optional[datetime] = None) -> AiCapData:
    """Set or clear one cap. A user's cap is on the desk or autopilot line only."""
    if req.user_id is not None:
        if req.line == ANALYST:
            raise BadRequestError("AI_CAP_NO_USERS", "The analyst's reads belong to no user: its cap is the line's")
        if req.line == APP:
            raise BadRequestError("AI_CAP_NO_USERS", "The whole app's cap is everyone's: it takes no user")
        if not await _user_exists(req.user_id):
            raise NotFoundError("AI_CAP_USER_NOT_FOUND", f"No user {req.user_id}")
    author = (req.author or "").strip() or None
    return await _set_cap(req.line, req.user_id, req.monthly_usd, author, now or datetime.now(timezone.utc))
