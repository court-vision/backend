"""
Decoder for ESPN's draft-room INIT frame, and the derived views the sync
service needs from it.

The draft socket sends `INIT <base64>` on every connect: the complete draft
state at that moment — every pick made so far, the pick order, the hard
position caps, the scoring settings and each team's roster. Unlike
`mDraftDetail` (which the read API leaves blank until the draft ends), INIT is
current the instant a client connects, which is what makes it the reconciliation
and late-join path.

Validated to consume every byte of the two fixture payloads under
tests/fixtures/espn_draft_init_*.b64. Stdlib only, no I/O — safe to import from
a unit test without pulling in the app.

Format notes:
  - readDouble/readFloat consume 8/4 bytes; their values are not meaningful in
    this format and are discarded, so scoringValue and the bid multipliers are
    never trustworthy.
  - Integers are signed 32-bit big-endian; -1 is the "unset" sentinel used for
    unlimited caps and empty pick slots.
"""

from __future__ import annotations

import base64
import re
from typing import Optional

UNSET = -1


class Reader:
    """Big-endian stream reader for the INIT wire format."""

    def __init__(self, data: bytes):
        self.b = data
        self.i = 0

    def _num(self, n: int) -> int:
        if self.i + n > len(self.b):
            raise EOFError(f"read past end at byte {self.i} (need {n}, have {len(self.b) - self.i})")
        v = int.from_bytes(self.b[self.i:self.i + n], "big")
        self.i += n
        return v

    def int32(self) -> int:
        v = self._num(4)
        # Two's complement: 0x80000000 is INT32_MIN, so the boundary is inclusive.
        return v - (1 << 32) if v >= (1 << 31) else v

    def short(self) -> int:
        return self._num(2)

    def long(self) -> int:
        return self._num(8)

    def boolean(self) -> bool:
        return self._num(1) == 1

    def double(self) -> None:
        """8 bytes consumed and discarded; the value is not meaningful in this format."""
        self._num(8)

    def utf(self) -> str:
        n = self.short()
        s = self.b[self.i:self.i + n]
        self.i += n
        return s.decode("utf-8", errors="replace")


def _obj(r: Reader, version: int, name: str):
    """Shared prologue. Returns False when the object is absent."""
    if r.int32() != 1:
        return False
    v = r.int32()
    if v != version:
        raise ValueError(f"version {v} not supported by {name} (expected {version})")
    return True


# --------------------------- leaf transcoders --------------------------- #

def break_schedule(r):
    if not _obj(r, 1, "BreakSchedule"):
        return None
    return {"leagueId": r.int32(), "interval": r.int32(), "intervalType": r.int32()}


def autodraft_protection(r):
    if not _obj(r, 1, "AutodraftProtection"):
        return None
    return {"leagueId": r.int32(), "cutoff": r.int32(), "cutoffType": r.int32()}


def scoring_category(r):
    if not _obj(r, 3, "ScoringCategory"):
        return None
    d = {"leagueId": r.int32(), "statId": r.int32()}
    r.double()                       # scoringValue — unreadable by design
    d["isTeamStat"] = r.boolean()
    return d


def scoring_settings(r):
    if not _obj(r, 1, "ScoringSettings"):
        return None
    d = {"leagueId": r.int32(), "scoringType": r.int32(), "categories": []}
    for _ in range(max(0, r.int32())):
        d["categories"].append(scoring_category(r))
    return d


def draft_block(r):
    if not _obj(r, 1, "DraftBlock"):
        return None
    d = {"leagueId": r.int32(), "state": r.int32()}
    d["expirationTime"] = r.long() if r.int32() != 0 else None
    d.update(nominationTeamIndex=r.int32(), upForBidPlayerId=r.int32(),
             highBidTeamId=r.int32(), highBidSlotId=r.int32(), highBidAmount=r.int32())
    return d


def draft_rules(r):
    if not _obj(r, 2, "DraftRules"):
        return None
    d = {"leagueId": r.int32(), "initialPickTime": r.int32(), "minimumPickTime": r.int32(),
         "nominationTime": r.int32(), "selectionTime": r.int32()}
    d["breakSchedule"] = break_schedule(r)
    d["autodraftProtection"] = autodraft_protection(r)
    d.update(pauseTime=r.int32(), nominationDelay=r.int32(),
             minimumBid=r.int32(), maximumBid=r.int32())
    for _ in range(4):               # the four bid multipliers, all doubles
        r.double()
    d["defaultBalance"] = r.int32()
    d["isRosterCompletionRequired"] = r.boolean()
    d.update(benchSlotCategoryId=r.int32(), injurySlotCategoryId=r.int32(),
             invalidSlotCategoryId=r.int32())
    d["isCensorDefault"] = r.boolean()
    d["isChatCaptureWanted"] = r.boolean()
    d["scoringSettings"] = scoring_settings(r)
    d["isFirstTierProtected"] = r.boolean()
    return d


