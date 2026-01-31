from pydantic import BaseModel, Field
from typing import Optional, Any, Generic, TypeVar
from enum import Enum

# ------------------------------- Base Models ------------------------------- #

class ApiStatus(str, Enum):
    """Standard API response statuses"""
    SUCCESS = "success"
    ERROR = "error"
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

class TeamResponse(BaseModel):
    """Team data response model"""
    team_id: int
    league_info: LeagueInfo

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
