from typing import Iterable, Optional

from schemas.team_insights import (
    TeamInsightsResp,
    TeamInsightsData,
    EnrichedRosterPlayer,
    PlayerScheduleInfo,
    CategoryStrengths,
    ScheduleOverview,
    RosterHealthSummary,
)
from schemas.espn import PlayerResp
from schemas.matchup import CategoryComparison
from schemas.common import ApiStatus, FantasyProvider
from services.team_service import TeamService
from services.espn_service import EspnService
from services.yahoo_service import YahooService
from services.matchup_service import MatchupService
from services.matchup_days import comparison_to_schema
from services.player_value_service import PlayerValueService
from services.player_service import _normalize_name
from services.scoring.categories import CategoryScoring
from services.scoring.models import CategoryDef, StatLine
from services.scoring.resolver import ResolvedScoring, resolve_scoring_for_team
from services.scoring.vocab import DEFAULT_CATEGORIES
from services import schedule_service
from core.logging import get_logger
from db.base import DB_RUNTIME_ERRORS, run_db


# Injury statuses that mean the player is out
_OUT_STATUSES = {"OUT", "O", "IL", "IL+", "SUSPENSION"}
_DTD_STATUSES = {"DAY_TO_DAY", "DTD"}
_GTD_STATUSES = {"GTD", "QUESTIONABLE", "DOUBTFUL"}

# Rolling window (days) behind category strengths and the opponent comparison.
STRENGTHS_WINDOW_DAYS = 14


def _classify_injury(injury_status: Optional[str]) -> str:
    """Classify an injury status string into a health category."""
    if not injury_status:
        return "healthy"
    upper = injury_status.upper()
    if upper in _OUT_STATUSES:
        return "out"
    if upper in _DTD_STATUSES:
        return "day_to_day"
    if upper in _GTD_STATUSES:
        return "game_time_decision"
    return "healthy"


