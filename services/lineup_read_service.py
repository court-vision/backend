"""
A day's lineup as ESPN sees it: slots, eligibility, per-game locks, game times.

`parse_espn_lineup` is pure (payload in, dataclasses out). `LineupReadService.read`
does the one ESPN call (mTeam + mRoster + mSettings) and the small DB lookups
around it — today's games for lock times, our player values for tie-breaks,
NBA ids for navigation — and returns the `LineupState` every consumer shares:
the manual editor, the write proxy (before and after a write) and the pipeline
evaluate route.

"Today" here is ESPN's fantasy day: the board is for `scoringPeriodId`, mapped
to a date through the season calendar (`date_for_espn_scoring_period`), never
`core.nba_calendar.nba_date_et` (the 6 AM game-date rule).

A caller may ask for a later ESPN day (`scoring_period_id`): ESPN returns the
roster as set for that day, and edits to it go out as FUTURE_ROSTER
(`services.providers.writers`). ESPN carries an edit forward to later days until
the next day that has its own edit.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Literal, Mapping, Optional

import pytz

from core.errors import BadRequestError
from services.providers.capabilities import ProviderCapabilityMissing
from core.logging import get_logger
from core.settings import settings
from db.base import run_db
from schemas.common import FantasyProvider, LeagueInfo
from schemas.lineup_editor import LineupPlayer, LineupSlotDef, LineupState, WriteBlockedReason
from services.credential_service import normalize_swid
from services.espn_service import EspnService
from services.lineup_planner import ACTIVE_SLOT_IDS, BENCH_SLOT_ID, IR_SLOT_ID, OUT_STATUSES
from services.matchup_days import index_games
from services.nba_id_resolver import nba_ids_by_espn_id
from services.player_value_service import PlayerValueService
from services import schedule_service
from utils.constants import ESPN_FANTASY_ENDPOINT
from utils.espn_helpers import POSITION_MAP, PRO_TEAM_MAP, TEAM_ABBREV_CORRECTIONS

EASTERN = pytz.timezone("US/Eastern")
LINEUP_VIEWS = ["mTeam", "mRoster", "mSettings"]
# Rendering order for slot rows; combo slots appear only when the league uses them.
SLOT_ORDER = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]

OwnerCheck = Literal["ok", "not_owner", "unknown"]

log = get_logger("lineup_read")


# ------------------------------- pure parse ------------------------------- #


@dataclass(frozen=True)
class ParsedEntry:
    player_id: int
    name: str
    pro_team: str
    lineup_slot_id: int
    eligible_slot_ids: tuple[int, ...]
    injured: bool
    injury_status: Optional[str]
    lineup_locked: bool
    default_position_id: Optional[int] = None


@dataclass(frozen=True)
class ParsedLineup:
    espn_team_id: int
    team_name: str
    resolved_by: Literal["id", "name"]
    scoring_period_id: Optional[int]
    slot_counts: dict[int, int]
    lock_type: Optional[str]
    entries: tuple[ParsedEntry, ...]
    owner_check: OwnerCheck
    # Only the capped positions (ESPN sends -1 / 0 for the rest): defaultPositionId -> max players
    position_limits: dict[int, int] = field(default_factory=dict)
    # status.latestScoringPeriod / finalScoringPeriod: ESPN's today and last day, whatever day was read
    current_scoring_period_id: Optional[int] = None
    final_scoring_period_id: Optional[int] = None


def slot_counts_from_names(named: Optional[Mapping[str, int]]) -> dict[int, int]:
    """{"PG": 1, "UT": 3} (usr.leagues.roster_slots) -> {0: 1, 11: 3}."""
    out: dict[int, int] = {}
    for name, count in (named or {}).items():
        slot_id = POSITION_MAP.get(name)
        if isinstance(slot_id, int):
            out[slot_id] = int(count or 0)
    return out


def _entry_player(entry: dict) -> dict:
    pool = entry.get("playerPoolEntry") or {}
    return pool.get("player") or entry.get("player") or {}


def parse_espn_lineup(
    payload: dict,
    *,
    team_name: str,
    espn_team_id: Optional[int] = None,
    swid: Optional[str] = None,
    fallback_slot_counts: Optional[Mapping[str, int]] = None,
) -> ParsedLineup:
    teams = payload.get("teams") or []
    target = None
    resolved_by: Literal["id", "name"] = "name"
    if espn_team_id is not None:
        target = next((t for t in teams if t.get("id") == espn_team_id), None)
        if target is not None:
            resolved_by = "id"
    if target is None:
        wanted = (team_name or "").strip()
        target = next((t for t in teams if (t.get("name") or "").strip() == wanted), None)
    if target is None:
        raise BadRequestError("TEAM_NAME_NOT_IN_LEAGUE", f"Team '{team_name}' not found in league")

    owner_check: OwnerCheck = "unknown"
    owners = target.get("owners")
    if isinstance(owners, list) and owners:
        mine = normalize_swid(swid)
        owner_check = "ok" if mine and mine in {normalize_swid(o) for o in owners} else "not_owner"

    roster_settings = ((payload.get("settings") or {}).get("rosterSettings") or {})
    raw_counts = roster_settings.get("lineupSlotCounts") or {}
    slot_counts = {int(k): int(v or 0) for k, v in raw_counts.items()} if raw_counts else slot_counts_from_names(fallback_slot_counts)
    raw_limits = roster_settings.get("positionLimits") or {}
    position_limits = {int(k): int(v) for k, v in raw_limits.items() if int(v or 0) > 0}

    entries: list[ParsedEntry] = []
    for entry in (target.get("roster") or {}).get("entries") or []:
        player = _entry_player(entry)
        if not player:
            continue
        pool = entry.get("playerPoolEntry") or {}
        team = PRO_TEAM_MAP.get(player.get("proTeamId", 0), "FA")
        entries.append(ParsedEntry(
            player_id=int(player.get("id") or entry.get("playerId") or 0),
            name=player.get("fullName", "Unknown"),
            pro_team=TEAM_ABBREV_CORRECTIONS.get(team, team),
            lineup_slot_id=int(entry.get("lineupSlotId", 0)),
            eligible_slot_ids=tuple(int(s) for s in player.get("eligibleSlots") or []),
            injured=bool(player.get("injured", False)),
            injury_status=player.get("injuryStatus") or entry.get("injuryStatus"),
            lineup_locked=bool(pool.get("lineupLocked", False)),
            default_position_id=int(player["defaultPositionId"]) if player.get("defaultPositionId") is not None else None,
        ))

    status = payload.get("status") or {}
    period = payload.get("scoringPeriodId") or status.get("latestScoringPeriod")
    current = status.get("latestScoringPeriod")    # never the top-level echo of a requested day
    final = status.get("finalScoringPeriod")
    return ParsedLineup(
        espn_team_id=int(target.get("id")),
        team_name=target.get("name") or team_name,
        resolved_by=resolved_by,
        scoring_period_id=int(period) if period else None,
        slot_counts=slot_counts,
        lock_type=roster_settings.get("lineupLocktimeType"),
        entries=tuple(entries),
        owner_check=owner_check,
        position_limits=position_limits,
        current_scoring_period_id=int(current) if current else None,
        final_scoring_period_id=int(final) if final else None,
    )


def roster_version(scoring_period_id: Optional[int], players: list[LineupPlayer]) -> str:
    pairs = sorted(f"{p.player_id}:{p.lineup_slot_id}" for p in players)
    return hashlib.sha1(f"{scoring_period_id}|{','.join(pairs)}".encode()).hexdigest()[:16]


def slot_rows(slot_counts: Mapping[int, int]) -> list[LineupSlotDef]:
    rows = [LineupSlotDef(slot_id=s, slot=POSITION_MAP.get(s, str(s)), count=int(slot_counts.get(s, 0)))
            for s in SLOT_ORDER if int(slot_counts.get(s, 0) or 0) > 0]
    return rows


# ------------------------------- DB helpers (run in the executor) ------------------------------- #


def _games_on(nba_date: date) -> list[Any]:
    from db.models.nba.games import Game
    return Game.get_games_on_date(nba_date)


def _values_for(league_info: LeagueInfo, espn_ids: list[int]) -> tuple[str, dict[int, Any], dict[int, int]]:
    scoring = EspnService._scoring_for(league_info)
    value_kind = PlayerValueService.value_kind_for(scoring)
    values = EspnService._values_for(scoring, espn_ids)
    return value_kind, values, nba_ids_by_espn_id(espn_ids)


def _persist_espn_team_id(team_id: int, espn_team_id: int) -> bool:
    """Record the provider's team id on the STORED json (never the hydrated copy)."""
    from db.models.teams import Team
    team = Team.get_or_none(Team.team_id == team_id)
    if team is None:
        return False
    stored = json.loads(team.league_info)
    if stored.get("espn_team_id") == espn_team_id:
        return False
    stored["espn_team_id"] = espn_team_id
    team.league_info = json.dumps(stored)
    team.save(only=[Team.league_info])
    return True


