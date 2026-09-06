"""
Resolve fantasy-provider player IDs to NBA (nba_api) player IDs.

Rosters, matchups and free-agent lists are all addressed by the provider's own
player ID, but everything that reads `nba.*` — and every terminal panel —
addresses players by `nba.players.id`. These helpers bridge the two in one
batched query rather than per-player lookups.

ESPN IDs map directly through `nba.players.espn_id`. Yahoo IDs have no column
of their own and share no namespace with ESPN's, so Yahoo resolves by
normalized name instead; matching Yahoo IDs against `espn_id` would silently
return a different player.
"""

from typing import Optional

from db.models.nba.players import Player as PlayerModel
from services.player_service import _normalize_name


def nba_ids_by_espn_id(espn_ids: list[int]) -> dict[int, int]:
    """Map ESPN player ID → NBA player ID for the IDs given."""
    if not espn_ids:
        return {}
    return {
        row.espn_id: row.id
        for row in PlayerModel.select(PlayerModel.id, PlayerModel.espn_id)
        .where(PlayerModel.espn_id.in_(espn_ids))
    }


def nba_ids_by_name(names: list[str]) -> dict[str, int]:
    """
    Map normalized player name → NBA player ID.

    Names shared by more than one player in `nba.players` are dropped rather
    than guessed: an absent ID costs the caller navigation, while a wrong one
    silently points at another player's stat line.

    Callers must key lookups with `_normalize_name`, the same normalization
    the stored-stat lookups use.
    """
    if not names:
        return {}
    resolved: dict[str, Optional[int]] = {}
    for row in (
        PlayerModel.select(PlayerModel.id, PlayerModel.name_normalized)
        .where(PlayerModel.name_normalized.in_(names))
    ):
        # A second sighting of a name makes it ambiguous; blank it out.
        resolved[row.name_normalized] = (
            None if row.name_normalized in resolved else row.id
        )
    return {name: pid for name, pid in resolved.items() if pid is not None}


def normalize(name: str) -> str:
    """Re-exported so callers need not reach into `player_service`."""
    return _normalize_name(name)
