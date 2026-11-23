"""
Reading a write back from ESPN until the board shows it.

ESPN answers reads from a copy that can trail its writes: a lineup read made
right after a write may still show the board from before it. From 2026-11-06 to
11-12 in production, 8 of 38 lineup writes and 1 of 5 add / drops read back
that way (`verified=false` in `lineup_write_applied` /
`roster_transaction_applied`). Twice James reloaded the Week desk about 7 s
after one and the move was there: the response had carried the pre-write board,
so the desk showed the old lineup until a reload or its next poll.

So the write chain (`LineupEditorService._write`, `RosterTransactionService._write`)
reads again, a little later each time, while the board doesn't show the write
yet, and only then calls it unconfirmed. A write that shows on the first read
costs nothing extra; the wait is spent only while ESPN is behind.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional, Sequence

from core.logging import get_logger
from schemas.lineup_editor import LineupState

log = get_logger("write_readback")

# Seconds slept before each read after the first: 5 s at most in all. Each read is
# ~0.2-0.5 s, so a write ESPN never shows answers ~6-7 s after it went out.
READBACK_DELAYS_S: tuple[float, ...] = (0.5, 1.0, 1.5, 2.0)


async def read_until_shown(
    read: Callable[[], Awaitable[LineupState]],
    shows: Callable[[LineupState], bool],
    *,
    delays: Optional[Sequence[float]] = None,
) -> tuple[LineupState, bool, int]:
    """Read the board, and read it again after each of `delays` (default
    `READBACK_DELAYS_S`) while `shows(board)` is false.

    Returns the last board read, whether it shows the write, and how many reads
    that took. The first read's failure is the caller's to handle (the write
    chain settles its audit row as applied but unverified); a later read that
    fails ends the wait with the board already in hand.
    """
    board = await read()
    reads = 1
    if shows(board):
        return board, True, reads
    for delay in READBACK_DELAYS_S if delays is None else delays:
        await asyncio.sleep(delay)
        try:
            board = await read()
        except Exception as exc:  # the write went out; one failed re-read is no reason to fail it
            log.warning("write_readback_reread_failed", reads=reads, error=str(exc))
            break
        reads += 1
        if shows(board):
            return board, True, reads
    return board, False, reads
