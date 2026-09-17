"""One lineup vocabulary for every provider.

The board, the planner, the editor, the writer payload and the frontend all
speak ESPN's slot ids (0 PG … 11 UT, 12 BE, 13 IR). Yahoo speaks strings
(PG, SG, G, SF, PF, F, C, Util, BN, IL, IL+, NA). Yahoo's slots are a subset of
ESPN's -- no combo slots -- so the canonical ids are ESPN's and each provider
maps in and out here. Injury statuses normalize to the sets the planner reads
(`OUT_STATUSES`, `IR_STATUSES` in lineup_planner), which already spell ESPN's
and Yahoo's out/IR statuses; the rest of Yahoo's vocabulary maps onto them.

Pure: constants and functions only, so the planner and the readers can both
import it.
"""

from __future__ import annotations

from typing import Optional

from services.lineup_planner import (  # noqa: F401  (re-exported as the canonical sets)
    ACTIVE_SLOT_IDS,
    BENCH_SLOT_ID,
    IR_SLOT_ID,
    IR_STATUSES,
    OUT_STATUSES,
)

NA_SLOT_ID = 14   # Yahoo's not-active list; ESPN's 14 is unused, the planner never touches it

# Canonical slot id -> name, the names the API has always rendered.
SLOT_NAMES: dict[int, str] = {
    0: "PG", 1: "SG", 2: "SF", 3: "PF", 4: "C", 5: "G", 6: "F",
    7: "SG/SF", 8: "G/F", 9: "PF/C", 10: "F/C",
    11: "UT", 12: "BE", 13: "IR", 14: "NA",
}
SLOT_IDS: dict[str, int] = {name: slot_id for slot_id, name in SLOT_NAMES.items()}

# Yahoo's slot names, as its roster and settings payloads spell them.
YAHOO_SLOT_IDS: dict[str, int] = {
    "PG": 0, "SG": 1, "SF": 2, "PF": 3, "C": 4, "G": 5, "F": 6,
    "Util": 11, "BN": 12, "IL": 13, "IL+": 13, "NA": 14,
}
# Canonical id -> the name a Yahoo write uses. IL+ is a second injured list
# some leagues add; a player on it keeps that name (see yahoo_slot_name).
YAHOO_SLOT_NAMES: dict[int, str] = {
    0: "PG", 1: "SG", 2: "SF", 3: "PF", 4: "C", 5: "G", 6: "F",
    11: "Util", 12: "BN", 13: "IL", 14: "NA",
}

# Yahoo's `status` values -> the statuses the planner's sets are written in.
YAHOO_INJURY_STATUSES: dict[str, str] = {
    "O": "OUT", "INJ": "OUT", "NA": "OUT", "IL": "IL", "IL+": "IL+",
    "SUSP": "SUSPENSION", "GTD": "GTD", "DTD": "DTD", "D": "DTD",
    "Q": "QUESTIONABLE", "P": "PROBABLE",
}


def slot_name(slot_id: int) -> str:
    return SLOT_NAMES.get(slot_id, str(slot_id))


def yahoo_slot_id(name: str) -> Optional[int]:
    """Yahoo's slot name -> canonical id; None for a name Yahoo never sends."""
    return YAHOO_SLOT_IDS.get(name)


def yahoo_slot_name(slot_id: int, *, current: Optional[str] = None) -> str:
    """Canonical id -> the name Yahoo expects. A player already on IL+ stays
    on IL+ when the canonical id (13) round-trips; everyone else gets IL."""
    if slot_id == IR_SLOT_ID and current == "IL+":
        return "IL+"
    return YAHOO_SLOT_NAMES.get(slot_id, SLOT_NAMES.get(slot_id, str(slot_id)))


def normalize_injury_status(provider: str, status: Optional[str]) -> Optional[str]:
    """The provider's status in the planner's vocabulary; None for healthy / unknown."""
    if not status:
        return None
    value = str(status).strip()
    if provider == "yahoo":
        return YAHOO_INJURY_STATUSES.get(value, value.upper())
    upper = value.upper()
    return None if upper == "ACTIVE" else upper
