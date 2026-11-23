"""
The provider read behind the live matchup, kept between box score refreshes.

GET /v1/internal/matchups/live/{team_id} answers with two things: the
provider's matchup (a full ESPN or Yahoo league read and a valuation of both
rosters) and tonight's box scores from nba.live_player_stats laid over it. The
box scores are written every 15 seconds; the rosters under them change when
somebody makes a move. A Week desk that reads once per write (core/live_age.py)
would ask the provider four times a minute to learn nothing new.

So a desk that already holds an answer says which one (`reuse_from`: that
answer's `provider_read_ms`), and the provider read behind its next answer may
be one this process made up to HOLD_SECONDS ago. The box scores, the baseline
and the scoring are read fresh every time.

What keeps an old lineup off the screen:

- **No `reuse_from`, no reuse.** A first read always reaches the provider, and
  so does the read a desk makes after a move: the frontend leaves the stamp out
  whenever something asked for the matchup again (`readLiveMatchup`).
- **Never across a write.** A kept read carries the write epoch it began in
  (core/inflight.py: bumped when any provider write starts and when it ends).
  Once the epoch has moved it is not handed back, so a move Dime or the
  auto-lineup sent shows on the next poll's own provider read.
- **Never older than the caller's.** A kept read made before the one the caller
  holds is not handed back. That cannot happen in one process; it is for a
  second replica, whose kept read could predate what the first one served.

Per process, like the write epoch and every other in-process cache here: a
replica keeps its own reads and knows only the writes it sent. The API runs as
one process on one replica today. With more, a move sent through another
replica is not fenced here, and the desk's own re-reads after a move (which
never reuse) are what cover it.

The kept `MatchupResp` is shared between answers, so it is read-only: the live
overlay builds new rows from it and changes nothing on it.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass

from core.inflight import write_epoch
from schemas.matchup import MatchupResp

# How long a provider read may stand in for a new one. A desk on the 15 s beat
# then reads the provider on every other poll: every 30 s, as often as it did
# when every poll read it, while the box scores come twice as often.
HOLD_SECONDS = 25.0
# One entry a team with a Week desk open in the last HOLD_SECONDS.
MAX_TEAMS = 256


@dataclass(frozen=True)
class Kept:
    """One provider read of a team's matchup."""

    user_id: int
    matchup: MatchupResp
    read_ms: int        # wall clock, epoch ms: the stamp the answer carries and the caller hands back
    at: float           # monotonic seconds, for its age
    epoch: int          # the write epoch the read began in


_kept: OrderedDict[int, Kept] = OrderedDict()


def reading() -> int:
    """Call before the provider is read; hand the result to `keep` with what came back."""
    return write_epoch()


def keep(user_id: int, team_id: int, matchup: MatchupResp, epoch: int) -> Kept:
    """Keep a provider read that began in `epoch` (`reading()`), and hand it back stamped."""
    kept = Kept(user_id=user_id, matchup=matchup, read_ms=int(time.time() * 1000), at=time.monotonic(), epoch=epoch)
    _kept[team_id] = kept
    _kept.move_to_end(team_id)
    while len(_kept) > MAX_TEAMS:
        _kept.popitem(last=False)
    return kept


def reusable(user_id: int, team_id: int, reuse_from: int | None) -> Kept | None:
    """The kept read that may answer a caller holding `reuse_from`, if there is one. See the module docstring."""
    if reuse_from is None:
        return None
    kept = _kept.get(team_id)
    if kept is None or kept.user_id != user_id:
        return None
    if kept.epoch != write_epoch() or kept.read_ms < reuse_from or time.monotonic() - kept.at > HOLD_SECONDS:
        del _kept[team_id]
        return None
    return kept


def clear() -> None:
    """Drop every kept read (tests)."""
    _kept.clear()
