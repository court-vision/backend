"""
Service for NBA team season stats.
"""

from core.logging import get_logger
from db.models.nba.teams import NBATeam
from db.models.nba.team_stats import TeamStats
from schemas.common import ApiStatus
from schemas.teams import NBATeamStatsResp, NBATeamStatsData


class NBATeamStatsService:
    """Service for retrieving NBA team season statistics."""

    @staticmethod
    async def get_team_stats(team_abbrev: str) -> NBATeamStatsResp:
        """
        Get the latest season stats for an NBA team.

        Args:
            team_abbrev: Team abbreviation (e.g., 'LAL')

        Returns:
            NBATeamStatsResp with season stats data
        """
        log = get_logger()
        team_id = team_abbrev.upper()

        try:
            team = NBATeam.get_or_none(NBATeam.id == team_id)
            if not team:
                return NBATeamStatsResp(
                    status=ApiStatus.NOT_FOUND,
                    message=f"Team '{team_id}' not found",
                    data=None,
                )

            stats = TeamStats.get_latest_for_team(team_id)
            if not stats:
                return NBATeamStatsResp(
                    status=ApiStatus.NOT_FOUND,
                    message=f"No stats found for team '{team_id}'",
                    data=None,
                )

            return NBATeamStatsResp(
                status=ApiStatus.SUCCESS,
                message=f"Stats for {team.name}",
                data=NBATeamStatsData(
                    team=team_id,
                    team_name=team.name,
                    conference=team.conference,
                    division=team.division,
                    as_of_date=stats.as_of_date.isoformat(),
                    season=stats.season,
                    gp=stats.gp,
                    w=stats.w,
                    l=stats.l,
                    w_pct=float(stats.w_pct) if stats.w_pct is not None else None,
                    pts=float(stats.pts) if stats.pts is not None else None,
                    reb=float(stats.reb) if stats.reb is not None else None,
                    ast=float(stats.ast) if stats.ast is not None else None,
                    stl=float(stats.stl) if stats.stl is not None else None,
                    blk=float(stats.blk) if stats.blk is not None else None,
                    tov=float(stats.tov) if stats.tov is not None else None,
                    fg_pct=float(stats.fg_pct) if stats.fg_pct is not None else None,
                    fg3_pct=float(stats.fg3_pct) if stats.fg3_pct is not None else None,
                    ft_pct=float(stats.ft_pct) if stats.ft_pct is not None else None,
                    off_rating=float(stats.off_rating) if stats.off_rating is not None else None,
                    def_rating=float(stats.def_rating) if stats.def_rating is not None else None,
                    net_rating=float(stats.net_rating) if stats.net_rating is not None else None,
                    pace=float(stats.pace) if stats.pace is not None else None,
                    ts_pct=float(stats.ts_pct) if stats.ts_pct is not None else None,
                    efg_pct=float(stats.efg_pct) if stats.efg_pct is not None else None,
                    pie=float(stats.pie) if stats.pie is not None else None,
                ),
            )

        except Exception as e:
            log.error("nba_team_stats_fetch_error", error=str(e), team=team_id)
            return NBATeamStatsResp(
                status=ApiStatus.ERROR,
                message="Failed to fetch team stats",
                data=None,
            )
