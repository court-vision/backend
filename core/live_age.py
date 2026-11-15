"""
How old the live pipeline's newest write is.

The live pipeline (data-platform, triggered by cron-runner every 15 seconds
through the game window) stamps what it writes: `last_updated` on a player's
line, `captured_at` on a game's score snapshot. A screen that polls on a timer
of its own reads a write anywhere from at once to a whole interval after it
landed, so a 15 second poll of a 15 second write shows a stat up to 30 seconds
after the pipeline had it.

A screen that is told how old the newest write was when the server answered
can time its next read for just after the next write is due instead. It needs
no clock of its own to do that: the age is the difference of two server
readings, and the screen only counts on from the moment the answer arrived.

The live answers carry it as `live_age_ms`: the leaders
(GET /v1/live/players/today), today's slate (GET /v1/games/{today}) and the
live matchup (GET /v1/internal/matchups/live/{team_id}). The frontend's reader
is `src/lib/live-beat.ts`.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime


def live_age_ms(written: Iterable[datetime | None], now: datetime | None = None) -> int | None:
    """Milliseconds since the newest of `written`; None when nothing was written.

    The stamps are UTC and naive, as the pipeline writes them, and so is `now`
    (the current time when not given). Never negative: data-platform's clock
    can run a little ahead of this one.
    """
    newest = max((at for at in written if at is not None), default=None)
    if newest is None:
        return None
    elapsed = (now or datetime.utcnow()) - newest
    return max(0, round(elapsed.total_seconds() * 1000))
