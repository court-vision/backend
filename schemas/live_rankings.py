"""Court Vision's live rankings as a public read: the boards stored every morning.

`nba.daily_rankings` keeps the two standard boards (ESPN default points and
standard 9-cat) the data platform stores each morning of the season, with
ESPN's own rank that day beside them (the Live rankings plan, step 1). These
are the shapes Scout and any other public client read them in: today's board
with how each player moved, and one player's rank since the first stored day.

Only the plain boards are served (`source='cv'`). A candidate board graded in
shadow is stored in the same table under another source and never reaches
these shapes.
"""

from datetime import date

from pydantic import Field

from schemas.common import ApiModel, BaseResponse


class LiveBoardPlace(ApiModel):
    """One player's place on one board this morning."""

    rank: int = Field(description="Rest-of-season rank on this board; 1 is best")
    value: float | None = Field(
        None,
        description="The per-game number the board shows: fantasy points on the points board, the 9-cat index on the categories board",
    )
    move_1d: int | None = Field(
        None,
        description=(
            "Places gained since the previous stored board (`compared.day`); positive is an improvement. "
            "Null when there is no such board or he was not on it."
        ),
    )
    move_7d: int | None = Field(
        None,
        description=(
            "Places gained since the board of a week ago (`compared.week`); positive is an improvement. "
            "Null when there is no such board or he was not on it."
        ),
    )
    espn_rank: int | None = Field(
        None,
        description=(
            "ESPN's own draft rank on `espn_as_of`: STANDARD beside the points board, ROTO beside the categories board. "
            "Null when ESPN does not rank him."
        ),
    )


class LiveRankingsPlayer(ApiModel):
    player_id: int = Field(description="NBA player ID")
    name: str
    team: str | None = Field(None, description="Current NBA team abbreviation; null without a team on record")
    position: str | None = None
    injury_status: str | None = Field(None, description="The availability flag the board was built with; null plays")
    season_gp: int | None = Field(None, description="Games played this season when the board was built")
    games: float | None = Field(None, description="Games he is expected to play from the board's day to the end of the season")
    points: LiveBoardPlace | None = None
    categories: LiveBoardPlace | None = None


class LiveRankingsCompared(ApiModel):
    """The earlier boards the moves are measured against."""

    day: date | None = Field(None, description="The previous stored board, at most three days back; null when there is none")
    week: date | None = Field(None, description="The newest board at least seven days back, at most ten; null when there is none")


class LiveRankingsData(ApiModel):
    season: str
    as_of: date | None = Field(None, description="The fantasy day of the board; null before the first one is stored")
    model: str | None = Field(None, description="The name of the model that built the board")
    espn_as_of: date | None = Field(None, description="The day ESPN's ranks beside the board were read")
    compared: LiveRankingsCompared = Field(default_factory=LiveRankingsCompared)
    players: list[LiveRankingsPlayer] = Field(default_factory=list, description="Every ranked player, in points-board order")


class LiveRankingsResp(BaseResponse):
    data: LiveRankingsData | None = None


class LiveRankDay(ApiModel):
    """One player's ranks on one stored day."""

    as_of: date
    points: int | None = None
    categories: int | None = None
    espn_points: int | None = None
    espn_categories: int | None = None


class EspnRankAge(ApiModel):
    """ESPN's rank for one player and how long it has stood."""

    rank: int
    unchanged_since: date = Field(description="The first stored day of the run of days ESPN has held this rank")
    since_first_stored: bool = Field(
        description="True when the run reaches back to the first stored day, so the rank may be older than `unchanged_since`",
    )


class LivePlayerEspn(ApiModel):
    as_of: date | None = Field(None, description="The day ESPN's ranks were last read")
    points: EspnRankAge | None = None
    categories: EspnRankAge | None = None


class LivePlayerRankData(ApiModel):
    player_id: int
    name: str
    team: str | None = None
    position: str | None = None
    season: str
    as_of: date | None = Field(None, description="The fantasy day of the newest board; null before the first one is stored")
    model: str | None = None
    compared: LiveRankingsCompared = Field(default_factory=LiveRankingsCompared)
    injury_status: str | None = None
    season_gp: int | None = None
    games: float | None = None
    points: LiveBoardPlace | None = Field(None, description="His place on the newest points board; null when he is not on it")
    categories: LiveBoardPlace | None = Field(None, description="His place on the newest categories board; null when he is not on it")
    espn: LivePlayerEspn = Field(default_factory=LivePlayerEspn)
    history: list[LiveRankDay] = Field(
        default_factory=list,
        description="His ranks on every stored day of the season he had one, oldest first",
    )
    board_days: list[date] = Field(
        default_factory=list,
        description=(
            "Every day of the season a Court Vision board was stored, oldest first. A day here that `history` "
            "has no Court Vision rank for is a day he was off the board, not a morning the board is missing."
        ),
    )
    espn_days: list[date] = Field(
        default_factory=list,
        description="Every day of the season ESPN's ranks were stored, oldest first; read beside `history` as `board_days` is",
    )


class LivePlayerRankResp(BaseResponse):
    data: LivePlayerRankData | None = None
