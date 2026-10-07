"""
Lineup snapshots, read side: a team's roster and slots as they stood on a
finished ESPN day.

usr.lineup_snapshots is written nightly by data-platform (one capture per
league, every team in it); this module reads it and, when a day has no row yet
— last night before the pipeline ran, a league added mid-season — reads the
same day live from ESPN's own per-day history (`?scoringPeriodId=N`, which
answers with every team's roster AS OF day N) and returns it as
`source="provider_history"`. The backend never writes the table.

Rows are keyed by the provider's ids (league, season, team), so a caller may
read another team in the SAME league — the opponent — and nothing else: the
query always carries the owner's league key, and a foreign team id is simply
not found.

A day is finished once ESPN has rolled past it, at ~2 AM ET
(`matchup_days.fantasy_today`), not at the 6 AM ET game-date turn: from 2 AM
the current roster is already the next day's. ESPN's own `latestScoringPeriod`
stays the final word for the live history read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable, Optional

from api.deps import OwnedTeamContext, load_owned_league_info
from core.errors import BadRequestError, NotFoundError
from core.logging import get_logger
from db.base import run_db
from db.models.lineup_snapshots import LineupSnapshot as SnapshotRow, LineupSnapshotPlayer as SnapshotPlayerRow
from schemas.common import ApiStatus, FantasyProvider, LeagueInfo
from schemas.lineup_snapshots import (
    LineupSnapshot,
    LineupSnapshotListData,
    LineupSnapshotListResp,
    LineupSnapshotPlayer,
    LineupSnapshotResp,
)
from services import schedule_service
from services.espn_service import EspnService
from services.lineup_read_service import ParsedLineup, _persist_espn_team_id, parse_espn_lineup
from services.matchup_days import fantasy_today
from services.providers.identity import nba_ids_by_espn_id
from services.team_service import TeamService
from utils.espn_helpers import POSITION_MAP

HISTORY_VIEWS = ["mTeam", "mRoster"]
MAX_RANGE_DAYS = 31
NOT_FOUND = "LINEUP_SNAPSHOT_NOT_FOUND"

log = get_logger("lineup_snapshots")


@dataclass(frozen=True)
class LeagueRef:
    provider: str
    provider_league_id: str
    season: int


def league_ref(league_info: LeagueInfo) -> LeagueRef:
    provider = league_info.provider.value if hasattr(league_info.provider, "value") else str(league_info.provider)
    return LeagueRef(provider=provider, provider_league_id=str(league_info.league_id), season=int(league_info.year))


def public_league_info(team: OwnedTeamContext) -> LeagueInfo:
    """The owner's league key and team name — no credential hydration."""
    return TeamService.deserialize_league_info(json.loads(team.league_info_json or "{}"))


def slot_sort_key(slot_id: Optional[int]) -> int:
    """Starters by slot id (ESPN's 0 PG … 11 UT), then bench (12), then IR (13)."""
    return int(slot_id) if slot_id is not None else 99


def position_name(default_position_id: Optional[int]) -> str:
    """ESPN's defaultPositionId is 1-based (1 = PG … 5 = C), the slot map is 0-based."""
    if default_position_id is None:
        return ""
    return POSITION_MAP.get(int(default_position_id) - 1, "")


def slot_name(slot_id: int) -> str:
    return POSITION_MAP.get(int(slot_id), str(slot_id))


def _sorted_players(players: Iterable[LineupSnapshotPlayer]) -> list[LineupSnapshotPlayer]:
    return sorted(players, key=lambda p: (slot_sort_key(p.lineup_slot_id), p.name))


# ------------------------------- rows / payloads → schema ------------------------------- #


