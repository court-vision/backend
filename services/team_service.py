from schemas.team import TeamAddReq, TeamGetResp, TeamAddResp, TeamRemoveResp, TeamUpdateResp, TeamResponse, TeamViewResp
from db.models import Team
from services.espn_service import EspnService
from services.yahoo_service import YahooService
from schemas.common import ApiStatus, LeagueInfo, FantasyProvider
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
    def serialize_league_info(league_info: LeagueInfo) -> str:
        """Serialize LeagueInfo to JSON string, preserving provider-specific fields."""
        data = {
            "provider": league_info.provider.value if hasattr(league_info.provider, 'value') else str(league_info.provider),
            "league_id": league_info.league_id,
            "team_name": league_info.team_name,
            "league_name": league_info.league_name if league_info.league_name else "N/A",
            "year": league_info.year,
            # ESPN-specific
            "espn_s2": league_info.espn_s2,
            "swid": league_info.swid,
            # Yahoo-specific
            "yahoo_access_token": league_info.yahoo_access_token,
            "yahoo_refresh_token": league_info.yahoo_refresh_token,
            "yahoo_token_expiry": league_info.yahoo_token_expiry,
            "yahoo_team_key": league_info.yahoo_team_key,
        }
        return json.dumps(data)

    @staticmethod
    def deserialize_league_info(league_info: dict) -> LeagueInfo:
        """Deserialize JSON dict to LeagueInfo, defaulting to ESPN for backward compatibility."""
        # Default to ESPN for existing records without provider field
        provider_str = league_info.get('provider', 'espn')
        try:
            provider = FantasyProvider(provider_str)
        except ValueError:
            provider = FantasyProvider.ESPN

        return LeagueInfo(
            provider=provider,
            league_id=league_info.get('league_id'),
            team_name=league_info.get('team_name'),
            league_name=league_info.get('league_name'),
            year=league_info.get('year'),
            # ESPN-specific
            espn_s2=league_info.get('espn_s2', ''),
            swid=league_info.get('swid', ''),
            # Yahoo-specific
            yahoo_access_token=league_info.get('yahoo_access_token'),
            yahoo_refresh_token=league_info.get('yahoo_refresh_token'),
            yahoo_token_expiry=league_info.get('yahoo_token_expiry'),
            yahoo_team_key=league_info.get('yahoo_team_key'),
        )

    @staticmethod
    async def add_team(user_id: int, league_info: LeagueInfo) -> TeamAddResp:
        team_identifier = str(league_info.league_id) + league_info.team_name

        # Route validation to correct service based on provider
        if league_info.provider == FantasyProvider.YAHOO:
            validation_result = YahooService.check_league(league_info)
        else:
            validation_result = EspnService.check_league(league_info)

        if not validation_result.valid:
            return TeamAddResp(status=ApiStatus.ERROR, message=validation_result.message or "Invalid league information", team_id=None, already_exists=False)
        
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
        # Route validation to correct service based on provider
        if league_info.provider == FantasyProvider.YAHOO:
            validation_result = YahooService.check_league(league_info)
        else:
            validation_result = EspnService.check_league(league_info)

        if not validation_result.valid:
            return TeamUpdateResp(status=ApiStatus.ERROR, message=validation_result.message or "Invalid league information", data=None)

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
