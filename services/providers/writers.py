"""The provider boundary for roster writes.

Every write goes to the private fantasy-writer, which is the only process that
speaks a provider's write protocol. This module owns the envelope each
provider's route takes, so the editor and the transaction service ask a
`RosterWriter` for the write and never build a provider payload themselves.
The audit row, the idempotency key and the verify-by-re-read stay with the
services; the writer client (`fantasy_writer_client`) stays the transport.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, Sequence, runtime_checkable

from schemas.common import FantasyProvider, LeagueInfo
from schemas.lineup_editor import LineupState
from services import fantasy_writer_client
from services.fantasy_writer_client import WriterResult
from services.lineup_planner import Move
from services.providers.capabilities import ProviderCapabilityMissing


@runtime_checkable
class RosterWriter(Protocol):
    provider: FantasyProvider

    async def apply_lineup(
        self, league_info: LeagueInfo, state: LineupState, moves: Sequence[Move], key: str
    ) -> WriterResult: ...

    async def apply_transaction(
        self, league_info: LeagueInfo, state: LineupState, add: Optional[int], drop: Optional[int], key: str
    ) -> WriterResult: ...


def espn_lineup_payload(league_info: LeagueInfo, state: LineupState, moves: Sequence[Move], key: str) -> dict[str, Any]:
    return {
        "season": league_info.year,
        "league_id": league_info.league_id,
        "espn_team_id": state.espn_team_id,
        "member_id": league_info.swid,
        "credentials": {"espn_s2": league_info.espn_s2, "swid": league_info.swid},
        "scoring_period_id": state.scoring_period_id,
        "moves": [{"player_id": m.player_id, "from_slot_id": m.from_slot_id, "to_slot_id": m.to_slot_id} for m in moves],
        "idempotency_key": key,
    }


def espn_transaction_payload(league_info: LeagueInfo, state: LineupState, add: Optional[int], drop: Optional[int],
                             key: str) -> dict[str, Any]:
    """The lineup envelope with `add_player_id` / `drop_player_id` in place of `moves`."""
    payload = espn_lineup_payload(league_info, state, [], key)
    del payload["moves"]
    payload["add_player_id"] = add
    payload["drop_player_id"] = drop
    return payload


class EspnRosterWriter:
    provider = FantasyProvider.ESPN

    async def apply_lineup(self, league_info, state, moves, key):
        # Looked up on the module at call time so a test can stand in for the transport.
        return await fantasy_writer_client.apply_lineup(espn_lineup_payload(league_info, state, moves, key))

    async def apply_transaction(self, league_info, state, add, drop, key):
        return await fantasy_writer_client.apply_transaction(
            espn_transaction_payload(league_info, state, add, drop, key)
        )


class YahooRosterWriter:
    """Not built yet (docs/YAHOO_PARITY_PLAN.md P6); refuses before anything is sent."""

    provider = FantasyProvider.YAHOO

    async def apply_lineup(self, league_info, state, moves, key):
        raise ProviderCapabilityMissing(self.provider, "lineup_changes")

    async def apply_transaction(self, league_info, state, add, drop, key):
        raise ProviderCapabilityMissing(self.provider, "roster_changes")


_WRITERS: dict[FantasyProvider, RosterWriter] = {
    FantasyProvider.ESPN: EspnRosterWriter(),
    FantasyProvider.YAHOO: YahooRosterWriter(),
}


def get_roster_writer(provider: FantasyProvider | str) -> RosterWriter:
    return _WRITERS[FantasyProvider(provider)]
