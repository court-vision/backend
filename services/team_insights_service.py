from datetime import date, timedelta
from typing import Optional

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
from schemas.common import ApiStatus, FantasyProvider
from services.team_service import TeamService
from services.espn_service import EspnService
from services.yahoo_service import YahooService
from services.player_service import PlayerService, _normalize_name, _compute_avg_stats
from services import schedule_service
from db.models.nba.players import Player
from db.models.nba.player_game_stats import PlayerGameStats
from core.logging import get_logger


# Injury statuses that mean the player is out
_OUT_STATUSES = {"OUT", "O", "IL", "IL+", "SUSPENSION"}
_DTD_STATUSES = {"DAY_TO_DAY", "DTD"}
_GTD_STATUSES = {"GTD", "QUESTIONABLE", "DOUBTFUL"}


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
    async def get_team_insights(team_id: int) -> TeamInsightsResp:
        log = get_logger()
        try:
            # Step 1: Get team info and route to provider for base roster
            team_view_resp = await TeamService.view_team(team_id)
            if team_view_resp.status != ApiStatus.SUCCESS or not team_view_resp.data:
                return TeamInsightsResp(
                    status=ApiStatus.ERROR,
                    message="Failed to fetch team data",
                    data=None,
                )

            league_info = team_view_resp.data.league_info

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
            if league_info.provider == FantasyProvider.YAHOO:
                player_lookups = [(p.name, p.team) for p in base_roster]
                avgs_l7 = PlayerService.get_last_n_day_avg_batch_by_name(player_lookups, days=7)
                avgs_l14 = PlayerService.get_last_n_day_avg_batch_by_name(player_lookups, days=14)
                avgs_l30 = PlayerService.get_last_n_day_avg_batch_by_name(player_lookups, days=30)

                def _get_avg(player: PlayerResp, avgs: dict, key_type: str = "name") -> Optional[float]:
                    normalized = _normalize_name(player.name)
                    return avgs.get(normalized)
            else:
                espn_ids = [p.player_id for p in base_roster]
                avgs_l7 = PlayerService.get_last_n_day_avg_batch(espn_ids, days=7)
                avgs_l14 = PlayerService.get_last_n_day_avg_batch(espn_ids, days=14)
                avgs_l30 = PlayerService.get_last_n_day_avg_batch(espn_ids, days=30)

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
                    schedule=schedule_info,
                    avg_fpts_l7=_get_avg(player, avgs_l7),
                    avg_fpts_l14=_get_avg(player, avgs_l14),
                    avg_fpts_l30=_get_avg(player, avgs_l30),
                ))

            # Step 5: Category strengths (L14 window from our DB)
            category_strengths = _compute_category_strengths(base_roster, league_info.provider)

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
                    schedule_overview=schedule_overview,
                    roster_health=roster_health,
                    projected_week_fpts=projected_week_fpts,
                ),
            )

        except Exception as e:
            log.error("get_team_insights_error", error=str(e), team_id=team_id)
            return TeamInsightsResp(
                status=ApiStatus.ERROR,
                message="Internal server error",
                data=None,
            )


def _compute_category_strengths(
    roster: list[PlayerResp],
    provider: FantasyProvider,
) -> Optional[CategoryStrengths]:
    """Compute team-wide category averages from the last 14 days of game data."""
    try:
        # Resolve ESPN IDs to internal player IDs
        if provider == FantasyProvider.YAHOO:
            # For Yahoo, look up by name
            player_ids = []
            for p in roster:
                player = Player.find_by_name(p.name)
                if player:
                    player_ids.append(player.id)
        else:
            # For ESPN, look up by espn_id
            espn_ids = [p.player_id for p in roster]
            players = list(
                Player.select()
                .where(Player.espn_id.in_(espn_ids))
            )
            player_ids = [p.id for p in players]

        if not player_ids:
            return None

        # Query last 14 days of game stats for all roster players
        cutoff_date = date.today() - timedelta(days=14)
        game_logs = list(
            PlayerGameStats.select()
            .where(
                (PlayerGameStats.player_id.in_(player_ids))
                & (PlayerGameStats.game_date >= cutoff_date)
            )
        )

        if not game_logs:
            return None

        avg_stats = _compute_avg_stats(game_logs)

        return CategoryStrengths(
            avg_points=avg_stats.avg_points,
            avg_rebounds=avg_stats.avg_rebounds,
            avg_assists=avg_stats.avg_assists,
            avg_steals=avg_stats.avg_steals,
            avg_blocks=avg_stats.avg_blocks,
            avg_turnovers=avg_stats.avg_turnovers,
            avg_fg_pct=avg_stats.avg_fg_pct,
            avg_ft_pct=avg_stats.avg_ft_pct,
        )

    except Exception:
        return None
