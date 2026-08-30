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
    db_pool_max_connections: int = 20
    db_max_in_flight: int = 16
    db_queue_timeout_seconds: float = 2.0

    # CPU-bound request work (category z-scoring, response assembly) runs in its
    # own small pool -- see core/compute.py. GIL-bound, so this is a
    # monopolisation limit rather than a throughput setting.
    cpu_max_in_flight: int = 2
    cpu_queue_timeout_seconds: float = 5.0

    # Rendered-response cache for the public rankings endpoint (core/cache.py).
    # Rankings change once a day, after post-game; the TTL only bounds how long
    # a replica may lag that. 0 disables the cache (kill switch).
    rankings_cache_ttl_seconds: float = 300.0
    # ~350 KB per category body: 32 entries is a ~11 MB ceiling, against a real
    # key space of 8 (2 formats x 4 windows) plus per-caller min_games variants.
    rankings_cache_max_entries: int = 32
    # League-scored rankings are cached per league scoring configuration, not per
    # user, so this bounds distinct league setups rather than sign-ups.
    league_rankings_cache_max_entries: int = 64

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
    provider_queue_timeout_seconds: float = 5.0
    espn_max_in_flight: int = 8
    yahoo_max_in_flight: int = 8
    nba_max_in_flight: int = 4
    features_max_in_flight: int = 4
    # Outbound email is synchronous and low-volume; it gets its own small
    # pool so a burst of alerts can never consume the NBA workers.
    email_max_in_flight: int = 2

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

    # Envelope-encryption keys for stored provider credentials, as
    # "1:<fernet-key>,2:<fernet-key>" (newest last). Empty disables the
    # encrypted store and falls back to the legacy plaintext column -- see
    # core/crypto.py. Not a SecretStr: it is parsed once at import and the
    # wrapper only makes that awkward without hiding anything (Sentry scrubbing
    # already covers keys named like this).
    credential_keys: str = ""

    # Live matchup overlay: pick the overlay day from the baseline's stored day
    # watermark (services/matchup_window.py) instead of the old wall-clock rule.
    # ON since 2026-08-30: the ordering question the opening-night probe was
    # built to answer (does ESPN advance latestScoringPeriod before totalPoints
    # absorbs the day?) stopped mattering when the write side started enforcing
    # the pairing — data-platform's daily_matchup_scores refuses to store a
    # watermark whose totals haven't moved, so a baseline can never claim
    # coverage it lacks. Both rules are still computed on every request and
    # disagreements logged as `matchup_window_divergence`; the probe stays
    # deployed as confirmation. Flip via LIVE_WINDOW_FROM_WATERMARK=false if
    # opening week says otherwise.
    live_window_from_watermark: bool = True

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
    def validate_concurrency_limits(self) -> "Settings":
        limits = {
            "db_pool_max_connections": self.db_pool_max_connections,
            "db_max_in_flight": self.db_max_in_flight,
            "cpu_max_in_flight": self.cpu_max_in_flight,
            "espn_max_in_flight": self.espn_max_in_flight,
            "yahoo_max_in_flight": self.yahoo_max_in_flight,
            "nba_max_in_flight": self.nba_max_in_flight,
            "features_max_in_flight": self.features_max_in_flight,
            "email_max_in_flight": self.email_max_in_flight,
        }
        invalid = [name for name, value in limits.items() if value <= 0]
        if invalid:
            raise ValueError(f"concurrency limits must be positive: {', '.join(invalid)}")
        if self.db_max_in_flight >= self.db_pool_max_connections:
            raise ValueError("db_max_in_flight must be smaller than db_pool_max_connections")
        timeouts = (
            self.db_queue_timeout_seconds,
            self.provider_queue_timeout_seconds,
            self.cpu_queue_timeout_seconds,
        )
        if any(t <= 0 for t in timeouts):
            raise ValueError("concurrency queue timeouts must be positive")
        if self.rankings_cache_ttl_seconds < 0 or self.rankings_cache_max_entries < 0:
            raise ValueError("rankings cache settings must not be negative")
        return self

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