class TeamInsightsService:

    @staticmethod
    def _stored_windows(team_id: int, league_info, roster: list[PlayerResp]):
        scoring = resolve_scoring_for_team(team_id)
        weights = scoring.point_weights
        if league_info.provider == FantasyProvider.YAHOO:
            lookups = [(player.name, player.team) for player in roster]
            return (
                scoring,
                PlayerValueService.rolling_avg_by_name(lookups, days=7, weights=weights),
                PlayerValueService.rolling_avg_by_name(lookups, days=14, weights=weights),
                PlayerValueService.rolling_avg_by_name(lookups, days=30, weights=weights),
            )
        espn_ids = [player.player_id for player in roster]
        return (
            scoring,
            PlayerValueService.rolling_avg_by_espn_id(espn_ids, days=7, weights=weights),
            PlayerValueService.rolling_avg_by_espn_id(espn_ids, days=14, weights=weights),
            PlayerValueService.rolling_avg_by_espn_id(espn_ids, days=30, weights=weights),
        )

    @staticmethod
    async def get_team_insights(team_id: int) -> TeamInsightsResp:
        log = get_logger()
        try:
            # Step 1: Get team info and route to provider for base roster
            # Credentials, not the client-facing view — this calls a provider.
            league_info = await TeamService.credentials_for(team_id)
            # Fetch base roster from ESPN or Yahoo
            if league_info.provider == FantasyProvider.YAHOO:
                roster_resp = await YahooService.get_team_data(league_info, 0, team_id)
            else:
                roster_resp = await EspnService.get_team_data(league_info, 0)

            if roster_resp.status != ApiStatus.SUCCESS or not roster_resp.data:
                return TeamInsightsResp(
                    status=ApiStatus.ERROR,
                    message="Failed to fetch roster data",
                    data=None,
                )

            base_roster: list[PlayerResp] = roster_resp.data

            # Step 2: Schedule enrichment (cache per NBA team)
            matchup = schedule_service.get_current_matchup()
            schedule_cache: dict[str, PlayerScheduleInfo] = {}

            if matchup:
                for player in base_roster:
                    team_abbrev = player.team
                    if team_abbrev not in schedule_cache:
                        game_days = schedule_service.get_remaining_game_days(team_abbrev)
                        games_remaining = schedule_service.get_remaining_games(team_abbrev)
                        has_b2b = schedule_service.has_remaining_b2b(team_abbrev)
                        schedule_cache[team_abbrev] = PlayerScheduleInfo(
                            game_days=game_days,
                            games_remaining=games_remaining,
                            has_b2b=has_b2b,
                        )

            # Step 3: Batch stat window lookups
            scoring, avgs_l7, avgs_l14, avgs_l30 = await run_db(
                "team_insights.windows", TeamInsightsService._stored_windows,
                team_id, league_info, base_roster,
            )
            if league_info.provider == FantasyProvider.YAHOO:
                def _get_avg(player: PlayerResp, avgs: dict, key_type: str = "name") -> Optional[float]:
                    normalized = _normalize_name(player.name)
                    return avgs.get(normalized)
            else:
                def _get_avg(player: PlayerResp, avgs: dict, key_type: str = "espn_id") -> Optional[float]:
                    return avgs.get(player.player_id)

            # Step 4: Build enriched roster
            enriched_roster: list[EnrichedRosterPlayer] = []
            for player in base_roster:
                schedule_info = schedule_cache.get(player.team)
                enriched_roster.append(EnrichedRosterPlayer(
                    player_id=player.player_id,
                    name=player.name,
                    avg_points=player.avg_points,
                    team=player.team,
                    valid_positions=player.valid_positions,
                    injured=player.injured,
                    injury_status=player.injury_status,
                    value_source=player.value_source,
                    schedule=schedule_info,
                    avg_fpts_l7=_get_avg(player, avgs_l7),
                    avg_fpts_l14=_get_avg(player, avgs_l14),
                    avg_fpts_l30=_get_avg(player, avgs_l30),
                ))

            # Step 5: Category strengths (L14 rolling window) and opponent comparison
            your_lines = await run_db(
                "team_insights.roster_lines", _roster_lines, base_roster, league_info.provider
            )
            your_line = StatLine.sum(your_lines) if your_lines else None
            category_strengths = _strengths_from_line(your_line) if your_line is not None else None
            opponent_strengths: Optional[CategoryStrengths] = None
            category_comparison: Optional[CategoryComparison] = None
            if your_line is not None:
                opponent_strengths, category_comparison = await _opponent_comparison(
                    team_id, your_line, league_info.provider, scoring,
                )

            # Step 6: Schedule overview
            schedule_overview = None
            if matchup:
                roster_teams = set(p.team for p in base_roster)
                teams_with_b2b = [
                    t for t in roster_teams
                    if schedule_service.has_remaining_b2b(t)
                ]

                # Compute per-day game counts across roster
                day_game_counts = [0] * matchup["game_span"]
                for player in enriched_roster:
                    if player.schedule:
                        for day in player.schedule.game_days:
                            if 0 <= day < len(day_game_counts):
                                day_game_counts[day] += 1

                total_team_games = sum(
                    p.schedule.games_remaining for p in enriched_roster if p.schedule
                )

                schedule_overview = ScheduleOverview(
                    matchup_number=matchup["matchup_number"],
                    matchup_start=str(matchup["start_date"]),
                    matchup_end=str(matchup["end_date"]),
                    current_day_index=matchup["current_day_index"],
                    game_span=matchup["game_span"],
                    total_team_games=total_team_games,
                    teams_with_b2b=sorted(teams_with_b2b),
                    day_game_counts=day_game_counts,
                )

            # Step 7: Roster health summary
            health_counts = {"healthy": 0, "out": 0, "day_to_day": 0, "game_time_decision": 0}
            for player in enriched_roster:
                category = _classify_injury(player.injury_status)
                health_counts[category] += 1

            roster_health = RosterHealthSummary(
                total_players=len(enriched_roster),
                healthy=health_counts["healthy"],
                out=health_counts["out"],
                day_to_day=health_counts["day_to_day"],
                game_time_decision=health_counts["game_time_decision"],
            )

            # Step 8: Projected week FPTS
            projected_week_fpts = 0.0
            for player in enriched_roster:
                if _classify_injury(player.injury_status) != "out" and player.schedule:
                    projected_week_fpts += player.avg_points * player.schedule.games_remaining
            projected_week_fpts = round(projected_week_fpts, 1)

            return TeamInsightsResp(
                status=ApiStatus.SUCCESS,
                message="Team insights fetched successfully",
                data=TeamInsightsData(
                    roster=enriched_roster,
                    category_strengths=category_strengths,
                    opponent_category_strengths=opponent_strengths,
                    category_comparison=category_comparison,
                    scoring_format=scoring.format,
                    value_kind=PlayerValueService.value_kind_for(scoring),
                    schedule_overview=schedule_overview,
                    roster_health=roster_health,
                    projected_week_fpts=projected_week_fpts,
                ),
            )

        except DB_RUNTIME_ERRORS:
            raise
        except Exception as e:
            log.error("get_team_insights_error", error=str(e), team_id=team_id)
            return TeamInsightsResp(
                status=ApiStatus.ERROR,
                message="Internal server error",
                data=None,
            )


