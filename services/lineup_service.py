import asyncio
import hashlib
import json
from typing import Literal, Optional

from core.logging import get_logger
from schemas.lineup import LineupInfo, SlimGene, SlimPlayer
from schemas.espn import PlayerResp
from schemas.lineup import GetLineupsResp, SaveLineupResp, DeleteLineupResp, GenerateLineupResp
from schemas.common import ApiStatus, FantasyProvider
from services import features_client
from services.espn_service import EspnService
from services.features_client import FeaturesRejected, FeaturesUnavailable
from services.yahoo_service import YahooService
from services.player_service import PlayerService, _normalize_name
from services.player_value_service import PlayerValueService
from services.team_service import TeamService
from db.models import Lineup, Team
from utils.constants import NUM_FREE_AGENTS

ScoringPreview = Literal["points", "categories"]


class LineupService:

    @staticmethod
    def _owned_team(user_id: int, team_id: int) -> Team:
        """The team row, scoped to its owner. Raises Team.DoesNotExist otherwise."""
        return Team.select().where(Team.user_id == user_id).where(Team.team_id == team_id).get()

    @staticmethod
    async def fetch_roster_and_fas(
        user_id: int, team_id: int, use_recent_stats: bool = False,
        scoring_preview: Optional[ScoringPreview] = None,
    ) -> tuple[list[PlayerResp], list[PlayerResp]]:
        """
        Fetch roster and free agents for a team with optional recent-stats override.
        `scoring_preview` renders the team under that format for this call only
        (rehearsals, what-if views); None uses the team's own stored setting.
        Raises Team.DoesNotExist if the team is not found for this user (implicit ownership check).
        Raises ValueError if the provider fetch fails.
        """
        team = LineupService._owned_team(user_id, team_id)
        league_info = TeamService.deserialize_league_info(json.loads(team.league_info))
        if scoring_preview is not None:
            league_info.scoring_preview = scoring_preview

        if league_info.provider == FantasyProvider.YAHOO:
            team_resp = await YahooService.get_team_data(league_info, 0, team_id)
            fa_resp = await YahooService.get_free_agents(league_info, NUM_FREE_AGENTS, team_id)
        else:
            team_resp = await EspnService.get_team_data(league_info)
            fa_resp = await EspnService.get_free_agents(league_info, NUM_FREE_AGENTS)

        if team_resp.status != ApiStatus.SUCCESS or fa_resp.status != ApiStatus.SUCCESS:
            raise ValueError("Failed to fetch roster or free agent data from provider")

        roster, fas = team_resp.data, fa_resp.data

        if use_recent_stats:
            # Re-value everyone on recent form under the league's scoring (decay-weighted
            # fpts for points leagues, the shortest-window category value for category
            # leagues); players we know nothing about keep the provider's number.
            all_players = roster + fas
            # An in-process preview must win over the team's stored setting, so
            # resolve from league_info (not the team row) when one was given.
            scoring = PlayerValueService.scoring_for(league_info, None if scoring_preview else team_id)
            value_kind = PlayerValueService.value_kind_for(scoring)
            if league_info.provider == FantasyProvider.YAHOO:
                # Yahoo player ids are not ESPN ids: resolve by normalized name instead
                values = await asyncio.to_thread(
                    PlayerValueService.avg_points_for, scoring,
                    names=[(p.name, p.team) for p in all_players], days=14, recent=True,
                )
                keyed = [(p, _normalize_name(p.name)) for p in all_players]
            else:
                values = await asyncio.to_thread(
                    PlayerValueService.avg_points_for, scoring,
                    espn_ids=[p.player_id for p in all_players], days=14, recent=True,
                )
                keyed = [(p, p.player_id) for p in all_players]
            for player, key in keyed:
                valued = values.get(key)
                if valued is not None and valued.value is not None:
                    player.avg_points = valued.value
                    player.value_source = valued.source
                    player.value_kind = value_kind

        return roster, fas

    @staticmethod
    async def generate_lineup(user_id: int, team_id: int, streaming_slots: int, week: int, avg_mode: str = "season"):
        try:
            roster_data, free_agent_data = await LineupService.fetch_roster_and_fas(
                user_id, team_id, use_recent_stats=(avg_mode == "recent")
            )
            return await LineupService.generate_lineup_v2(roster_data, free_agent_data, streaming_slots, week)
        except Team.DoesNotExist:
            return GenerateLineupResp(status=ApiStatus.ERROR, message="Team not found", data=None)
        except ValueError as e:
            return GenerateLineupResp(status=ApiStatus.ERROR, message=str(e), data=None)
        except Exception as e:
            get_logger().exception("generate_lineup_failed", team_id=team_id, week=week, error=str(e))
            return GenerateLineupResp(status=ApiStatus.ERROR, message="Internal server error", data=None)

    @staticmethod
    def build_features_payload(roster_data: list[PlayerResp], free_agent_data: list[PlayerResp],
                               streaming_slots: int, week: int) -> dict:
        """The `POST /generate-lineup` body the features service expects."""
        return {
            "roster_data": [p.model_dump() for p in roster_data],
            "free_agent_data": [p.model_dump() for p in free_agent_data],
            "streaming_slots": streaming_slots,
            "week": week,
        }

    @staticmethod
    async def generate_lineup_v2(roster_data: list[PlayerResp], free_agent_data: list[PlayerResp], streaming_slots: int, week: int) -> GenerateLineupResp:
        """Ask the features service for a streaming plan.

        A request the service rejects (e.g. a week outside its calendar) comes back
        as an ERROR carrying the service's own reason; an unreachable or failing
        service as an ERROR asking the user to retry. Neither is a 500.
        """
        payload = LineupService.build_features_payload(roster_data, free_agent_data, streaming_slots, week)
        try:
            result = await features_client.request_lineup(payload, caller="lineup_service")
            return GenerateLineupResp(status=ApiStatus.SUCCESS, message="Lineup generated successfully", data=result)
        except FeaturesRejected as e:
            return GenerateLineupResp(status=ApiStatus.ERROR, message=e.message, data=None,
                                      error_code="LINEUP_SERVICE_REJECTED")
        except FeaturesUnavailable as e:
            return GenerateLineupResp(status=ApiStatus.ERROR, message=e.message, data=None,
                                      error_code="LINEUP_SERVICE_UNAVAILABLE")
        except Exception as e:
            get_logger().exception("generate_lineup_v2_failed", week=week, error=str(e))
            return GenerateLineupResp(status=ApiStatus.ERROR, message="Internal server error", data=None)

    @staticmethod
    def serialize_lineup_info(lineup_info) -> str:
        return json.dumps({
            "Timestamp": lineup_info.Timestamp,
            "Improvement": lineup_info.Improvement,
            "Week": lineup_info.Week,
            "StreamingSlots": lineup_info.StreamingSlots,
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
        result = []
        for lineup in lineups:
            lineup_id = lineup[0]
            lineup_data = lineup[1] if isinstance(lineup[1], dict) else json.loads(lineup[1])
            result.append(LineupInfo(
                Id=lineup_id,
                Timestamp=lineup_data['Timestamp'],
                Improvement=lineup_data['Improvement'],
                Week=lineup_data['Week'],
                StreamingSlots=lineup_data.get('StreamingSlots', lineup_data.get('Threshold', 0)),
                Lineup=[
                    SlimGene(
                        Day=gene['Day'],
                        Additions=[SlimPlayer(**player) for player in gene['Additions']],
                        Removals=[SlimPlayer(**player) for player in gene['Removals']],
                        Roster={pos: SlimPlayer(**player) for pos, player in gene['Roster'].items()}
                    ) for gene in lineup_data['Lineup']
                ]
            ))
        return result

    @staticmethod
    async def get_lineups(user_id: int, team_id: int) -> GetLineupsResp:
        try:
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
            lineup_hash = LineupService.generate_lineup_hash(lineup_info)

            lineup_exists = (Lineup
                .select()
                .join(Team, on=(Lineup.team_id == Team.team_id))
                .where((Team.user_id == user_id) & (Lineup.lineup_hash == lineup_hash))
                .exists())

            if lineup_exists:
                return SaveLineupResp(status=ApiStatus.ERROR, message="Lineup already exists", error_code="LINEUP_ALREADY_EXISTS")

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
