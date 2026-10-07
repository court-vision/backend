"""
Shared building blocks for daily / weekly matchup breakdowns.

Extracted from matchup_service so the daily and weekly endpoints build days the
same way (they used to carry verbatim copies), and so team totals go through the
league's scoring strategy: per-player fpts use the league's point weights, and
category leagues get per-category day totals plus a comparison.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable, Optional

import pytz

from schemas.matchup import (
    CategoryComparison,
    CategoryScoreItem,
    DailyMatchupData,
    DailyMatchupFuturePlayer,
    DailyMatchupPlayerStats,
    DailyMatchupTeam,
    MatchupData,
    RosterSource,
)
from core.nba_calendar import nba_date_et
from services import schedule_service
from services.scoring.models import CategoryComparisonData, StatLine
from services.scoring.resolver import ResolvedScoring

EASTERN = pytz.timezone("US/Eastern")
STAT_FIELDS = ("pts", "reb", "ast", "stl", "blk", "tov", "min", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta")
BENCH_SLOT_ID = 12
IR_SLOT_ID = 13


@dataclass(frozen=True)
class RosterSide:
    """One team's roster for a past day and where it came from.

    `players` None means the team's current roster stands in (the old
    behaviour); a snapshot or provider-history roster carries each player's
    `lineup_slot_id`, which is what lets the day total count starters only.
    """

    players: Optional[list[Any]] = None
    source: RosterSource = "current"
    captured_at: Optional[str] = None

    @property
    def dated(self) -> bool:
        return self.players is not None and self.source != "current"


@dataclass(frozen=True)
class DayRosters:
    your: RosterSide = RosterSide()
    opp: RosterSide = RosterSide()


def counts_for_the_day(p: Any) -> bool:
    """A player whose slot that day was not bench or IR (slot unknown counts)."""
    slot_id = getattr(p, "lineup_slot_id", None)
    return slot_id is None or slot_id not in (BENCH_SLOT_ID, IR_SLOT_ID)


def nba_today() -> date:
    """NBA date convention: before 6 AM ET counts as the previous day."""
    return nba_date_et()


def fantasy_today() -> date:
    """ESPN's fantasy day: before 2 AM ET counts as the previous day.

    The day a lineup belongs to. ESPN rolls its day at ~2 AM ET
    (`schedule_service.get_nba_today`); from then on the previous day's lineup
    is history (a lineup snapshot, or ESPN's per-day history) and the current
    roster is already the next day's. `nba_today` is the game date, which stays
    on last night until 6 AM ET. The two rules disagree for four hours a night
    on purpose and must not be merged: this one answers "is that day's lineup
    final", that one "which night's box scores are these".
    """
    return schedule_service.get_nba_today()


def make_nba_id_resolver(all_roster: Iterable[Any]) -> Callable[[Any], Optional[int]]:
    """Resolve fantasy roster players to nba.players ids: ESPN id first, normalized name second."""
    from db.models.nba.players import Player

    roster = list(all_roster)
    espn_ids = [p.player_id for p in roster]
    espn_to_nba: dict[int, int] = {}
    if espn_ids:
        espn_to_nba = {p.espn_id: p.id for p in Player.select().where(Player.espn_id.in_(espn_ids))}
    unresolved = [p.name.lower().strip() for p in roster if p.player_id not in espn_to_nba]
    name_to_nba: dict[str, int] = {}
    if unresolved:
        name_to_nba = {p.name_normalized: p.id for p in Player.select().where(Player.name_normalized.in_(unresolved))}

    def resolve(roster_player: Any) -> Optional[int]:
        nba_id = espn_to_nba.get(roster_player.player_id)
        if nba_id:
            return nba_id
        return name_to_nba.get(roster_player.name.lower().strip())

    return resolve


def index_games(games: Iterable[Any]) -> tuple[set[str], dict[str, Any]]:
    teams_playing: set[str] = set()
    team_game_map: dict[str, Any] = {}
    for game in games:
        teams_playing.add(game.home_team_id)
        teams_playing.add(game.away_team_id)
        team_game_map[game.home_team_id] = game
        team_game_map[game.away_team_id] = game
    return teams_playing, team_game_map


def score_stat_row(row: Any, scoring: ResolvedScoring) -> float:
    """League-weighted fantasy points for one stat row (stored fpts when weights are default)."""
    points = scoring.points
    if points.is_default and not points.uses_game_only_stats:
        return float(row.fpts)
    return round(points.score(StatLine.from_game_row(row)), 1)


def build_past_roster(roster: Iterable[Any], resolve: Callable[[Any], Optional[int]], teams_playing: set[str],
                      nba_id_to_stats: dict[int, Any], scoring: ResolvedScoring, *,
                      with_slots: bool = False) -> list[DailyMatchupPlayerStats]:
    """Box scores for a roster on a past day.

    `with_slots` is for a roster that is really that day's (a snapshot or
    ESPN's history): each player's slot comes through and the rows sit in
    lineup order — starters by slot, then bench, then IR, fpts desc within a
    slot. Today's roster standing in for a past day never carries slots.
    """
    result: list[DailyMatchupPlayerStats] = []
    for p in roster:
        nba_id = resolve(p)
        stats = nba_id_to_stats.get(nba_id) if nba_id else None
        slot_id = getattr(p, "lineup_slot_id", None) if with_slots else None
        result.append(DailyMatchupPlayerStats(
            player_id=p.player_id,
            name=p.name,
            team=p.team,
            position=p.position,
            nba_player_id=nba_id,
            had_game=p.team in teams_playing,
            lineup_slot=getattr(p, "lineup_slot", None) if with_slots else None,
            lineup_slot_id=slot_id,
            injury_status=getattr(p, "injury_status", None) if with_slots else None,
            fpts=score_stat_row(stats, scoring) if stats else None,
            **{k: (getattr(stats, k) if stats else None) for k in STAT_FIELDS},
        ))
    if with_slots:
        result.sort(key=lambda x: (x.lineup_slot_id if x.lineup_slot_id is not None else 99, -(x.fpts or 0), x.name))
    else:
        # Players with stats first (by fpts desc), then had a game but no stats, then no game
        result.sort(key=lambda x: (0 if x.fpts is not None else (1 if x.had_game else 2), -(x.fpts or 0)))
    return result


def build_future_roster(roster: Iterable[Any], team_game_map: dict[str, Any],
                        resolve: Callable[[Any], Optional[int]]) -> list[DailyMatchupFuturePlayer]:
    result: list[DailyMatchupFuturePlayer] = []
    for p in roster:
        game = team_game_map.get(p.team)
        opponent = None
        game_time = None
        if game:
            opponent = f"vs {game.away_team_id}" if game.home_team_id == p.team else f"@ {game.home_team_id}"
            game_time = str(game.start_time_et) if game.start_time_et else None
        result.append(DailyMatchupFuturePlayer(
            player_id=p.player_id, nba_player_id=resolve(p),
            name=p.name, team=p.team, position=p.position,
            has_game=game is not None, opponent=opponent, game_time_et=game_time,
            injured=p.injured, injury_status=p.injury_status,
        ))
    result.sort(key=lambda x: (0 if x.has_game else 1, x.name))
    return result


def player_stat_line(p: DailyMatchupPlayerStats) -> Optional[StatLine]:
    if p.fpts is None:
        return None
    return StatLine.from_dict({k: getattr(p, k) or 0 for k in STAT_FIELDS})


def team_day_totals(roster: list[DailyMatchupPlayerStats], scoring: ResolvedScoring, *,
                    active_only: bool = False) -> tuple[float, Optional[dict[str, float]]]:
    """The day's fpts and (category leagues) per-category totals.

    `active_only` counts the players whose slot that day was not bench/IR —
    ESPN's own accounting — and is used whenever the roster is really that
    day's. Without slots every rostered player counts, as before.
    """
    counted = [p for p in roster if counts_for_the_day(p)] if active_only else list(roster)
    total = float(sum(p.fpts for p in counted if p.fpts is not None))
    if not scoring.is_categories or scoring.categories is None:
        return total, None
    lines = [line for line in (player_stat_line(p) for p in counted) if line is not None]
    return total, scoring.categories.team_totals(lines)


def comparison_to_schema(cmp: CategoryComparisonData) -> CategoryComparison:
    return CategoryComparison(
        items=[CategoryScoreItem(key=i.key, label=i.label, you=i.you, opp=i.opp, winner=i.winner,
                                 higher_is_better=i.higher_is_better, is_rate=i.is_rate) for i in cmp.items],
        wins=cmp.wins, losses=cmp.losses, ties=cmp.ties,
    )


def build_day(md: MatchupData, target_date: date, today: date, period_start: date,
              nba_id_to_stats: dict[int, Any], games_on_date: Iterable[Any],
              resolve: Callable[[Any], Optional[int]], scoring: ResolvedScoring,
              rosters: Optional[DayRosters] = None) -> DailyMatchupData:
    """One day of a matchup: box scores for past/today, schedule for future days.

    `rosters` supplies a past day's real rosters (lineup snapshots or ESPN's
    history) side by side; a side left at its default uses the current roster.
    """
    if target_date < today:
        day_type = "past"
    elif target_date == today:
        day_type = "today"
    else:
        day_type = "future"

    teams_playing, team_game_map = index_games(games_on_date)
    comparison: Optional[CategoryComparison] = None
    sides = rosters or DayRosters()

    if day_type in ("past", "today"):
        your_players = sides.your.players if sides.your.players is not None else md.your_team.roster
        opp_players = sides.opp.players if sides.opp.players is not None else md.opponent_team.roster
        your_roster = build_past_roster(your_players, resolve, teams_playing, nba_id_to_stats, scoring,
                                        with_slots=sides.your.dated)
        opp_roster = build_past_roster(opp_players, resolve, teams_playing, nba_id_to_stats, scoring,
                                       with_slots=sides.opp.dated)
        your_total, your_cats = team_day_totals(your_roster, scoring, active_only=sides.your.dated)
        opp_total, opp_cats = team_day_totals(opp_roster, scoring, active_only=sides.opp.dated)
        if your_cats is not None and opp_cats is not None and scoring.categories is not None:
            comparison = comparison_to_schema(scoring.categories.compare(your_cats, opp_cats))
        your_team = DailyMatchupTeam(team_name=md.your_team.team_name, team_id=md.your_team.team_id,
                                     total_fpts=your_total, roster=your_roster, categories=your_cats,
                                     roster_source=sides.your.source, lineup_captured_at=sides.your.captured_at)
        opponent_team = DailyMatchupTeam(team_name=md.opponent_team.team_name, team_id=md.opponent_team.team_id,
                                         total_fpts=opp_total, roster=opp_roster, categories=opp_cats,
                                         roster_source=sides.opp.source, lineup_captured_at=sides.opp.captured_at)
    else:
        your_team = DailyMatchupTeam(team_name=md.your_team.team_name, team_id=md.your_team.team_id,
                                     total_fpts=None, roster=build_future_roster(md.your_team.roster, team_game_map, resolve))
        opponent_team = DailyMatchupTeam(team_name=md.opponent_team.team_name, team_id=md.opponent_team.team_id,
                                         total_fpts=None, roster=build_future_roster(md.opponent_team.roster, team_game_map, resolve))

    return DailyMatchupData(
        date=target_date.isoformat(),
        day_type=day_type,
        day_of_week=target_date.strftime("%a"),
        day_index=(target_date - period_start).days,
        matchup_period=md.matchup_period,
        matchup_period_start=md.matchup_period_start,
        matchup_period_end=md.matchup_period_end,
        your_team=your_team,
        opponent_team=opponent_team,
        scoring_format=scoring.format,
        category_comparison=comparison,
    )