# ---- Category strengths ----------------------------------------------------------------


def _roster_lines(roster: Iterable, provider: FantasyProvider,
                  days: int = STRENGTHS_WINDOW_DAYS) -> list[StatLine]:
    """Per-game average StatLine for each rostered player over the rolling window.

    Works for any roster payload whose players expose `player_id` (ESPN id),
    `name`, and `team`. Players with no data in the window are skipped.
    """
    players = list(roster)
    if provider == FantasyProvider.YAHOO:
        lines = PlayerValueService.rolling_lines_by_name([(p.name, p.team) for p in players], days=days)
    else:
        lines = PlayerValueService.rolling_lines_by_espn_id([p.player_id for p in players], days=days)
    return [line for line in lines.values() if line is not None]


def _strengths_from_line(line: StatLine, window_days: int = STRENGTHS_WINDOW_DAYS) -> CategoryStrengths:
    """Strengths from a team per-game total line (the sum of players' per-game lines).

    StatLine.get recomputes FG%/FT% from the summed makes and attempts, so the
    percentages are volume-weighted 0-1 fractions rather than a mean of player rates.
    """
    return CategoryStrengths(
        avg_points=round(line.pts, 1),
        avg_rebounds=round(line.reb, 1),
        avg_assists=round(line.ast, 1),
        avg_steals=round(line.stl, 1),
        avg_blocks=round(line.blk, 1),
        avg_turnovers=round(line.tov, 1),
        avg_fg_pct=round(line.get("fg_pct"), 4),
        avg_ft_pct=round(line.get("ft_pct"), 4),
        avg_fg3m=round(line.fg3m, 1),
        avg_fga=round(line.fga, 1),
        avg_fta=round(line.fta, 1),
        window_days=window_days,
    )


def _category_scoring(scoring: ResolvedScoring) -> CategoryScoring:
    """The league's categories when it is a category league, else standard 9-cat."""
    if scoring.is_categories and scoring.categories is not None:
        return scoring.categories
    return CategoryScoring([CategoryDef.for_key(k) for k in DEFAULT_CATEGORIES])


async def _opponent_comparison(
    team_id: int,
    your_line: StatLine,
    provider: FantasyProvider,
    scoring: ResolvedScoring,
) -> tuple[Optional[CategoryStrengths], Optional[CategoryComparison]]:
    """This week's opponent's strengths and a per-category comparison against yours.

    Best-effort: a failed or missing matchup (provider outage, bye, no data)
    is logged and yields (None, None) so insights never fail because of it.
    """
    log = get_logger()
    try:
        matchup = await MatchupService.get_matchup_by_team_id(0, team_id)
        if matchup.status != ApiStatus.SUCCESS or not matchup.data:
            log.warning("team_insights_no_matchup", team_id=team_id, message=matchup.message)
            return None, None

        opp_lines = await run_db(
            "team_insights.opponent_lines", _roster_lines,
            matchup.data.opponent_team.roster, provider,
        )
        if not opp_lines:
            return None, None
        opp_line = StatLine.sum(opp_lines)

        cs = _category_scoring(scoring)
        comparison = cs.compare(cs.totals_from_line(your_line), cs.totals_from_line(opp_line))
        return _strengths_from_line(opp_line), comparison_to_schema(comparison)
    except DB_RUNTIME_ERRORS:
        raise
    except Exception as e:
        log.warning("team_insights_opponent_comparison_failed", team_id=team_id, error=str(e))
        return None, None
