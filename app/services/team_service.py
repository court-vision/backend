from typing import Optional
from app.schemas.team import TeamGetResp, TeamAddResp, TeamRemoveResp, TeamUpdateResp
from app.schemas.espn import ValidateLeagueResp
from app.db.models import Team
from app.services.espn_service import EspnService
from app.utils.espn_helpers import json_parsing
import json

class TeamService:
    
    @staticmethod
    async def get_teams(user_id: int) -> TeamGetResp:
        try:
            teams_query = Team.select().where(Team.user_id == user_id)
            teams = []
            
            for team in teams_query:
                teams.append({"team_id": team.team_id, "team_info": team.team_info})

            return TeamGetResp(teams=teams)
            
        except Exception as e:
            print(f"Error in get_teams: {e}")
            return TeamGetResp(teams=[])

    @staticmethod
    def serialize_league_info(league_info) -> str:
        return json.dumps({
            "league_id": league_info.league_id,
            "espn_s2": league_info.espn_s2,
            "swid": league_info.swid,
            "team_name": league_info.team_name,
            "league_name": league_info.league_name if league_info.league_name else "N/A",
            "year": league_info.year
        })

    @staticmethod
    async def add_team(user_id: int, team_info) -> TeamAddResp:
        league_info = team_info.league_info
        team_identifier = str(league_info.league_id) + league_info.team_name

        if not EspnService.check_league(league_info).valid:
            return TeamAddResp(team_id=None, already_exists=False)
        
        try:
            team_exists = Team.select().where(
                (Team.user_id == user_id) & (Team.team_identifier == team_identifier)
            ).exists()
            
            if team_exists:
                return TeamAddResp(team_id=None, already_exists=True)
            
            # Create new team
            team = Team.create(
                user_id=user_id,
                team_identifier=team_identifier,
                team_info=TeamService.serialize_league_info(league_info)
            )

            return TeamAddResp(team_id=team.team_id, already_exists=False)
            
        except Exception as e:
            print(f"Error in add_team: {e}")
            return TeamAddResp(team_id=None, already_exists=False)

    @staticmethod
    async def remove_team(user_id: int, team_id: int) -> TeamRemoveResp:
        try:
            Team.delete().where(
                (Team.user_id == user_id) & (Team.team_id == team_id)
            ).execute()
            
            return TeamRemoveResp(success=True)
            
        except Exception as e:
            print(f"Error in remove_team: {e}")
            return TeamRemoveResp(success=False)

    @staticmethod
    async def update_team(user_id: int, team_id: int, team_info) -> TeamUpdateResp:
        if not EspnService.check_league(team_info.league_info).valid:
            return TeamUpdateResp(success=False)

        league_info = team_info.league_info

        try:
            Team.update(team_info=TeamService.serialize_league_info(league_info)).where(
                (Team.user_id == user_id) & (Team.team_id == team_id)
            ).execute()

            return TeamUpdateResp(success=True)
            
        except Exception as e:
            print(f"Error in update_team: {e}")
            return TeamUpdateResp(success=False)

    @staticmethod
    async def view_team(team_id: int):
        try:
            team = Team.select().where(Team.team_id == team_id).first()
            if not team:
                return {"error": "Team not found"}

            # This will be handled by the ESPN service
            return {"team_info": team.team_info}
            
        except Exception as e:
            print(f"Error in view_team: {e}")
            return {"error": "Failed to fetch team data"}
