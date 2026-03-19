"""
Service for NBA team roster data.
"""

from core.logging import get_logger
from db.models.nba.teams import NBATeam
from db.models.nba.players import Player
from db.models.nba.player_season_stats import PlayerSeasonStats
from db.models.nba.player_injuries import PlayerInjury
from schemas.common import ApiStatus
from schemas.teams import NBATeamRosterResp, NBATeamRosterData, NBATeamRosterPlayer


class NBATeamRosterService:
    """Service for retrieving NBA team roster with per-game stats."""

    @staticmethod
    async def get_team_roster(team_abbrev: str) -> NBATeamRosterResp:
        """
        Get the active roster for an NBA team with per-game averages.

        Args:
            team_abbrev: Team abbreviation (e.g., 'LAL')

        Returns:
            NBATeamRosterResp with roster data
        """
        log = get_logger()
        team_id = team_abbrev.upper()

        try:
            team = NBATeam.get_or_none(NBATeam.id == team_id)
            if not team:
                return NBATeamRosterResp(
                    status=ApiStatus.NOT_FOUND,
                    message=f"Team '{team_id}' not found",
                    data=None,
                )

            # Find the latest as_of_date with data
            latest_date = (
                PlayerSeasonStats.select(PlayerSeasonStats.as_of_date)
                .order_by(PlayerSeasonStats.as_of_date.desc())
                .limit(1)
                .scalar()
            )

            if not latest_date:
                return NBATeamRosterResp(
                    status=ApiStatus.NOT_FOUND,
                    message="No player stats available",
                    data=None,
                )

            # Fetch all players on this team as of the latest date
            season_stats = list(
                PlayerSeasonStats.select(PlayerSeasonStats, Player)
                .join(Player)
                .where(
                    (PlayerSeasonStats.team == team_id)
                    & (PlayerSeasonStats.as_of_date == latest_date)
                    & (PlayerSeasonStats.gp > 0)
                )
                .order_by(PlayerSeasonStats.fpts.desc())
            )

            # Build injury lookup for all players in one pass
            player_ids = [s.player_id for s in season_stats]
            injury_map: dict[int, str] = {}
            if player_ids:
                for injury in PlayerInjury.get_injured_players():
                    if injury.player_id in player_ids:
                        injury_map[injury.player_id] = injury.status

            players = []
            for s in season_stats:
                gp = s.gp or 1  # guard against division by zero
                fg_pct = (s.fgm / s.fga) if s.fga and s.fga > 0 else None
                fg3_pct = (s.fg3m / s.fg3a) if s.fg3a and s.fg3a > 0 else None
                ft_pct = (s.ftm / s.fta) if s.fta and s.fta > 0 else None

                players.append(
                    NBATeamRosterPlayer(
                        player_id=s.player_id,
                        name=s.player.name,
                        position=s.player.position,
                        gp=s.gp,
                        pts=round(s.pts / gp, 1),
                        reb=round(s.reb / gp, 1),
                        ast=round(s.ast / gp, 1),
                        stl=round(s.stl / gp, 1),
                        blk=round(s.blk / gp, 1),
                        tov=round(s.tov / gp, 1),
                        fpts=round(s.fpts / gp, 1),
                        fg_pct=round(fg_pct, 3) if fg_pct is not None else None,
                        fg3_pct=round(fg3_pct, 3) if fg3_pct is not None else None,
                        ft_pct=round(ft_pct, 3) if ft_pct is not None else None,
                        injury_status=injury_map.get(s.player_id),
                    )
                )

            return NBATeamRosterResp(
                status=ApiStatus.SUCCESS,
                message=f"Roster for {team.name}",
                data=NBATeamRosterData(
                    team=team_id,
                    team_name=team.name,
                    players=players,
                    as_of_date=latest_date.isoformat(),
                ),
            )

        except Exception as e:
            log.error("nba_team_roster_fetch_error", error=str(e), team=team_id)
            return NBATeamRosterResp(
                status=ApiStatus.ERROR,
                message="Failed to fetch team roster",
                data=None,
            )
