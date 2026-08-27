"""
Centralized Settings Configuration

Uses Pydantic Settings to load configuration from environment variables
with validation and type coercion.
"""

from typing import Optional
from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.season import espn_year_for, season_key, validate_season


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Database
    database_url: str

    # Season. Both default to the current season derived from today's date
    # (flips on Aug 1); set NBA_SEASON / ESPN_YEAR to pin them.
    nba_season: str = ""
    espn_year: int = 0

    # ESPN Configuration
    espn_league_id: int = 993431466

    # BALLDONTLIE API (for injury data)
    # Get a free key at https://app.balldontlie.io
    balldontlie_api_key: Optional[SecretStr] = None

    # Resilience
    retry_max_attempts: int = 3
    retry_base_delay: float = 2.0
    retry_max_delay: float = 30.0
    circuit_breaker_threshold: int = 5
    circuit_breaker_timeout: int = 60
    http_timeout: int = 30

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"  # "json" or "console"
    service_name: str = "court-vision-api"

    # Pipeline Auth
    pipeline_api_token: SecretStr

    # Clerk Auth
    clerk_jwks_url: str
    clerk_secret_key: SecretStr
    # JWT issuer to verify against; derived from clerk_jwks_url when unset
    clerk_issuer: Optional[str] = None
    # Frontend origins allowed as the token's `azp` (authorized party).
    # Empty disables the check. Env format is JSON: ["http://localhost:3000"]
    clerk_authorized_parties: list[str] = []

    # Yahoo OAuth Configuration
    yahoo_client_id: Optional[str] = None
    yahoo_client_secret: Optional[SecretStr] = None
    yahoo_redirect_uri: str = "http://localhost:8000/v1/internal/yahoo/callback"
    frontend_url: str = "http://localhost:3000"

    # Resend (email notifications)
    resend_api_key: Optional[SecretStr] = None
    notification_from_email: str = "alerts@courtvision.dev"
    lineup_alert_window_minutes: int = 150  # broad outer gate; must be >= max user-configurable value (150)

    # Post-game pipeline scheduling
    estimated_game_duration_minutes: int = 150  # time added to latest game start to estimate end (~2.5hr)
    post_game_pipeline_window_minutes: int = 60  # window after estimated end to attempt trigger

    # Development mode
    development_mode: bool = False

    # Deployment metadata (Railway injects these at runtime; unset locally)
    railway_git_commit_sha: Optional[str] = None
    railway_environment_name: Optional[str] = None
    railway_service_name: Optional[str] = None

    # Sentry. No DSN -> SDK not initialised (dev, tests).
    sentry_dsn: Optional[SecretStr] = None
    sentry_environment: Optional[str] = None  # defaults to the Railway environment name, else "development"
    sentry_traces_sample_rate: float = 0.0

    # Event-loop watchdog: exit the process (Railway restarts it) when the loop has not
    # serviced a heartbeat for this many seconds. 0 disables (tests, local dev).
    loop_watchdog_stall_s: float = 45.0

    # Live matchup overlay: pick the overlay day from the baseline's stored day
    # watermark (services/matchup_window.py) instead of the old wall-clock rule.
    # Off until ESPN's period-vs-totals ordering is confirmed in preseason; both
    # are computed either way and disagreements are logged as
    # `matchup_window_divergence`.
    live_window_from_watermark: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate log level is a valid Python logging level."""
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper_v = v.upper()
        if upper_v not in valid_levels:
            raise ValueError(f"log_level must be one of {valid_levels}")
        return upper_v

    @field_validator("log_format")
    @classmethod
    def validate_log_format(cls, v: str) -> str:
        """Validate log format is either json or console."""
        lower_v = v.lower()
        if lower_v not in {"json", "console"}:
            raise ValueError("log_format must be 'json' or 'console'")
        return lower_v

    @model_validator(mode="after")
    def derive_season(self) -> "Settings":
        """NBA_SEASON defaults to today's season; ESPN_YEAR to the season's end year."""
        if not self.nba_season:
            self.nba_season = season_key()
        validate_season(self.nba_season)
        if not self.espn_year:
            self.espn_year = espn_year_for(self.nba_season)
        return self

    @model_validator(mode="after")
    def derive_sentry_environment(self) -> "Settings":
        if not self.sentry_environment:
            self.sentry_environment = self.environment
        return self

    @property
    def environment(self) -> str:
        """Deployment environment name: Railway's, else "development"."""
        return self.railway_environment_name or "development"

    @property
    def version(self) -> str:
        """Short git SHA of the deployed build ("dev" outside Railway)."""
        return (self.railway_git_commit_sha or "")[:7] or "dev"

    @model_validator(mode="after")
    def derive_clerk_issuer(self) -> "Settings":
        """Clerk's issuer is the JWKS host, e.g. https://<instance>.clerk.accounts.dev."""
        if not self.clerk_issuer:
            self.clerk_issuer = self.clerk_jwks_url.removesuffix("/.well-known/jwks.json").rstrip("/")
        return self


def get_settings() -> Settings:
    """
    Get application settings.

    This function creates a new Settings instance each time,
    allowing for testing with different configurations.
    For production use, consider caching with functools.lru_cache.
    """
    return Settings()


# Default settings instance for convenience
# Import this for quick access: from core.settings import settings
settings = Settings()
