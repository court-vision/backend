import asyncio

from fastapi import FastAPI, APIRouter
from slowapi.errors import RateLimitExceeded

# Apply NBA API patch early, before any nba_api imports elsewhere
import utils.patches  # noqa: F401 - imported for side effect (patches nba_api)

from core.middleware import setup_middleware
from core.db_middleware import DatabaseMiddleware
from core.correlation_middleware import RequestContextMiddleware
from core.health import health_response
from core.logging import setup_logging, get_logger
from core.settings import settings
from core.telemetry import init_sentry
from core.watchdog import start_loop_watchdog
from core.rate_limit import limiter, rate_limit_exceeded_handler
from db.base import init_db, close_db
from services.schedule_service import assert_calendar_available
from api.v1.internal import auth, users, teams, lineups, espn, yahoo, matchups, streamers, notifications, api_keys
from api.v1.public import rankings, players, games, teams as public_teams, ownership, analytics, schedule, live as live_public, playoffs

# Sentry must be initialised before the app exists so its ASGI integration wraps it.
# No SENTRY_DSN (dev, tests) -> nothing happens.
init_sentry(settings, ignore_errors=[RateLimitExceeded])


async def lifespan(app: FastAPI):
    # Setup structured logging first
    setup_logging(
        log_level=settings.log_level,
        json_format=settings.log_format == "json",
        service_name=settings.service_name,
        version=settings.version,
    )
    log = get_logger()
    log.info("application_starting", service=settings.service_name, version=settings.version,
             environment=settings.environment)

    # Initialize database
    init_db()
    log.info("database_initialized")

    # The season's fantasy calendar must ship with the image (static/schedule{yy}-{yy}.json)
    assert_calendar_available()

    # A blocked event loop hangs every request with nothing logged; exit so Railway restarts us.
    watchdog = start_loop_watchdog(asyncio.get_running_loop(), settings.loop_watchdog_stall_s)

    yield

    if watchdog is not None:
        watchdog.stop()
    # Close database connection
    close_db()
    log.info("application_stopped")


app = FastAPI(
    title="Court Vision API",
    description="Fantasy basketball analytics and insights",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_tags=[
        {"name": "Players", "description": "Player data and statistics"},
        {"name": "Games", "description": "Game schedule and results"},
        {"name": "Teams", "description": "Team information and schedules"},
        {"name": "Rankings", "description": "Fantasy player rankings"},
        {"name": "Ownership", "description": "ESPN roster ownership trends"},
        {"name": "Analytics", "description": "Advanced analytics and streaming recommendations (API key required)"},
    ],
)

# Add rate limiter
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

# Add middlewares (order matters - first added = outermost)
app.add_middleware(RequestContextMiddleware)  # Outermost: correlation id + one http_request log line
app.add_middleware(DatabaseMiddleware)        # Per-request connection on the loop thread
setup_middleware(app)                         # Exception handlers + CORS

# API v1 Public routes
api_v1_public = APIRouter(prefix="/v1")
api_v1_public.include_router(rankings.router)
api_v1_public.include_router(players.router)
api_v1_public.include_router(games.router)
api_v1_public.include_router(public_teams.router)
api_v1_public.include_router(ownership.router)
api_v1_public.include_router(analytics.router)
api_v1_public.include_router(schedule.router)
api_v1_public.include_router(live_public.router)
api_v1_public.include_router(playoffs.router)

app.include_router(api_v1_public)

# API v1 Internal routes
api_v1_internal = APIRouter(prefix="/v1/internal")
api_v1_internal.include_router(auth.router)
api_v1_internal.include_router(users.router)
api_v1_internal.include_router(teams.router)
api_v1_internal.include_router(lineups.router)
api_v1_internal.include_router(espn.router)
api_v1_internal.include_router(yahoo.router)
api_v1_internal.include_router(matchups.router)
api_v1_internal.include_router(streamers.router)
api_v1_internal.include_router(notifications.router)
api_v1_internal.include_router(api_keys.router)

app.include_router(api_v1_internal)

@app.get("/")
async def root():
    return {"message": "Hello, Court Visionary!"}


# Wake up server (liveness only; never touches the database)
@app.get("/ping")
async def ping():
    return {"message": "Pong!"}


# Readiness: database + calendar. 503 "degraded" when the database is unreachable.
@app.get("/health")
async def health():
    return await health_response()
