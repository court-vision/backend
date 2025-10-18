from app.schemas.team import TeamAddReq, TeamGetResp, TeamAddResp, TeamRemoveResp, TeamUpdateResp, TeamResponse, TeamViewResp
from app.db.models import Team
from app.services.espn_service import EspnService
from app.schemas.common import ApiStatus, LeagueInfo
import json

class TeamService:
    
    @staticmethod
    async def get_teams(user_id: int) -> TeamGetResp:
        try:
            teams_query = Team.select().where(Team.user_id == user_id)
            teams: list[TeamResponse] = [TeamResponse(team_id=team.team_id, league_info=TeamService.deserialize_league_info(json.loads(team.league_info))) for team in teams_query]

            return TeamGetResp(status=ApiStatus.SUCCESS, message="Teams fetched successfully", data=teams)
            
        except Exception as e:
            print(f"Error in get_teams: {e}")
            return TeamGetResp(status=ApiStatus.ERROR, message="Internal server error")

    @staticmethod
    def serialize_league_info(league_info: LeagueInfo) -> dict:
        return json.dumps({
            "league_id": league_info.league_id,
            "espn_s2": league_info.espn_s2,
            "swid": league_info.swid,
            "team_name": league_info.team_name,
            "league_name": league_info.league_name if league_info.league_name else "N/A",
            "year": league_info.year
        })
    
    @staticmethod
    def deserialize_league_info(league_info: dict) -> LeagueInfo:
        return LeagueInfo(
            league_id=league_info.get('league_id', None),
            espn_s2=league_info.get('espn_s2', None),
            swid=league_info.get('swid', None),
            team_name=league_info.get('team_name', None),
            league_name=league_info.get('league_name', None),
            year=league_info.get('year', None)
        )

    @staticmethod
    async def add_team(user_id: int, league_info: LeagueInfo) -> TeamAddResp:
        team_identifier = str(league_info.league_id) + league_info.team_name

        if not EspnService.check_league(league_info).valid:
            return TeamAddResp(status=ApiStatus.ERROR, message="Invalid league information", team_id=None, already_exists=False)
        
        try:
            team_exists = Team.select().where(
                (Team.user_id == user_id) & (Team.team_identifier == team_identifier)
            ).exists()
            
            if team_exists:
                return TeamAddResp(status=ApiStatus.SUCCESS, message="Team already exists", team_id=None, already_exists=True)
            
            # Create new team
            team = Team.create(
                user_id=user_id,
                team_identifier=team_identifier,
                league_info=TeamService.serialize_league_info(league_info)
            )

            return TeamAddResp(status=ApiStatus.SUCCESS, message="Team added successfully", team_id=team.team_id, already_exists=False)
            
        except Exception as e:
            print(f"Error in add_team: {e}")
            return TeamAddResp(status=ApiStatus.ERROR, message="Internal server error", team_id=None, already_exists=False)

    @staticmethod
    async def remove_team(user_id: int, team_id: int) -> TeamRemoveResp:
        try:
            team = Team.select().where(Team.user_id == user_id).where(Team.team_id == team_id).first()
            if not team:
                return TeamRemoveResp(status=ApiStatus.ERROR, message="Team not found")

            team.delete_instance()
            return TeamRemoveResp(status=ApiStatus.SUCCESS, message="Team removed successfully", data=team.team_id)   

        except Exception as e:
            print(f"Error in remove_team: {e}")
            return TeamRemoveResp(status=ApiStatus.ERROR, message="Internal server error", data=None)

    @staticmethod
    async def update_team(user_id: int, team_id: int, league_info: LeagueInfo) -> TeamUpdateResp:
        if not EspnService.check_league(league_info).valid:
            return TeamUpdateResp(status=ApiStatus.ERROR, message="Invalid league information", data=None)

        try:
            Team.update(league_info=TeamService.serialize_league_info(league_info)).where(
                (Team.user_id == user_id) & (Team.team_id == team_id)
            ).execute()

            return TeamUpdateResp(status=ApiStatus.SUCCESS, message="Team updated successfully", data=TeamResponse(team_id=team_id, league_info=league_info))
            
        except Exception as e:
            print(f"Error in update_team: {e}")
            return TeamUpdateResp(status=ApiStatus.ERROR, message="Internal server error", data=None)

    @staticmethod
    async def view_team(team_id: int) -> TeamViewResp:
        try:
            team = Team.select().where(Team.team_id == team_id).first()
            if not team:
                return TeamViewResp(status=ApiStatus.ERROR, message="Team not found", data=None)

            # This will be passed to the ESPN service to get the roster data
            return TeamViewResp(status=ApiStatus.SUCCESS, message="Team fetched successfully", data=TeamResponse(team_id=team.team_id, league_info=TeamService.deserialize_league_info(json.loads(team.league_info))))
            
        except Exception as e:
            print(f"Error in view_team: {e}")
            return TeamViewResp(status=ApiStatus.ERROR, message="Internal server error", data=None)
