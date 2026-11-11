"""
Our availability verdicts as the roster tools read them (nba.player_availability,
written by data-platform's availability pipeline; the news plan's steps 3b and 6).

    GET /teams/{id}/availability  ->  AvailabilityService.for_team

One row per player per game date: out, doubtful, questionable, probable or available,
the source that decided it (`decided_by`), the reason and when it last changed. The
Week desk paints its cells from these, with ESPN's status string as the fallback for a
player we hold no verdict on. Daily actions (services/daily_actions_service.py) and the
late-scratch swap (services/late_scratch_service.py) put them in place of ESPN's status
when planning slots; IR moves stay on ESPN's own injury flag, since ESPN decides who may
go on IR.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional, Sequence

from db.base import run_db
from schemas.availability import TeamAvailabilityData, TeamAvailabilityResp, Verdict
from schemas.common import ApiStatus

# The verdicts that keep a player off the floor, or likely do
SITS = frozenset({"out", "doubtful"})

# The source that decided a verdict, in words (decided_by)
SOURCE_WORDS = {
    "nba_report": "the NBA injury report",
    "nba_boxscore": "the NBA box score",
    "espn_injuries": "ESPN's injury list",
    "espn_fantasy": "ESPN fantasy",
    "news": "the news",
}

# How many players and days one read may name (both rosters of a matchup, a week of days)
MAX_PLAYERS = 60
MAX_DAYS = 14


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def rows(nba_ids: Sequence[int], start: date, end: date) -> list[Verdict]:
    """Blocking: every verdict for these players from `start` to `end`, by player then date."""
    from db.models.nba.availability import PlayerAvailability
    ids = sorted({int(i) for i in nba_ids})
    if not ids:
        return []
    found = (PlayerAvailability.select()
             .where(PlayerAvailability.player.in_(ids) & (PlayerAvailability.game_date >= start)
                    & (PlayerAvailability.game_date <= end))
             .order_by(PlayerAvailability.player, PlayerAvailability.game_date))
    return [
        Verdict(
            nba_player_id=int(r.player_id), game_date=r.game_date.isoformat(), verdict=r.verdict,
            final=bool(r.final), decided_by=r.decided_by, reason=r.reason, detail=r.detail,
            expected_return=r.expected_return.isoformat() if r.expected_return else None,
            previous_verdict=r.previous_verdict, changed_at=_aware(r.changed_at),
        )
        for r in found
    ]


def planner_verdicts(found: Sequence[Verdict], game_date: date) -> dict[int, tuple[str, Optional[str]]]:
    """NBA id -> (verdict, the one before it) on that day, as `planner_players` takes them."""
    day = game_date.isoformat()
    return {v.nba_player_id: (v.verdict, v.previous_verdict) for v in found if v.game_date == day}


class AvailabilityService:

    @staticmethod
    async def for_team(nba_ids: Sequence[int], start: date, end: date) -> TeamAvailabilityResp:
        """The verdicts on these players (the matchup's two rosters) for those days."""
        ids = sorted({int(i) for i in nba_ids if int(i) > 0})[:MAX_PLAYERS]
        found = await run_db("availability.team", rows, ids, start, end)
        return TeamAvailabilityResp(
            status=ApiStatus.SUCCESS,
            message=f"{len(found)} verdict(s)",
            data=TeamAvailabilityData(start=start.isoformat(), end=end.isoformat(), verdicts=found),
        )

    @staticmethod
    async def today(nba_ids: Sequence[int], game_date: date) -> list[Verdict]:
        """The verdicts on these players for one day (daily actions)."""
        return await run_db("availability.today", rows, list(nba_ids), game_date, game_date)