# ------------------------------- the read ------------------------------- #


class LineupReadService:

    @staticmethod
    async def read(
        team_id: int,
        league_info: LeagueInfo,
        *,
        fallback_slot_counts: Optional[Mapping[str, int]] = None,
        now: Optional[datetime] = None,
        scoring_period_id: Optional[int] = None,
    ) -> LineupState:
        """The board for ESPN's current day, or for `scoring_period_id` (today or a
        later day of the season; anything else is 400 SCORING_PERIOD_OUT_OF_RANGE)."""
        # This is ESPN's reader; every consumer reaches it through the adapter's
        # `read_lineup`, which is where another provider's board will come from.
        if league_info.provider != FantasyProvider.ESPN:
            raise ProviderCapabilityMissing(league_info.provider, "lineup_editing")

        payload = await EspnService.fetch_league(league_info, LINEUP_VIEWS, scoring_period_id=scoring_period_id)
        parsed = parse_espn_lineup(
            payload,
            team_name=league_info.team_name,
            espn_team_id=league_info.espn_team_id,
            swid=league_info.swid,
            fallback_slot_counts=fallback_slot_counts,
        )
        if parsed.resolved_by == "name":
            log.info("lineup_team_resolved_by_name", team_id=team_id, espn_team_id=parsed.espn_team_id)

        now_et = (now or datetime.now(EASTERN)).astimezone(EASTERN)
        if scoring_period_id is None:
            period, source, nba_date = LineupReadService._resolve_period(parsed.scoring_period_id, now_et)
            current = period
        else:
            # The payload's top-level period is the echo of our request; today is in `status`.
            current, source, today = LineupReadService._resolve_period(parsed.current_scoring_period_id, now_et)
            period, nba_date = LineupReadService._requested_period(
                scoring_period_id, current, today, parsed.final_scoring_period_id)

        team_game_map: dict[str, Any] = {}
        first_tip: Optional[time] = None
        if nba_date is not None:
            games = await run_db("lineup.games", _games_on, nba_date)
            _, team_game_map = index_games(games)
            tips = [g.start_time_et for g in games if g.start_time_et]
            first_tip = min(tips) if tips else None

        espn_ids = [e.player_id for e in parsed.entries]
        value_kind, values, nba_ids = "fpts", {}, {}
        try:
            value_kind, values, nba_ids = await run_db("lineup.values", _values_for, league_info, espn_ids)
        except Exception as exc:  # values are a tie-break, never a reason to fail the read
            log.warning("lineup_values_lookup_failed", team_id=team_id, error=str(exc))

        players: list[LineupPlayer] = []
        for e in parsed.entries:
            game = team_game_map.get(e.pro_team)
            opponent = game_time = None
            started = False
            if game is not None:
                opponent = f"vs {game.away_team_id}" if game.home_team_id == e.pro_team else f"@ {game.home_team_id}"
                if game.start_time_et:
                    game_time = game.start_time_et.strftime("%H:%M")
                    tip = EASTERN.localize(datetime.combine(nba_date, game.start_time_et))
                    started = tip <= now_et
            valued = values.get(e.player_id)
            is_out = (e.injury_status or "ACTIVE").upper() in OUT_STATUSES
            players.append(LineupPlayer(
                player_id=e.player_id,
                nba_player_id=nba_ids.get(e.player_id),
                name=e.name,
                team=e.pro_team,
                lineup_slot_id=e.lineup_slot_id,
                lineup_slot=POSITION_MAP.get(e.lineup_slot_id, str(e.lineup_slot_id)),
                eligible_slot_ids=list(e.eligible_slot_ids),
                eligible_slots=[POSITION_MAP.get(s, str(s)) for s in e.eligible_slot_ids],
                injured=e.injured,
                injury_status=e.injury_status if e.injury_status and e.injury_status != "ACTIVE" else None,
                default_position_id=e.default_position_id,
                lineup_locked=e.lineup_locked,
                has_game_today=game is not None,
                opponent=opponent,
                game_time_et=game_time,
                game_started=started,
                locked=e.lineup_locked or started,
                playable=game is not None and not is_out,
                avg_points=float(valued.value) if valued is not None and valued.value is not None else 0.0,
                value_kind=value_kind,
                value_source=valued.source if valued is not None else None,
            ))

        can_write, reason = LineupReadService._write_gate(league_info, period, parsed)
        state = LineupState(
            provider=league_info.provider,
            team_name=parsed.team_name,
            espn_team_id=parsed.espn_team_id,
            nba_date=nba_date.isoformat() if nba_date else None,
            scoring_period_id=period,
            scoring_period_source=source,
            current_scoring_period_id=current,
            final_scoring_period_id=parsed.final_scoring_period_id,
            first_game_time_et=first_tip.strftime("%H:%M") if first_tip else None,
            slot_counts={str(k): v for k, v in sorted(parsed.slot_counts.items())},
            slots=slot_rows(parsed.slot_counts),
            position_limits={str(k): v for k, v in sorted(parsed.position_limits.items())},
            lock_type=parsed.lock_type,
            players=players,
            can_write=can_write,
            write_blocked_reason=reason,
            roster_version=roster_version(period, players),
            fetched_at=now_et.isoformat(timespec="seconds"),
        )

        if league_info.espn_team_id != parsed.espn_team_id:
            try:
                if await run_db("teams.persist_espn_team_id", _persist_espn_team_id, team_id, parsed.espn_team_id):
                    log.info("lineup_espn_team_id_learned", team_id=team_id, espn_team_id=parsed.espn_team_id)
            except Exception as exc:
                log.warning("lineup_espn_team_id_persist_failed", team_id=team_id, error=str(exc))
        return state

    @staticmethod
    def _resolve_period(provider_period: Optional[int], now_et: datetime):
        """(scoring_period_id, source, nba_date) — ESPN's period first, the calendar second."""
        fantasy_today = schedule_service.get_nba_today()
        calendar_period = schedule_service.season_day(fantasy_today)
        if provider_period:
            if calendar_period and calendar_period != provider_period:
                log.info("lineup_period_mismatch", provider=provider_period, calendar=calendar_period)
            try:
                nba_date = schedule_service.date_for_espn_scoring_period(provider_period)
            except Exception:
                nba_date = fantasy_today
            return provider_period, "provider", nba_date
        if calendar_period:
            return calendar_period, "calendar", fantasy_today
        return None, "none", None

    @staticmethod
    def _requested_period(
        requested: int, current: Optional[int], today: Optional[date], final: Optional[int],
    ) -> tuple[int, Optional[date]]:
        """(scoring_period_id, nba_date) for a day the caller asked for.

        ESPN answers 200 for any number (a past day's lineup, a full roster for
        days past the season), so the bounds are ours: today through `finalScoringPeriod`.
        """
        if current is None:
            reason = "ESPN reports no current day for this league"
        elif requested < current:
            reason = f"ESPN day {requested} has passed; today is day {current}"
        elif final and requested > final:
            reason = f"ESPN day {requested} is after the season's last day ({final})"
        else:
            reason = None
        if reason:
            raise BadRequestError("SCORING_PERIOD_OUT_OF_RANGE", reason)
        try:
            nba_date = schedule_service.date_for_espn_scoring_period(requested)
        except Exception:
            nba_date = today + timedelta(days=requested - current) if today else None
        return requested, nba_date

    @staticmethod
    def _write_gate(league_info: LeagueInfo, period: Optional[int], parsed: ParsedLineup) -> tuple[bool, Optional[WriteBlockedReason]]:
        if league_info.provider != FantasyProvider.ESPN:
            return False, "provider_not_supported"
        if not (league_info.espn_s2 and league_info.swid):
            return False, "no_credentials"
        if not settings.roster_writes_enabled:
            return False, "writes_disabled"
        if period is None:
            return False, "no_scoring_period"
        if parsed.owner_check == "not_owner":
            return False, "not_team_owner"
        if not parsed.espn_team_id:
            return False, "team_id_unresolved"
        return True, None
