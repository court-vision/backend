from pydantic import BaseModel, Field
from typing import Literal, Optional, Any, Generic, TypeVar
from enum import Enum

# ------------------------------- Base Models ------------------------------- #

class ApiStatus(str, Enum):
    """Standard API response statuses"""
    SUCCESS = "success"
    ERROR = "error"
    BAD_REQUEST = "bad_request"
    VALIDATION_ERROR = "validation_error"
    AUTHENTICATION_ERROR = "authentication_error"
    AUTHORIZATION_ERROR = "authorization_error"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"

class BaseResponse(BaseModel, Generic[TypeVar('T')]):
    """
    Base response model that all API responses should extend.
    Provides consistent structure across all endpoints.
    """
    status: ApiStatus
    message: str
    data: Optional[Any] = None
    error_code: Optional[str] = None
    timestamp: Optional[str] = None

    class Config:
        use_enum_values = True

class BaseRequest(BaseModel):
    """
    Base request model that all API requests can extend.
    Provides common fields and validation.
    """
    pass

# ------------------------------- Success Response Helpers ------------------------------- #

def success_response(
    message: str = "Operation completed successfully",
    data: Any = None,
    timestamp: Optional[str] = None
) -> dict:
    """Helper function to create a standardized success response"""
    return {
        "status": ApiStatus.SUCCESS.value,
        "message": message,
        "data": data,
        "timestamp": timestamp
    }

def error_response(
    message: str = "An error occurred",
    status: ApiStatus = ApiStatus.ERROR,
    error_code: Optional[str] = None,
    data: Any = None,
    timestamp: Optional[str] = None
) -> dict:
    """Helper function to create a standardized error response"""
    return {
        "status": status.value,
        "message": message,
        "data": data,
        "error_code": error_code,
        "timestamp": timestamp
    }

# ------------------------------- Fantasy Provider ------------------------------- #

class FantasyProvider(str, Enum):
    """Supported fantasy basketball providers."""
    ESPN = "espn"
    YAHOO = "yahoo"

# ------------------------------- Specific Response Models ------------------------------- #

class LeagueInfo(BaseModel):
    # Provider field - defaults to ESPN for backward compatibility
    provider: FantasyProvider = FantasyProvider.ESPN

    # Common fields
    league_id: int = Field(ge=1, description="League ID must be positive")
    team_name: str = Field(min_length=1, description="Team name cannot be empty")
    league_name: str | None = "N/A"
    year: int = Field(ge=2020, le=2030, description="Year must be between 2020 and 2030")

    # ESPN-specific fields
    espn_s2: str | None = ""
    swid: str | None = ""

    # Yahoo-specific fields
    yahoo_access_token: str | None = None
    yahoo_refresh_token: str | None = None
    yahoo_token_expiry: str | None = None  # ISO datetime string
    yahoo_team_key: str | None = None  # e.g., "428.l.12345.t.1"
    # Opaque handle to credentials already stored by the OAuth callback. The
    # browser never sees Yahoo tokens, so this is what it sends when adding a
    # team; the server resolves it to the real tokens. Never persisted.
    yahoo_connection_id: int | None = None

    # View this team as a different scoring format than its league actually uses
    # (e.g. see a points league as 9-cat). Lives with the team, not the league,
    # so a settings sync never touches it. None = the league's real format.
    scoring_preview: Optional[Literal["points", "categories"]] = Field(
        default=None,
        description="Override the rendered scoring format for this team only; None uses the league's synced format",
    )

class AuthResponse(BaseModel):
    """Base authentication response model"""
    access_token: Optional[str] = None
    user_id: Optional[int] = None
    email: Optional[str] = None
    expires_at: Optional[str] = None

class VerificationResponse(BaseModel):
    """Email verification specific response"""
    verification_sent: bool = False
    email: str
    expires_in_seconds: Optional[int] = None
    verification_id: Optional[str] = None

class UserResponse(BaseModel):
    """User data response model"""
    user_id: int
    email: str
    created_at: Optional[str] = None
    last_login: Optional[str] = None