def draft_position(r):
    if not _obj(r, 1, "DraftPosition"):
        return None
    return {"leagueId": r.int32(), "positionId": r.int32(), "positionMax": r.int32()}


def slot_position(r):
    if not _obj(r, 1, "DraftSlotPosition"):
        return None
    return {"leagueId": r.int32(), "slotCategoryId": r.int32(), "positionId": r.int32()}


def draft_slot(r):
    if not _obj(r, 1, "DraftSlot"):
        return None
    d = {"leagueId": r.int32(), "slotId": r.int32(), "slotCategoryId": r.int32(), "positions": []}
    for _ in range(max(0, r.int32())):
        d["positions"].append(slot_position(r))
    return d


def draft_pick(r):
    if not _obj(r, 3, "DraftPick"):
        return None
    return {
        "leagueId": r.int32(), "teamId": r.int32(), "pickNumber": r.int32(),
        "playerId": r.int32(), "slotId": r.int32(), "bidAmount": r.int32(),
        "nominatingTeamId": r.int32(), "isKeeper": r.boolean(),
        "autodraftTypeId": r.int32(), "selectorUserProfileId": r.int32(),
    }


def draft_owner(r):
    if not _obj(r, 1, "DraftOwner"):
        return None
    return {"leagueId": r.int32(), "teamId": r.int32(), "userProfileId": r.int32(),
            "isLM": r.boolean(), "isOnline": r.boolean(), "isCensorEnabled": r.boolean()}


def roster_item(r):
    if not _obj(r, 1, "DraftRosterItem"):
        return None
    return {"leagueId": r.int32(), "teamId": r.int32(), "slotId": r.int32(),
            "playerId": r.int32(), "isKeeper": r.boolean()}


def draft_team(r):
    if not _obj(r, 2, "DraftTeam"):
        return None
    d = {"leagueId": r.int32(), "teamId": r.int32(), "draftPosition": r.int32(),
         "autodraftTypeId": r.int32(), "amountLeft": r.int32(), "owners": [], "roster": []}
    for _ in range(max(0, r.int32())):
        d["owners"].append(draft_owner(r))
    for _ in range(max(0, r.int32())):
        d["roster"].append(roster_item(r))
    return d


def draft_list_player(r):
    if not _obj(r, 1, "DraftListPlayer"):
        return None
    return {"leagueId": r.int32(), "teamId": r.int32(), "playerId": r.int32(),
            "draftValue": r.int32(), "ordinalRank": r.int32()}


def draft_list(r):
    if not _obj(r, 1, "DraftList"):
        return None
    d = {"leagueId": r.int32(), "teamId": r.int32(), "isCustom": r.boolean(), "players": []}
    for _ in range(max(0, r.int32())):
        d["players"].append(draft_list_player(r))
    return d


def nomination_player(r):
    if not _obj(r, 1, "DraftNominationListPlayer"):
        return None
    return {"leagueId": r.int32(), "teamId": r.int32(), "nominationId": r.int32(),
            "playerId": r.int32(), "initialBid": r.int32()}


def nomination_list(r):
    if not _obj(r, 1, "DraftNominationList"):
        return None
    d = {"leagueId": r.int32(), "teamId": r.int32(), "players": []}
    for _ in range(max(0, r.int32())):
        d["players"].append(nomination_player(r))
    return d


def draft_league(r):
    if not _obj(r, 1, "DraftLeague"):
        return None
    d = {"leagueId": r.int32(), "draftType": r.int32(), "universeId": r.int32()}
    d["draftDate"] = r.long() if r.int32() != 0 else None
    d["draftState"] = r.int32()
    d["draftBlock"] = draft_block(r)
    d["draftRules"] = draft_rules(r)
    for key, fn in (("draftPositions", draft_position), ("draftSlots", draft_slot),
                    ("draftPicks", draft_pick), ("draftTeams", draft_team)):
        d[key] = [fn(r) for _ in range(max(0, r.int32()))]
    return d


