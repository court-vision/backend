import hashlib
from typing import Optional
from app.schemas.lineup import GetLineupsResp, SaveLineupResp, DeleteLineupResp
from app.db.models import Lineup, Team
import json

class LineupService:
    
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
    async def get_lineups(user_id: int, selected_team: int) -> GetLineupsResp:
        try:
            # Join teams and lineups tables
            lineups_query = (Lineup
                .select(Lineup.lineup_id, Lineup.lineup_info)
                .join(Team, on=(Lineup.team_id == Team.team_id))
                .where((Team.user_id == user_id) & (Team.team_id == selected_team)))
            
            lineups = list(lineups_query)

            if not lineups:
                return GetLineupsResp(lineups=None, no_lineups=True)
            
            # Convert to the format expected by deserialize_lineups
            lineup_data = [(lineup.lineup_id, lineup.lineup_info) for lineup in lineups]
        
            return GetLineupsResp(lineups=LineupService.deserialize_lineups(lineup_data), no_lineups=False)
            
        except Exception as e:
            print(f"Error in get_lineups: {e}")
            return GetLineupsResp(lineups=None, no_lineups=True)

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
                return SaveLineupResp(success=False, already_exists=True)

            # Save the lineup
            Lineup.create(
                team_id=selected_team,
                lineup_info=LineupService.serialize_lineup_info(lineup_info),
                lineup_hash=lineup_hash
            )

            return SaveLineupResp(success=True, already_exists=False)

        except Exception as e:
            print(f"Error in save_lineup: {e}")
            return SaveLineupResp(success=False, already_exists=False)

    @staticmethod
    async def remove_lineup(lineup_id: int) -> DeleteLineupResp:
        try:
            lineup = Lineup.select().where(Lineup.lineup_id == lineup_id).first()
            if not lineup:
                return DeleteLineupResp(success=False)
            
            lineup.delete_instance()
            return DeleteLineupResp(success=True)
            
        except Exception as e:
            print(f"Error in remove_lineup: {e}")
            return DeleteLineupResp(success=False)