class CategoryDefResp(BaseModel):
    key: str
    label: str
    higher_is_better: bool
    is_rate: bool

class LeagueSummary(BaseModel):
    """Provider-detected league settings (scoring format), embedded in team responses."""
    id: int
    provider: FantasyProvider
    provider_league_id: str
    season: int
    name: Optional[str] = None
    scoring_type: Literal["points", "categories", "roto"]
    category_win_mode: Optional[Literal["each_category", "most_categories"]] = None
    categories: list[CategoryDefResp] = []
    point_weights: dict[str, float] = {}
    settings_synced: bool = False
    settings_synced_at: Optional[str] = None
    # Set when the team's scoring_preview overrides the league's real format above
    scoring_preview: Optional[Literal["points", "categories"]] = None

class LeagueInfoPublic(BaseModel):
    """What a client is allowed to see about a stored team.

    Deliberately a separate model rather than `LeagueInfo` with fields excluded:
    a model that cannot *represent* a credential cannot leak one, however it is
    constructed. `LeagueInfo` stays the internal working object that carries
    secrets to the provider services.

    `yahoo_team_key` is here because it identifies a team, not because it grants
    anything — it is useless without a token.
    """
    provider: FantasyProvider = FantasyProvider.ESPN
    league_id: int
    team_name: str
    league_name: str | None = "N/A"
    year: int
    yahoo_team_key: str | None = None
    scoring_preview: Optional[Literal["points", "categories"]] = None

    # Whether credentials are on file, so the UI can render "stored" without
    # ever receiving the value. Editing a team leaves the fields blank and the
    # server keeps what it has -- see TeamService._merge_stored_credentials.
    has_espn_credentials: bool = False
    has_yahoo_credentials: bool = False

    @classmethod
    def from_league_info(
        cls, league_info: "LeagueInfo", has_credentials: Optional[bool] = None
    ) -> "LeagueInfoPublic":
        """Build the public view of a team's league info.

        `has_credentials` lets the caller state what is on file without
        decrypting it — the stored path knows from the connection link. When
        omitted (the add/update path, where the caller just supplied them) it is
        inferred from the values present.
        """
        espn_creds = bool(league_info.espn_s2 and league_info.swid)
        yahoo_creds = bool(league_info.yahoo_refresh_token)
        if has_credentials is not None:
            provider = getattr(league_info.provider, "value", league_info.provider)
            is_yahoo = str(provider) == "yahoo"
            espn_creds = has_credentials and not is_yahoo
            yahoo_creds = has_credentials and is_yahoo
        return cls(
            provider=league_info.provider,
            league_id=league_info.league_id,
            team_name=league_info.team_name,
            league_name=league_info.league_name,
            year=league_info.year,
            yahoo_team_key=league_info.yahoo_team_key,
            scoring_preview=league_info.scoring_preview,
            has_espn_credentials=espn_creds,
            has_yahoo_credentials=yahoo_creds,
        )


class TeamResponse(BaseModel):
    """Team data response model"""
    team_id: int
    league_info: LeagueInfoPublic
    league: Optional[LeagueSummary] = None   # None until league settings have been synced

class LineupResponse(BaseModel):
    """Lineup data response model"""
    lineup_id: int
    lineup_data: dict
    created_at: Optional[str] = None
    week: Optional[str] = None
    threshold: Optional[float] = None

# ------------------------------- Pagination Models ------------------------------- #

class PaginationParams(BaseModel):
    """Pagination parameters for list endpoints"""
    page: int = Field(default=1, ge=1, description="Page number (1-based)")
    limit: int = Field(default=20, ge=1, le=100, description="Number of items per page")

class PaginatedResponse(BaseModel):
    """Paginated response wrapper"""
    items: list[Any]
    total: int
    page: int
    limit: int
    total_pages: int
    has_next: bool
    has_prev: bool

# ------------------------------- Validation Models ------------------------------- #

class ValidationError(BaseModel):
    """Individual validation error"""
    field: str
    message: str
    value: Optional[Any] = None

class ValidationErrorResponse(BaseModel):
    """Validation error response"""
    errors: list[ValidationError]
    message: str = "Validation failed"
