"""Public, provider-labelled preseason snapshots. Missing measurements stay null."""

from datetime import date
from typing import Annotated, Literal

from pydantic import AfterValidator, Field

from core.season import validate_season
from schemas.common import ApiModel, BaseResponse

SeasonKey = Annotated[str, Field(pattern=r"^\d{4}-\d{2}$"), AfterValidator(validate_season)]
MarketSort = Literal["rank", "adp", "auction_value", "auction_value_avg"]
MarketMovementDirection = Literal["up", "down", "both"]


class ESPNMarketPlayer(ApiModel):
    player_id: int = Field(description="NBA player ID")
    espn_id: int | None = None
    name: str
    overall_rank: int | None = Field(None, description="ESPN STANDARD editorial draft rank; lower is better")
    adp: float | None = Field(None, description="Average pick in real ESPN drafts; lower is earlier")
    auction_value: float | None = Field(None, description="ESPN editorial auction value")
    auction_value_avg: float | None = Field(None, description="Average auction price in real ESPN drafts")
    default_position_id: int | None = Field(None, description="ESPN primary position: 1=PG, 2=SG, 3=SF, 4=PF, 5=C")
    eligible_slot_ids: list[int] | None = Field(None, description="ESPN lineup slot IDs: 0=PG, 1=SG, 2=SF, 3=PF, 4=C, 5=G, 6=F, 11=UT; distinct from primary position IDs")
    injury_status: str | None = Field(None, description="ESPN status at snapshot time (ACTIVE, OUT, DAY_TO_DAY, etc.)")


class ESPNMarketData(ApiModel):
    season: str
    source: Literal["espn"] = "espn"
    as_of_date: date | None = None
    players: list[ESPNMarketPlayer] = Field(default_factory=list)
    total: int
    limit: int
    offset: int
    sort_by: MarketSort


class ESPNMarketResp(BaseResponse):
    data: ESPNMarketData | None = None


class ProjectionStats(ApiModel):
    """Per-game projections. Shooting rates use a 0–1 scale."""

    min: float | None = None
    pts: float | None = None
    reb: float | None = None
    ast: float | None = None
    stl: float | None = None
    blk: float | None = None
    tov: float | None = None
    fgm: float | None = None
    fga: float | None = None
    fg3m: float | None = None
    fg3a: float | None = None
    ftm: float | None = None
    fta: float | None = None
    fg_pct: float | None = Field(None, description="Projected FGM / FGA (0–1); null without positive attempts")
    fg3_pct: float | None = Field(None, description="Projected 3PM / 3PA (0–1); null without positive attempts")
    ft_pct: float | None = Field(None, description="Projected FTM / FTA (0–1); null without positive attempts")


class PlayerProjectionData(ApiModel):
    player_id: int = Field(description="NBA player ID")
    espn_id: int | None = None
    name: str
    season: str
    source: Literal["espn"] = "espn"
    as_of_date: date
    projected_gp: int | None = None
    stats: ProjectionStats


class PlayerProjectionResp(BaseResponse):
    data: PlayerProjectionData | None = None


class PlayerProjectionsData(ApiModel):
    season: str
    source: Literal["espn"] = "espn"
    as_of_date: date | None = None
    players: list[PlayerProjectionData] = Field(default_factory=list)
    total: int
    limit: int
    offset: int


class PlayerProjectionsResp(BaseResponse):
    data: PlayerProjectionsData | None = None


class MarketMeasurements(ApiModel):
    overall_rank: int | None = None
    adp: float | None = None
    auction_value: float | None = None
    auction_value_avg: float | None = None


class MarketChanges(ApiModel):
    overall_rank: int | None = Field(
        None,
        description="Positive means the player improved to an earlier ESPN editorial rank",
    )
    adp: float | None = Field(
        None,
        description="Positive means the player improved to an earlier average draft position",
    )
    auction_value: float | None = Field(
        None,
        description="Positive means ESPN's editorial auction value increased",
    )
    auction_value_avg: float | None = Field(
        None,
        description="Positive means the average ESPN auction price increased",
    )


class ESPNMarketMovementPlayer(ApiModel):
    player_id: int = Field(description="NBA player ID")
    espn_id: int | None = None
    name: str
    before: MarketMeasurements | None = Field(
        None,
        description="Measurements in the resolved from snapshot; null when the player was absent",
    )
    after: MarketMeasurements | None = Field(
        None,
        description="Measurements in the resolved to snapshot; null when the player was absent",
    )
    changes: MarketChanges


class ESPNMarketMovementData(ApiModel):
    season: str
    source: Literal["espn"] = "espn"
    from_as_of_date: date | None = None
    to_as_of_date: date | None = None
    metric: MarketSort
    direction: MarketMovementDirection
    players: list[ESPNMarketMovementPlayer] = Field(default_factory=list)
    total: int
    limit: int
    offset: int


class ESPNMarketMovementResp(BaseResponse):
    data: ESPNMarketMovementData | None = None
