"""
Response models for the public live endpoints.

These routes returned bare dicts since they were written, which made them the
only public, frontend-consumed endpoints with no OpenAPI schema — exactly the
ones a generated client types as `unknown`. The models below describe what the
handlers already return; the handlers keep building dicts and FastAPI validates
them on the way out.

Field notes:
- `game_status` is NBA's integer code (1=scheduled, 2=in_progress, 3=final);
  `game_status_label` on the scoreboard is its string form.
- `game_clock` is an ISO-8601 duration as NBA's CDN reports it ("PT07M23.00S").
- Times/dates are serialized strings because the handlers build them that way;
  changing them to datetime/date would change the wire format.
"""

from typing import Optional

from pydantic import BaseModel

from schemas.common import ApiModel, BaseResponse


class LivePlayerItem(ApiModel):
    """One player's current box score from nba.live_player_stats."""

    espn_id: Optional[int] = None
    player_id: int
    player_name: str
    game_id: str
    game_date: str
    game_status: int
    period: Optional[int] = None
    game_clock: Optional[str] = None
    fpts: int
    pts: int
    reb: int
    ast: int
    stl: int
    blk: int
    tov: int
    min: int
    fgm: int
    fga: int
    fg3m: int
    fg3a: int
    ftm: int
    fta: int
    last_updated: Optional[str] = None


class LivePlayersData(ApiModel):
    game_date: str
    player_count: int
    players: list[LivePlayerItem]


class LivePlayersResp(BaseResponse):
    """GET /v1/live/players/today"""

    data: Optional[LivePlayersData] = None


class LiveScheduleData(ApiModel):
    """First-tip info for the cron-runner's live loop.

    `first_game_et` / `wake_at_et` are ISO datetimes with offset; both are None
    when there are no games or start times have not been loaded yet.
    """

    has_games: bool
    game_date: str
    first_game_et: Optional[str] = None
    wake_at_et: Optional[str] = None


class LiveScheduleResp(BaseResponse):
    """GET /v1/live/schedule/today"""

    data: Optional[LiveScheduleData] = None


class ScoreboardGameItem(ApiModel):
    game_id: str
    game_status: int
    game_status_label: str  # scheduled | in_progress | final | unknown
    period: Optional[int] = None
    game_clock: Optional[str] = None


class ScoreboardData(ApiModel):
    game_date: str
    game_count: int
    games: list[ScoreboardGameItem]


class ScoreboardResp(BaseResponse):
    """GET /v1/live/scoreboard"""

    data: Optional[ScoreboardData] = None
