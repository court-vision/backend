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
"""

from __future__ import annotations

from typing import Mapping, Optional

# Rank type -> the (rank, auction value) column pair it lives in.
RANK_TYPE_FIELDS: dict[str, tuple[str, str]] = {
    "standard": ("overall_rank", "auction_value"),
    "roto": ("roto_rank", "roto_auction_value"),
}

__all__ = ["RANK_TYPE_FIELDS", "market_auction_of", "market_rank_of", "rank_type_for"]


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
