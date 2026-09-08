"""Public schemas for the player dimension and profile snapshot."""

from datetime import date, datetime

from pydantic import Field

from schemas.common import ApiModel, BaseResponse


class PlayerSearchItem(ApiModel):
    """Minimal dimension-backed player identity for search results."""

    id: int = Field(description="NBA player ID")
    espn_id: int | None = Field(None, description="ESPN player ID, when mapped")
    name: str
    position: str | None = None
    team: str | None = Field(
        None,
        description="Team abbreviation from the player-profile snapshot; not a current-roster guarantee",
    )
    player_updated_at: datetime = Field(description="UTC timestamp of the player identity row")
    profile_updated_at: datetime | None = Field(
        None,
        description="UTC timestamp of the joined profile snapshot, or null when no profile is stored",
    )


class PlayerSearchData(ApiModel):
    query: str
    players: list[PlayerSearchItem] = Field(default_factory=list)
    total: int
    limit: int
    offset: int


class PlayerSearchResp(BaseResponse):
    """Response for GET /v1/players/search."""

    data: PlayerSearchData | None = None


class PlayerProfileDetails(ApiModel):
    """The optional ``nba.player_profiles`` row for a player identity."""

    first_name: str | None = None
    last_name: str | None = None
    birthdate: date | None = None
    height: str | None = Field(None, description='NBA height text, for example "6-11"')
    height_inches: int | None = None
    weight: int | None = Field(None, description="Weight in pounds")
    position: str | None = None
    jersey_number: str | None = None
    team: str | None = Field(
        None,
        description="Team abbreviation at profile refresh time; not a current-roster guarantee",
    )
    draft_year: int | None = None
    draft_round: int | None = None
    draft_number: int | None = Field(None, description="Overall draft pick")
    season_exp: int | None = None
    country: str | None = None
    school: str | None = None
    from_year: int | None = None
    to_year: int | None = None
    updated_at: datetime = Field(description="UTC timestamp of this profile snapshot")


class PlayerProfileData(ApiModel):
    """Player identity plus its optional profile snapshot."""

    id: int = Field(description="NBA player ID")
    espn_id: int | None = Field(None, description="ESPN player ID, when mapped")
    name: str
    position: str | None = Field(None, description="Position from the player dimension")
    created_at: datetime = Field(description="UTC timestamp when the player identity was created")
    updated_at: datetime = Field(description="UTC timestamp when the player identity was last updated")
    profile: PlayerProfileDetails | None = Field(
        None,
        description="Biographical/profile snapshot; null until the profile pipeline has supplied one",
    )


class PlayerProfileResp(BaseResponse):
    """Response for GET /v1/players/{player_id}/profile."""

    data: PlayerProfileData | None = None
