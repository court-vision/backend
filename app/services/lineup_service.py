import hashlib
import requests
from app.services.espn_service import EspnService
from app.services.team_service import TeamService
from app.schemas.espn import PlayerResp, TeamDataResp
from app.schemas.lineup import GetLineupsResp, SaveLineupResp, DeleteLineupResp,GenerateLineupResp
from app.schemas.common import ApiStatus
from app.db.models import Lineup, Team
from app.utils.constants import LOCAL_FEATURES_ENDPOINT, NUM_FREE_AGENTS
import json

class LineupService:

    @staticmethod
    async def generate_lineup(user_id: int, team_id: int, threshold: float, week: int):
        try:
            # Get the league info
            league_info = Team.select(Team.league_info).where(Team.user_id == user_id).where(Team.team_id == team_id).get().league_info
            if not league_info:
                return GenerateLineupResp(status=ApiStatus.ERROR, message="League info not found", data=None)
            
            # Get the roster and free agent data
            team_data_resp: TeamDataResp = await EspnService.get_team_data(TeamService.deserialize_league_info(json.loads(league_info)))
            free_agent_data_resp: TeamDataResp = await EspnService.get_free_agents(TeamService.deserialize_league_info(json.loads(league_info)), NUM_FREE_AGENTS)
            if team_data_resp.status != ApiStatus.SUCCESS or free_agent_data_resp.status != ApiStatus.SUCCESS:
                return GenerateLineupResp(status=ApiStatus.ERROR, message="Failed to fetch roster or free agent data", data=None)

            roster_data: list[PlayerResp] = team_data_resp.data
            free_agent_data: list[PlayerResp] = free_agent_data_resp.data
            
            # Call Go server to generate the lineup
            lineup_resp: GenerateLineupResp = await LineupService.generate_lineup_v2(roster_data, free_agent_data, threshold, week)
            
            return lineup_resp
            
        except Exception as e:
            print(f"Error in generate_lineup: {e}")
            return GenerateLineupResp(status=ApiStatus.ERROR, message="Internal server error", data=None)

    @staticmethod
    async def generate_lineup_v2(roster_data: list[PlayerResp], free_agent_data: list[PlayerResp], threshold: float, week: int) -> GenerateLineupResp:
        try:
            # Make HTTP request to Go server to generate the lineup
            response = requests.post(f"{LOCAL_FEATURES_ENDPOINT}/generate-lineup", json={
                "roster_data": [player.model_dump() for player in roster_data],
                "free_agent_data": [player.model_dump() for player in free_agent_data],
                "threshold": threshold,
                "week": week
            })
            if response.status_code != 200:
                return GenerateLineupResp(status=ApiStatus.ERROR, message="Failed to generate lineup", data=None)
            
            return GenerateLineupResp(status=ApiStatus.SUCCESS, message="Lineup generated successfully", data=response.json())

        except Exception as e:
            print(f"Error in generate_lineup_v2: {e}")
            return GenerateLineupResp(status=ApiStatus.ERROR, message="Internal server error", data=None)
    
    @staticmethod
    def serialize_lineup_info(lineup_info) -> str:
        return json.dumps({
            "Timestamp": lineup_info.Timestamp,
            "Improvement": lineup_info.Improvement,
            "Week": lineup_info.Week,
            "Threshold": lineup_info.Threshold,
            "Lineup": [{
                "Day": gene.Day,
                "Additions": [{
                    "Name": player.Name,
                    "AvgPoints": player.AvgPoints,
                    "Team": player.Team
                } for player in gene.Additions],
                "Removals": [{
                    "Name": player.Name,
                    "AvgPoints": player.AvgPoints,
                    "Team": player.Team
                } for player in gene.Removals],
                "Roster": {
                    player: {
                        "Name": gene.Roster[player].Name,
                        "AvgPoints": gene.Roster[player].AvgPoints,
                        "Team": gene.Roster[player].Team
                    } for player in gene.Roster
                }
            } for gene in lineup_info.Lineup]
        })

    @staticmethod
    def generate_lineup_hash(lineup_info) -> str:
        return hashlib.md5(LineupService.serialize_lineup_info(lineup_info).encode('utf-8')).hexdigest()

    @staticmethod
    def deserialize_lineups(lineups: list[tuple]):
        from app.schemas.lineup import LineupInfo, SlimGene, SlimPlayer
        
        return [LineupInfo(
            Id=lineup[0],
            Timestamp=lineup[1]['Timestamp'],
            Improvement=lineup[1]['Improvement'],
            Week=lineup[1]['Week'],
            Threshold=lineup[1]['Threshold'],
            Lineup=[
                SlimGene(
                Day=gene['Day'],
                Additions=[SlimPlayer(**player) for player in gene['Additions']],
                Removals=[SlimPlayer(**player) for player in gene['Removals']],
                Roster={pos: SlimPlayer(**player) for pos, player in gene['Roster'].items()}
            ) for gene in lineup[1]['Lineup']]
        ) for lineup in lineups]

    @staticmethod
    async def get_lineups(user_id: int, team_id: int) -> GetLineupsResp:
        try:
            # Join teams and lineups tables
            lineups_query = (Lineup
                .select(Lineup.lineup_id, Lineup.lineup_info)
                .join(Team, on=(Lineup.team_id == Team.team_id))
                .where((Team.user_id == user_id) & (Team.team_id == team_id)))
            
            lineups = list(lineups_query)

            if not lineups:
                return GetLineupsResp(status=ApiStatus.SUCCESS, message="No lineups found", data=None)
            
            lineup_data = [(lineup.lineup_id, lineup.lineup_info) for lineup in lineups]
        
            return GetLineupsResp(status=ApiStatus.SUCCESS, message="Lineups fetched successfully", data=LineupService.deserialize_lineups(lineup_data))
            
        except Exception as e:
            print(f"Error in get_lineups: {e}")
            return GetLineupsResp(status=ApiStatus.ERROR, message="Internal server error", data=None)

    @staticmethod
    async def save_lineup(user_id: int, selected_team: int, lineup_info) -> SaveLineupResp:
        try:
            # Check if the lineup already exists
            lineup_hash = LineupService.generate_lineup_hash(lineup_info)

            lineup_exists = (Lineup
                .select()
                .join(Team, on=(Lineup.team_id == Team.team_id))
                .where((Team.user_id == user_id) & (Lineup.lineup_hash == lineup_hash))
                .exists())
                
            if lineup_exists:
                return SaveLineupResp(status=ApiStatus.ERROR, message="Lineup already exists", error_code="LINEUP_ALREADY_EXISTS")

            # Save the lineup
            Lineup.create(
                team_id=selected_team,
                lineup_info=LineupService.serialize_lineup_info(lineup_info),
                lineup_hash=lineup_hash
            )

            return SaveLineupResp(status=ApiStatus.SUCCESS, message="Lineup saved successfully")

        except Exception as e:
            print(f"Error in save_lineup: {e}")
            return SaveLineupResp(status=ApiStatus.ERROR, message="Failed to save lineup", error_code="INTERNAL_ERROR")

    @staticmethod
    async def remove_lineup(lineup_id: int) -> DeleteLineupResp:
        try:
            lineup = Lineup.select().where(Lineup.lineup_id == lineup_id).first()
            if not lineup:
                return DeleteLineupResp(status=ApiStatus.ERROR, message="Lineup not found", error_code="LINEUP_NOT_FOUND")
            
            lineup.delete_instance()
            return DeleteLineupResp(status=ApiStatus.SUCCESS, message="Lineup deleted successfully")
            
        except Exception as e:
            print(f"Error in remove_lineup: {e}")
            return DeleteLineupResp(status=ApiStatus.ERROR, message="Failed to delete lineup", error_code="INTERNAL_ERROR")
