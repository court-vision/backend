"""
ESPN's two draft boards, and reading the one a league actually drafts off.

ESPN ranks the same pool twice in one `kona_player_info` payload: STANDARD is
the points-league board, ROTO the category-league one. They are separate
opinions rather than a rescaling of each other — over ESPN's own top 150 they
disagree by a mean of 28 places, and Damian Lillard is STANDARD 66 / ROTO 310 —
so handing a category room the points board is handing it the wrong board.

Both live on one `nba.draft_market` row. This module is the single place that
says which columns each format reads; the draft board and the recap both go
through it so a draft and its grading can never disagree about which ESPN
opinion they were measured against.

Pure: plain mappings in, scalars out, so the pure recap math can use it too.

It also decides *whose* board a room drafts off. ESPN's published rank is the
board's own ordering in an ESPN room -- a league ESPN runs, a room with no
league at all (its market pool is ESPN's anyway), or a room that follows an
ESPN draft -- and Court Vision's value is the second opinion beside it. A room
whose league lives elsewhere keeps CV's ordering: no other provider's rankings
reach the platform. So does any room while no market snapshot exists, because a
board ordered by nothing would be worse than one ordered by CV.
"""

from __future__ import annotations

from typing import Mapping, Optional

# Rank type -> the (rank, auction value) column pair it lives in.
RANK_TYPE_FIELDS: dict[str, tuple[str, str]] = {
    "standard": ("overall_rank", "auction_value"),
    "roto": ("roto_rank", "roto_auction_value"),
}

__all__ = [
    "RANK_TYPE_FIELDS",
    "market_auction_of",
    "market_ladder",
    "market_rank_of",
    "rank_basis_for",
    "rank_type_for",
]


def rank_type_for(is_categories: bool) -> str:
    """Which of ESPN's two boards a league drafts off."""
    return "roto" if is_categories else "standard"


def market_rank_of(market: Mapping, rank_type: str) -> Optional[int]:
    """ESPN's rank for `rank_type`, falling back to STANDARD.

    The fallback matters for snapshots taken before the pipeline captured ROTO:
    a category room would otherwise see every market column empty and read it as
    "ESPN has no opinion" rather than "we only stored one board".
    """
    rank_field, _ = RANK_TYPE_FIELDS[rank_type]
    rank = market.get(rank_field)
    return rank if rank is not None else market.get("overall_rank")


def market_auction_of(market: Mapping, rank_type: str) -> Optional[float]:
    """ESPN's auction value for `rank_type`, falling back to STANDARD's.

    Unlike the rank, a $0 ROTO value is a real opinion (ESPN prices plenty of
    points-league starters at nothing in categories), so only a missing column
    falls back.
    """
    _, value_field = RANK_TYPE_FIELDS[rank_type]
    value = market.get(value_field)
    return value if value is not None else market.get("auction_value")


def rank_basis_for(league, espn_league_id: Optional[int], has_market: bool) -> tuple[str, str]:
    """Whose board a room drafts off, and why: `("espn" | "cv", reason)`.

    `league` is anything carrying `provider` (a `League` row or the
    `LeagueDetail` a request has already loaded), or None for a room without
    one. The reasons are the wire vocabulary the room shows, so a drafter can
    see *why* a board is ordered the way it is rather than infer it.
    """
    if league is None:
        basis, reason = "espn", "league_less_room"
    else:
        provider = getattr(league, "provider", None)
        provider = getattr(provider, "value", provider)
        if provider == "espn":
            basis, reason = "espn", "espn_league"
        elif espn_league_id is not None:
            basis, reason = "espn", "linked_espn_draft"
        else:
            return "cv", "provider_not_espn"
    if not has_market:
        return "cv", "no_market_snapshot"
    return basis, reason


def market_ladder(market: Mapping[int, Mapping], rank_type: str) -> list[tuple[int, Optional[float]]]:
    """ESPN's board for `rank_type` as a ladder: `(player_id, auction_value)`,
    best first, one rung per ranked player.

    Positional, like the CV ladder the recap prices picks against: the player
    at pick *k* is `ladder[k - 1]`, so a gap or a duplicate in ESPN's numbering
    cannot shift what a slot is worth. Ties on rank break on player id, which
    is arbitrary but stable.
    """
    ranked = [
        (rank, pid, market_auction_of(row, rank_type))
        for pid, row in market.items()
        if (rank := market_rank_of(row, rank_type)) is not None
    ]
    ranked.sort(key=lambda t: (t[0], t[1]))
    return [(pid, value) for _rank, pid, value in ranked]
