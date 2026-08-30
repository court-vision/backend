"""
Shared building blocks for daily / weekly matchup breakdowns.

Extracted from matchup_service so the daily and weekly endpoints build days the
same way (they used to carry verbatim copies), and so team totals go through the
league's scoring strategy: per-player fpts use the league's point weights, and
category leagues get per-category day totals plus a comparison.
"""

from __future__ import annotations

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
)
from core.nba_calendar import nba_date_et
from services.scoring.models import CategoryComparisonData, StatLine
from services.scoring.resolver import ResolvedScoring

EASTERN = pytz.timezone("US/Eastern")
STAT_FIELDS = ("pts", "reb", "ast", "stl", "blk", "tov", "min", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta")


def nba_today() -> date:
    """NBA date convention: before 6 AM ET counts as the previous day."""
    return nba_date_et()


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
                      nba_id_to_stats: dict[int, Any], scoring: ResolvedScoring) -> list[DailyMatchupPlayerStats]:
    result: list[DailyMatchupPlayerStats] = []
    for p in roster:
        nba_id = resolve(p)
        stats = nba_id_to_stats.get(nba_id) if nba_id else None
        result.append(DailyMatchupPlayerStats(
            player_id=p.player_id,
            name=p.name,
            team=p.team,
            position=p.position,
            nba_player_id=nba_id,
            had_game=p.team in teams_playing,
            fpts=score_stat_row(stats, scoring) if stats else None,
            **{k: (getattr(stats, k) if stats else None) for k in STAT_FIELDS},
        ))
    # Players with stats first (by fpts desc), then had a game but no stats, then no game
    result.sort(key=lambda x: (0 if x.fpts is not None else (1 if x.had_game else 2), -(x.fpts or 0)))
    return result


def build_future_roster(roster: Iterable[Any], team_game_map: dict[str, Any]) -> list[DailyMatchupFuturePlayer]:
    result: list[DailyMatchupFuturePlayer] = []
    for p in roster:
        game = team_game_map.get(p.team)
        opponent = None
        game_time = None
        if game:
            opponent = f"vs {game.away_team_id}" if game.home_team_id == p.team else f"@ {game.home_team_id}"
            game_time = str(game.start_time_et) if game.start_time_et else None
        result.append(DailyMatchupFuturePlayer(
            player_id=p.player_id, name=p.name, team=p.team, position=p.position,
            has_game=game is not None, opponent=opponent, game_time_et=game_time,
            injured=p.injured, injury_status=p.injury_status,
        ))
    result.sort(key=lambda x: (0 if x.has_game else 1, x.name))
    return result


def player_stat_line(p: DailyMatchupPlayerStats) -> Optional[StatLine]:
    if p.fpts is None:
        return None
    return StatLine.from_dict({k: getattr(p, k) or 0 for k in STAT_FIELDS})


def team_day_totals(roster: list[DailyMatchupPlayerStats], scoring: ResolvedScoring
                    ) -> tuple[float, Optional[dict[str, float]]]:
    total = float(sum(p.fpts for p in roster if p.fpts is not None))
    if not scoring.is_categories or scoring.categories is None:
        return total, None
    lines = [line for line in (player_stat_line(p) for p in roster) if line is not None]
    return total, scoring.categories.team_totals(lines)


def comparison_to_schema(cmp: CategoryComparisonData) -> CategoryComparison:
    return CategoryComparison(
        items=[CategoryScoreItem(key=i.key, label=i.label, you=i.you, opp=i.opp, winner=i.winner,
                                 higher_is_better=i.higher_is_better, is_rate=i.is_rate) for i in cmp.items],
        wins=cmp.wins, losses=cmp.losses, ties=cmp.ties,
    )


def build_day(md: MatchupData, target_date: date, today: date, period_start: date,
              nba_id_to_stats: dict[int, Any], games_on_date: Iterable[Any],
              resolve: Callable[[Any], Optional[int]], scoring: ResolvedScoring) -> DailyMatchupData:
    """One day of a matchup: box scores for past/today, schedule for future days."""
    if target_date < today:
        day_type = "past"
    elif target_date == today:
        day_type = "today"
    else:
        day_type = "future"

    teams_playing, team_game_map = index_games(games_on_date)
    comparison: Optional[CategoryComparison] = None

    if day_type in ("past", "today"):
        your_roster = build_past_roster(md.your_team.roster, resolve, teams_playing, nba_id_to_stats, scoring)
        opp_roster = build_past_roster(md.opponent_team.roster, resolve, teams_playing, nba_id_to_stats, scoring)
        your_total, your_cats = team_day_totals(your_roster, scoring)
        opp_total, opp_cats = team_day_totals(opp_roster, scoring)
        if your_cats is not None and opp_cats is not None and scoring.categories is not None:
            comparison = comparison_to_schema(scoring.categories.compare(your_cats, opp_cats))
        your_team = DailyMatchupTeam(team_name=md.your_team.team_name, team_id=md.your_team.team_id,
                                     total_fpts=your_total, roster=your_roster, categories=your_cats)
        opponent_team = DailyMatchupTeam(team_name=md.opponent_team.team_name, team_id=md.opponent_team.team_id,
                                         total_fpts=opp_total, roster=opp_roster, categories=opp_cats)
    else:
        your_team = DailyMatchupTeam(team_name=md.your_team.team_name, team_id=md.your_team.team_id,
                                     total_fpts=None, roster=build_future_roster(md.your_team.roster, team_game_map))
        opponent_team = DailyMatchupTeam(team_name=md.opponent_team.team_name, team_id=md.opponent_team.team_id,
                                         total_fpts=None, roster=build_future_roster(md.opponent_team.roster, team_game_map))

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
