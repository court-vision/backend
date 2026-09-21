"""The fantasy-provider boundary used by provider-agnostic services.

ESPN and Yahoo expose the same Court Vision capabilities with different call
signatures, data shapes and player-id spaces. Keeping those differences here
prevents every consumer from growing its own provider conditional: a feature
asks the adapter what the provider can do (`capabilities`) and what it
returned, never which provider it is (docs/YAHOO_PARITY_PLAN.md §2).

An operation a provider does not have yet raises `ProviderCapabilityMissing`
(400 PROVIDER_NOT_SUPPORTED). Features that can answer with an empty state
check `capabilities()` first and never see it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

from schemas.common import FantasyProvider, LeagueInfo
from schemas.espn import PlayerResp, TeamDataResp, ValidateLeagueResp
from schemas.lineup_editor import LineupState
from schemas.matchup import MatchupResp
from services.providers.capabilities import (
    ESPN_CAPABILITIES,
    YAHOO_CAPABILITIES,
    ProviderCapabilities,
    ProviderCapabilityMissing,
)
from services.scoring.models import LeagueSettings
from services.scoring.resolver import ResolvedScoring


@runtime_checkable
class FantasyProviderAdapter(Protocol):
    """Operations shared by every supported fantasy provider."""

    provider: FantasyProvider
    # How the provider's players are keyed in our stored stats: by their
    # provider id ("espn_id") or by normalized name ("name"). `player_value_key`
    # returns that key for a player; `nba_ids` and the value lookups use it.
    uses_name_identity: bool
    identity_kind: str

    def capabilities(self, league_info: Optional[LeagueInfo] = None) -> ProviderCapabilities: ...

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

    async def fetch_league_settings(
        self, league_info: LeagueInfo, *, team_id: int | None = None, payload: dict | None = None
    ) -> LeagueSettings: ...

    async def read_lineup(
        self,
        team_id: int,
        league_info: LeagueInfo,
        *,
        fallback_slot_counts: Optional[Mapping[str, int]] = None,
        now: Optional[datetime] = None,
    ) -> LineupState: ...

    async def player_pool_entries(
        self, league_info: LeagueInfo, player_ids: Sequence[int], *, scoring_period_id: int | None = None
    ) -> dict[int, Any]: ...

    def nba_ids(self, players: Sequence[Any]) -> dict[object, int]: ...

    def player_value_keys(self, players: Sequence[PlayerResp]) -> dict[str, object]: ...

    def player_value_key(self, player: PlayerResp) -> object: ...


class EspnAdapter:
    provider = FantasyProvider.ESPN
    uses_name_identity = False
    identity_kind = "espn_id"

    def capabilities(self, league_info: Optional[LeagueInfo] = None) -> ProviderCapabilities:
        return ESPN_CAPABILITIES

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

    async def fetch_league_settings(self, league_info, *, team_id=None, payload=None):
        from services.providers.http import provider_get
        from services.scoring.providers.espn_settings import parse_espn_settings
        from utils.constants import ESPN_FANTASY_ENDPOINT

        if payload is None:
            payload = await provider_get(
                "espn",
                ESPN_FANTASY_ENDPOINT.format(league_info.year, league_info.league_id),
                params={"view": "mSettings"},
                cookies={"espn_s2": league_info.espn_s2, "SWID": league_info.swid},
                expect_key="settings",
            )
        parsed = parse_espn_settings(payload)
        if not parsed.provider_league_id:
            parsed.provider_league_id = str(league_info.league_id)
        if not parsed.season:
            parsed.season = int(league_info.year)
        return parsed

    async def read_lineup(self, team_id, league_info, *, fallback_slot_counts=None, now=None):
        from services.lineup_read_service import LineupReadService

        return await LineupReadService.read(
            team_id, league_info, fallback_slot_counts=fallback_slot_counts, now=now
        )

    async def player_pool_entries(self, league_info, player_ids, *, scoring_period_id=None):
        from services.espn_service import EspnService

        return await EspnService.get_player_pool_entries(
            league_info, list(player_ids), scoring_period_id=scoring_period_id
        )

    def nba_ids(self, players: Sequence[Any]) -> dict[object, int]:
        from services.providers.identity import nba_ids_by_espn_id

        return nba_ids_by_espn_id([player.player_id for player in players])

    def player_value_keys(self, players: Sequence[PlayerResp]) -> dict[str, object]:
        return {"espn_ids": [player.player_id for player in players]}

    def player_value_key(self, player: PlayerResp) -> object:
        return player.player_id


class YahooAdapter:
    provider = FantasyProvider.YAHOO
    uses_name_identity = True
    identity_kind = "name"

    def capabilities(self, league_info: Optional[LeagueInfo] = None) -> ProviderCapabilities:
        return YAHOO_CAPABILITIES

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

    async def fetch_league_settings(self, league_info, *, team_id=None, payload=None):
        from services.scoring.providers.yahoo_settings import fetch_yahoo_league_settings, parse_yahoo_settings
        from services.yahoo_service import YahooService

        token = await YahooService._ensure_valid_token(league_info, team_id)
        league_key = league_info.yahoo_team_key.rsplit(".t.", 1)[0]
        return parse_yahoo_settings(await fetch_yahoo_league_settings(token, league_key), season=int(league_info.year))

    async def read_lineup(self, team_id, league_info, *, fallback_slot_counts=None, now=None):
        raise ProviderCapabilityMissing(self.provider, "lineup_editing")

    async def player_pool_entries(self, league_info, player_ids, *, scoring_period_id=None):
        raise ProviderCapabilityMissing(self.provider, "roster_changes")

    def nba_ids(self, players: Sequence[Any]) -> dict[object, int]:
        from services.providers.identity import nba_ids_by_name

        return nba_ids_by_name([self.player_value_key(player) for player in players])

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