def snapshot_from_rows(header: Any, players: Iterable[Any], nba_ids: dict[int, int]) -> LineupSnapshot:
    rows = [
        LineupSnapshotPlayer(
            player_id=p.player_id,
            nba_player_id=nba_ids.get(p.player_id),
            name=p.player_name,
            team=p.pro_team or "FA",
            position=position_name(p.default_position_id),
            lineup_slot_id=p.lineup_slot_id,
            lineup_slot=slot_name(p.lineup_slot_id),
            eligible_slots=[slot_name(s) for s in (p.eligible_slot_ids or [])],
            injured=bool(p.injured),
            injury_status=p.injury_status,
            applied_total=float(p.applied_total) if p.applied_total is not None else None,
        )
        for p in players
    ]
    captured = header.captured_at
    return LineupSnapshot(
        provider=FantasyProvider(header.provider),
        provider_league_id=header.provider_league_id,
        season=header.season,
        provider_team_id=header.provider_team_id,
        team_name=header.team_name,
        scoring_period_id=header.scoring_period_id,
        nba_date=header.nba_date.isoformat(),
        matchup_period_id=header.matchup_period_id,
        opponent_provider_team_id=header.opponent_provider_team_id,
        applied_stat_total=float(header.applied_stat_total) if header.applied_stat_total is not None else None,
        captured_at=captured.isoformat(timespec="seconds") if captured is not None else None,
        source="snapshot",
        players=_sorted_players(rows),
    )


def snapshot_from_parsed(parsed: ParsedLineup, ref: LeagueRef, scoring_period_id: int, nba_date: date,
                         nba_ids: dict[int, int]) -> LineupSnapshot:
    """ESPN's per-day history, parsed by the lineup editor's parser, in the snapshot shape."""
    rows = [
        LineupSnapshotPlayer(
            player_id=e.player_id,
            nba_player_id=nba_ids.get(e.player_id),
            name=e.name,
            team=e.pro_team,
            position=position_name(e.default_position_id),
            lineup_slot_id=e.lineup_slot_id,
            lineup_slot=slot_name(e.lineup_slot_id),
            eligible_slots=[slot_name(s) for s in e.eligible_slot_ids],
            injured=e.injured,
            injury_status=e.injury_status,
            applied_total=None,
        )
        for e in parsed.entries
    ]
    return LineupSnapshot(
        provider=FantasyProvider(ref.provider),
        provider_league_id=ref.provider_league_id,
        season=ref.season,
        provider_team_id=parsed.espn_team_id,
        team_name=parsed.team_name,
        scoring_period_id=scoring_period_id,
        nba_date=nba_date.isoformat(),
        captured_at=None,
        source="provider_history",
        players=_sorted_players(rows),
    )


# ------------------------------- DB (sync; call through run_db) ------------------------------- #


def _league_rows(ref: LeagueRef):
    return (
        (SnapshotRow.provider == ref.provider)
        & (SnapshotRow.provider_league_id == ref.provider_league_id)
        & (SnapshotRow.season == ref.season)
    )


def _load_snapshots(ref: LeagueRef, team_ids: list[int], dates: list[date]) -> dict[tuple[int, date], LineupSnapshot]:
    """Stored snapshots for these teams on these dates, keyed (team id, date)."""
    if not team_ids or not dates:
        return {}
    headers = list(
        SnapshotRow.select().where(
            _league_rows(ref)
            & (SnapshotRow.provider_team_id.in_(team_ids))
            & (SnapshotRow.nba_date.in_(dates))
        )
    )
    if not headers:
        return {}
    players_by_header: dict[int, list[Any]] = {h.id: [] for h in headers}
    for row in SnapshotPlayerRow.select().where(SnapshotPlayerRow.snapshot.in_([h.id for h in headers])):
        players_by_header[row.snapshot_id].append(row)
    espn_ids = sorted({row.player_id for rows in players_by_header.values() for row in rows})
    nba_ids = nba_ids_by_espn_id(espn_ids)
    return {
        (h.provider_team_id, h.nba_date): snapshot_from_rows(h, players_by_header[h.id], nba_ids)
        for h in headers
    }


