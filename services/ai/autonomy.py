"""
How much Dime may do on a team (usr.autopilot `mode` and each kind of move's own,
migration 0044; the Dime autopilot plan, step 5). Pure: no reads, no writes.

Three modes, from least to most:

    shadow   a check-in's change is logged as what Dime would have done (step 3)
    suggest  the change becomes a chip the user can send or apply, as in chat
    act      the server sends it itself, within the policy (services/ai/acting.py)

Three kinds of move, which a proposal's kind maps to (`capabilities_of`):

    lineup      slot moves (propose_lineup), and pins (a steer chip's pin, or an
                override at 0, which auto-lineup's fill reads as out)
    streaming   the lanes (set_lane), a lane's pickup cancelled, and the holds that
                steer the lanes' plans (keep, block, override)
    adds_drops  Dime's own adds and drops (propose_move), and a pickup the user
                scheduled, cancelled

Whether the user sets one mode for the whole team or one per kind of move is still
theirs to decide (the plan doc lays the choice out), so the row holds both: `mode`
is the team's, and a kind's own column stands in for it when set. Everything that
asks how much Dime may do asks `mode_for`, so either shape is a change to what the
settings screen writes, not to the executor. A chip that touches two kinds (a steer
chip with a pin and a keep) gets the lesser of their modes.

`DIME_ACTING_ENABLED` off, act reads as suggest everywhere (`effective`): what Dime
would send becomes a chip for the user instead.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

from core.settings import settings

SHADOW, SUGGEST, ACT = "shadow", "suggest", "act"
MODES = (SHADOW, SUGGEST, ACT)
_RANK = {SHADOW: 0, SUGGEST: 1, ACT: 2}

LINEUP, STREAMING, ADDS_DROPS = "lineup", "streaming", "adds_drops"
CAPABILITIES = (LINEUP, STREAMING, ADDS_DROPS)
COLUMNS = {LINEUP: "lineup_mode", STREAMING: "streaming_mode", ADDS_DROPS: "adds_drops_mode"}
LABELS = {LINEUP: "lineup", STREAMING: "streaming", ADDS_DROPS: "adds and drops"}


def acting_available() -> bool:
    """The server lets a team act at all (DIME_ACTING_ENABLED)."""
    return settings.dime_acting_enabled


def own_mode(row: Any, capability: str) -> Optional[str]:
    """A kind of move's own setting on the row, None when it follows the team's."""
    return getattr(row, COLUMNS[capability], None) if row is not None else None


def mode_for(row: Any, capability: str) -> str:
    """The mode the row sets for a kind of move: its own when set, else the team's. No row is shadow."""
    if row is None:
        return SHADOW
    return own_mode(row, capability) or getattr(row, "mode", None) or SHADOW


def effective(row: Any, capability: str) -> str:
    """What Dime may actually do on a kind of move now: `mode_for`, with act read as suggest while
    acting is switched off on the server, and shadow while the team is paused or has no row."""
    if row is None or getattr(row, "paused_at", None) is not None:
        return SHADOW
    mode = mode_for(row, capability)
    if mode == ACT and not acting_available():
        return SUGGEST
    return mode


def least(modes: Iterable[str]) -> str:
    out = ACT
    for m in modes:
        if _RANK[m] < _RANK[out]:
            out = m
    return out


def effective_modes(row: Any) -> dict[str, str]:
    return {c: effective(row, c) for c in CAPABILITIES}


def all_shadow(row: Any) -> bool:
    return all(m == SHADOW for m in effective_modes(row).values())


# ------------------------------- what a chip touches ------------------------------- #


def _hold_capabilities(hold: Mapping[str, Any]) -> set[str]:
    kind = hold.get("kind")
    if kind == "pin":
        return {LINEUP}
    if kind == "override" and (hold.get("share") or 0) <= 0:
        return {LINEUP, STREAMING}       # the lanes plan without him, and the fill reads him as out
    return {STREAMING}


def capabilities_of(row: Any) -> frozenset[str]:
    """The kinds of move a proposal is: what its mode is read from."""
    kind = row.kind
    action = row.action or {}
    if kind == "lineup":
        return frozenset({LINEUP})
    if kind in ("transaction", "scheduled"):
        return frozenset({ADDS_DROPS})
    if kind == "lane":
        return frozenset({STREAMING})
    if kind == "cancel_pickup":
        return frozenset({STREAMING if action.get("lane_id") else ADDS_DROPS})
    if kind == "steer":
        caps: set[str] = set()
        for h in [*(action.get("holds") or []), *(action.get("clear") or [])]:
            caps |= _hold_capabilities(h)
        return frozenset(caps or {STREAMING})
    return frozenset({ADDS_DROPS})


def chip_mode(autopilot: Any, row: Any) -> str:
    """The mode a proposal is handled in: the least of its kinds' effective modes."""
    return least(effective(autopilot, c) for c in capabilities_of(row))


def primary(caps: Iterable[str]) -> str:
    """One kind to file an action under: the first in CAPABILITIES' order."""
    have = set(caps)
    return next(c for c in CAPABILITIES if c in have)


def adds_of(row: Any) -> int:
    """Players the move adds: what the caps count (a transaction's or a scheduled pickup's add)."""
    return 1 if row.kind in ("transaction", "scheduled") and row.add_player_id is not None else 0


# ------------------------------- the settings ------------------------------- #


def view(row: Any) -> dict[str, Any]:
    """The team's settings as the API returns them: the team's mode, each kind's own (None = the
    team's), and what each kind is now (`effective`)."""
    return {
        "mode": getattr(row, "mode", None) or SHADOW if row is not None else SHADOW,
        "own": {c: own_mode(row, c) for c in CAPABILITIES},
        "effective": effective_modes(row),
    }


def words(modes: Mapping[str, str]) -> str:
    """'lineup act, streaming suggest, adds and drops shadow', for the check-in's brief."""
    return ", ".join(f"{LABELS[c]} {modes[c]}" for c in CAPABILITIES)