def decode_init(b64: str) -> dict:
    """Decode an `INIT <base64>` payload into the full draft state."""
    r = Reader(base64.b64decode(b64))
    if not _obj(r, 1, "DraftInit"):
        raise ValueError("INIT payload is absent")
    out = {"leagueId": r.int32(), "teamId": r.int32()}
    out["league"] = draft_league(r)
    out["draftList"] = draft_list(r)
    out["nominationList"] = nomination_list(r)
    out["_bytes_consumed"] = r.i
    out["_bytes_total"] = len(r.b)
    return out

# --------------------------- derived views --------------------------- #

DRAFT_TYPE_AUCTION = 4

# ESPN position ids on the draft snapshot: 1..5 = PG/SG/SF/PF/C. Id 0 is a
# "no position" bucket that always carries max 0 and is not a real cap.
_POSITION_NAMES = {1: "PG", 2: "SG", 3: "SF", 4: "PF", 5: "C"}


def strip_init_prefix(payload: str) -> str:
    """The base64 body of an `INIT <base64>` frame, prefix and whitespace removed."""
    return re.sub(r"\s+", "", re.sub(r"^INIT\s+", "", payload.strip()))


def _league(decoded: dict) -> dict:
    return decoded.get("league") or {}


def all_picks(decoded: dict) -> list[dict]:
    """Every pick slot, made or not, ordered by pick number."""
    picks = [p for p in _league(decoded).get("draftPicks", []) if p]
    return sorted(picks, key=lambda p: p["pickNumber"])


def made_picks(decoded: dict) -> list[dict]:
    """Only the slots that have been drafted (playerId != -1), in pick order."""
    return [p for p in all_picks(decoded) if p["playerId"] != UNSET]


def draft_teams(decoded: dict) -> list[dict]:
    return [t for t in _league(decoded).get("draftTeams", []) if t]


def team_count(decoded: dict) -> int:
    teams = draft_teams(decoded)
    if teams:
        return len(teams)
    # Fallback: the number of distinct teams in the first round of the skeleton.
    picks = all_picks(decoded)
    return len({p["teamId"] for p in picks}) if picks else 0


def pick_order_of(decoded: dict) -> list[int]:
    """Round-1 team ids in order — the session's `pick_order`.

    Read off the pick skeleton (pick numbers 1..n_teams), which is authoritative
    and present even at room-open; falls back to the teams sorted by their draft
    position when the skeleton is somehow absent.
    """
    n = team_count(decoded)
    if n:
        first_round = [p for p in all_picks(decoded) if 1 <= p["pickNumber"] <= n]
        if len(first_round) == n:
            return [p["teamId"] for p in first_round]
    return [t["teamId"] for t in sorted(draft_teams(decoded), key=lambda t: t["draftPosition"])]


def my_slot_of(decoded: dict, pick_order: Optional[list[int]] = None) -> Optional[int]:
    """The connecting user's 1-based seat: the index of their team in the order."""
    order = pick_order if pick_order is not None else pick_order_of(decoded)
    my_team = decoded.get("teamId")
    return order.index(my_team) + 1 if my_team in order else None


def rounds_of(decoded: dict) -> Optional[int]:
    """Draft length in rounds: total slots / team count."""
    n = team_count(decoded)
    if not n:
        return None
    total = len(all_picks(decoded))
    return total // n or None


def draft_type_of(decoded: dict) -> str:
    return "auction" if _league(decoded).get("draftType") == DRAFT_TYPE_AUCTION else "snake"


def espn_front(decoded: dict) -> int:
    """The next pick number to assign: one past the highest made pick, else 1."""
    made = made_picks(decoded)
    return (max(p["pickNumber"] for p in made) + 1) if made else 1


def position_limits_of(decoded: dict) -> dict[str, int]:
    """Hard per-position caps keyed by ESPN position name (PG..C).

    Only real caps: the id-0 "no position" bucket and -1 (unlimited) are dropped.
    """
    caps: dict[str, int] = {}
    for pos in _league(decoded).get("draftPositions", []):
        if not pos:
            continue
        name = _POSITION_NAMES.get(pos.get("positionId"))
        maximum = pos.get("positionMax")
        if name is not None and isinstance(maximum, int) and maximum >= 0:
            caps[name] = maximum
    return caps