def _team_id_by_name(ref: LeagueRef, dates: list[date], team_name: str) -> Optional[int]:
    """The provider team id stored days give to this team name, from the newest
    of `dates` that has it (the id is learned, never guessed). Any stored day
    will do: the newest finished day is the one most likely to be missing."""
    wanted = (team_name or "").strip()
    if not wanted or not dates:
        return None
    row = (
        SnapshotRow.select(SnapshotRow.provider_team_id)
        .where(_league_rows(ref) & (SnapshotRow.nba_date.in_(dates)) & (SnapshotRow.team_name == wanted))
        .order_by(SnapshotRow.nba_date.desc())
        .first()
    )
    return row.provider_team_id if row else None


# ------------------------------- ESPN's per-day history ------------------------------- #


async def _history_from_espn(league_info: LeagueInfo, target: date, sides: dict[Optional[int], Optional[str]]
                             ) -> dict[int, LineupSnapshot]:
    """Read one finished ESPN day live and parse the teams asked for.

    `sides` maps a provider team id (None when the caller's own id is not known
    yet) to a team name used as the fallback match. A day ESPN has not finished
    (its `latestScoringPeriod` is not past it) yields nothing: that lineup is
    still today's, not history.
    """
    scoring_period_id = schedule_service.season_day(target)
    if scoring_period_id is None or not sides:
        return {}
    payload = await EspnService.fetch_league(league_info, HISTORY_VIEWS, scoring_period_id=scoring_period_id)
    latest = (payload.get("status") or {}).get("latestScoringPeriod")
    if latest and int(latest) <= scoring_period_id:
        log.info("lineup_history_day_not_finished", scoring_period_id=scoring_period_id, latest=latest)
        return {}
    ref = league_ref(league_info)
    parsed_sides: list[ParsedLineup] = []
    for team_id, name in sides.items():
        try:
            parsed_sides.append(parse_espn_lineup(payload, team_name=name or "", espn_team_id=team_id))
        except BadRequestError:
            log.info("lineup_history_team_not_in_payload", team_id=team_id, team_name=name)
    espn_ids = sorted({e.player_id for p in parsed_sides for e in p.entries})
    nba_ids = await run_db("lineup_snapshots.nba_ids", nba_ids_by_espn_id, espn_ids) if espn_ids else {}
    return {
        parsed.espn_team_id: snapshot_from_parsed(parsed, ref, scoring_period_id, target, nba_ids)
        for parsed in parsed_sides
    }


# ------------------------------- the service ------------------------------- #


