"""Fantasy-provider boundary used by provider-agnostic services.

ESPN and Yahoo expose the same Court Vision capabilities with slightly
different call signatures. Keeping those differences here prevents every
consumer from growing its own provider conditional.
"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from schemas.common import FantasyProvider, LeagueInfo
from schemas.espn import PlayerResp, TeamDataResp, ValidateLeagueResp
from schemas.matchup import MatchupResp
from services.scoring.resolver import ResolvedScoring


@runtime_checkable
class FantasyProviderAdapter(Protocol):
    """Operations shared by every supported fantasy provider."""

    provider: FantasyProvider
    uses_name_identity: bool

    async def validate_league(
        self, league_info: LeagueInfo, team_id: int | None = None
    ) -> ValidateLeagueResp: ...

    async def get_team(
        self, league_info: LeagueInfo, *, team_id: int | None = None
    ) -> TeamDataResp: ...

    async def get_free_agents(
        self, league_info: LeagueInfo, count: int, *, team_id: int | None = None
    ) -> TeamDataResp: ...

    async def get_matchup(
        self,
        league_info: LeagueInfo,
        avg_window: str,
        *,
        team_id: int | None = None,
        scoring: ResolvedScoring | None = None,
    ) -> MatchupResp: ...

    def player_value_keys(self, players: Sequence[PlayerResp]) -> dict[str, object]: ...

    def player_value_key(self, player: PlayerResp) -> object: ...


class EspnAdapter:
    provider = FantasyProvider.ESPN
    uses_name_identity = False

    async def validate_league(self, league_info: LeagueInfo, team_id: int | None = None):
        from services.espn_service import EspnService

        return await EspnService.check_league(league_info)

    async def get_team(self, league_info: LeagueInfo, *, team_id: int | None = None):
        from services.espn_service import EspnService

        return await EspnService.get_team_data(league_info, 0)

    async def get_free_agents(
        self, league_info: LeagueInfo, count: int, *, team_id: int | None = None
    ):
        from services.espn_service import EspnService

        return await EspnService.get_free_agents(league_info, count)

    async def get_matchup(
        self,
        league_info: LeagueInfo,
        avg_window: str,
        *,
        team_id: int | None = None,
        scoring: ResolvedScoring | None = None,
    ):
        from services.espn_service import EspnService

        return await EspnService.get_matchup_data(league_info, avg_window, scoring=scoring)

    def player_value_keys(self, players: Sequence[PlayerResp]) -> dict[str, object]:
        return {"espn_ids": [player.player_id for player in players]}

    def player_value_key(self, player: PlayerResp) -> object:
        return player.player_id


class YahooAdapter:
    provider = FantasyProvider.YAHOO
    uses_name_identity = True

    async def validate_league(self, league_info: LeagueInfo, team_id: int | None = None):
        from services.yahoo_service import YahooService

        return await YahooService.check_league(league_info, team_id)

    async def get_team(self, league_info: LeagueInfo, *, team_id: int | None = None):
        from services.yahoo_service import YahooService

        return await YahooService.get_team_data(league_info, 0, team_id)

    async def get_free_agents(
        self, league_info: LeagueInfo, count: int, *, team_id: int | None = None
    ):
        from services.yahoo_service import YahooService

        return await YahooService.get_free_agents(league_info, count, team_id)

    async def get_matchup(
        self,
        league_info: LeagueInfo,
        avg_window: str,
        *,
        team_id: int | None = None,
        scoring: ResolvedScoring | None = None,
    ):
        from services.yahoo_service import YahooService

        return await YahooService.get_matchup_data(
            league_info, avg_window, team_id, scoring=scoring
        )

    def player_value_keys(self, players: Sequence[PlayerResp]) -> dict[str, object]:
        return {"names": [(player.name, player.team) for player in players]}

    def player_value_key(self, player: PlayerResp) -> object:
        from services.player_service import _normalize_name

        return _normalize_name(player.name)


_ADAPTERS: dict[FantasyProvider, FantasyProviderAdapter] = {
    FantasyProvider.ESPN: EspnAdapter(),
    FantasyProvider.YAHOO: YahooAdapter(),
}


def get_provider_adapter(provider: FantasyProvider | str) -> FantasyProviderAdapter:
    """Return the adapter for a provider enum or serialized provider name."""
    return _ADAPTERS[FantasyProvider(provider)]
