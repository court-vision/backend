"""
Centralized Settings Configuration

Uses Pydantic Settings to load configuration from environment variables
with validation and type coercion.
"""

from typing import Optional
from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Database
    database_url: str

    # ESPN Configuration
    espn_year: int = 2026
    espn_league_id: int = 993431466

    # NBA API
    nba_season: str = "2025-26"

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