class LineupSnapshotService:

    @staticmethod
    async def get_day(team: OwnedTeamContext, target: date, provider_team_id: Optional[int] = None
                      ) -> LineupSnapshotResp:
        """One team's lineup on one finished day: the stored snapshot, else ESPN's history."""
        info = public_league_info(team)
        if info.provider != FantasyProvider.ESPN:
            raise NotFoundError(NOT_FOUND, "Lineup snapshots are kept for ESPN teams only")
        if target >= fantasy_today():
            raise BadRequestError("DATE_NOT_PAST", f"{target} is not a finished day yet")
        ref = league_ref(info)
        own_id = info.espn_team_id
        wanted = provider_team_id if provider_team_id is not None else own_id
        learned_own_id = False
        if wanted is None:
            wanted = await run_db("lineup_snapshots.team_by_name", _team_id_by_name, ref, [target], info.team_name)
            learned_own_id = wanted is not None

        snapshot: Optional[LineupSnapshot] = None
        if wanted is not None:
            stored = await run_db("lineup_snapshots.day", _load_snapshots, ref, [wanted], [target])
            snapshot = stored.get((wanted, target))
        if snapshot is None:
            is_own = provider_team_id is None or provider_team_id == own_id
            hydrated = await load_owned_league_info(team)
            found = await _history_from_espn(hydrated, target, {wanted: info.team_name if is_own else None})
            snapshot = next(iter(found.values()), None)
            learned_own_id = learned_own_id or (is_own and own_id is None and snapshot is not None)
        if snapshot is None:
            raise NotFoundError(NOT_FOUND, f"No lineup is stored for {target}, and ESPN has no history for it")

        if learned_own_id:
            try:
                if await run_db("teams.persist_espn_team_id", _persist_espn_team_id, team.team_id,
                                snapshot.provider_team_id):
                    log.info("lineup_snapshot_espn_team_id_learned", team_id=team.team_id,
                             espn_team_id=snapshot.provider_team_id)
            except Exception as exc:  # learning the id is a convenience, never the answer
                log.warning("lineup_snapshot_espn_team_id_persist_failed", team_id=team.team_id, error=str(exc))
        return LineupSnapshotResp(status=ApiStatus.SUCCESS, message="Lineup snapshot retrieved", data=snapshot)

    @staticmethod
    async def list_range(team: OwnedTeamContext, from_date: Optional[date], to_date: Optional[date]
                         ) -> LineupSnapshotListResp:
        """The owner's stored snapshots over a date range (default: the current matchup week)."""
        info = public_league_info(team)
        if info.provider != FantasyProvider.ESPN:
            raise NotFoundError(NOT_FOUND, "Lineup snapshots are kept for ESPN teams only")
        today = fantasy_today()
        if from_date is None or to_date is None:
            week = schedule_service.get_current_matchup(today)
            if week:
                from_date, to_date = from_date or week["start_date"], to_date or week["end_date"]
            else:
                from_date, to_date = from_date or today - timedelta(days=7), to_date or today
        if to_date < from_date:
            raise BadRequestError("DATE_RANGE_INVALID", "`to` is before `from`")
        if (to_date - from_date).days + 1 > MAX_RANGE_DAYS:
            raise BadRequestError("DATE_RANGE_TOO_LONG", f"At most {MAX_RANGE_DAYS} days per request")

        ref = league_ref(info)
        dates = [from_date + timedelta(days=i) for i in range((to_date - from_date).days + 1)]
        finished = [d for d in dates if d < today]
        own_id = info.espn_team_id
        if own_id is None and finished:
            own_id = await run_db("lineup_snapshots.team_by_name", _team_id_by_name, ref, finished, info.team_name)
        stored = {}
        if own_id is not None and finished:
            stored = await run_db("lineup_snapshots.range", _load_snapshots, ref, [own_id], finished)
        snapshots = [stored[(own_id, d)] for d in finished if (own_id, d) in stored]
        missing = [d.isoformat() for d in finished if (own_id, d) not in stored]
        return LineupSnapshotListResp(
            status=ApiStatus.SUCCESS,
            message="Lineup snapshots retrieved",
            data=LineupSnapshotListData(
                team_id=team.team_id, provider_team_id=own_id,
                from_date=from_date.isoformat(), to_date=to_date.isoformat(),
                snapshots=snapshots, missing_dates=missing,
            ),
        )

    @staticmethod
    async def rosters_for_matchup(league_info: LeagueInfo, sides: dict[int, str], target: date
                                  ) -> dict[int, LineupSnapshot]:
        """Both matchup sides' rosters on a finished day, keyed by provider team id.

        Stored snapshots first; the sides without one come from a single live
        ESPN read of that day. A side with neither is simply absent — the
        caller shows today's roster for it, labelled as such.
        """
        ref = league_ref(league_info)
        team_ids = [tid for tid in sides if tid is not None]
        stored = await run_db("lineup_snapshots.matchup", _load_snapshots, ref, team_ids, [target])
        found = {tid: snap for (tid, _), snap in stored.items()}
        missing = {tid: name for tid, name in sides.items() if tid is not None and tid not in found}
        if missing:
            try:
                found.update(await _history_from_espn(league_info, target, missing))
            except Exception as exc:  # a failed history read must not take the day down
                log.warning("lineup_history_read_failed", date=str(target), error=str(exc))
        return found
