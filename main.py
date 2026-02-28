from fastapi import FastAPI, APIRouter
from slowapi.errors import RateLimitExceeded

# temp comment for new push

# Apply NBA API patch early, before any nba_api imports elsewhere
import utils.patches  # noqa: F401 - imported for side effect (patches nba_api)

from core.middleware import setup_middleware
from core.db_middleware import DatabaseMiddleware
from core.correlation_middleware import CorrelationMiddleware
from core.logging import setup_logging, get_logger
from core.settings import settings
from core.rate_limit import limiter, rate_limit_exceeded_handler
from db.base import init_db, close_db
from api.v1.internal import auth, users, teams, lineups, espn, yahoo, matchups, streamers, notifications, api_keys
from api.v1.public import rankings, players, games, teams as public_teams, ownership, analytics, schedule, live as live_public, breakout as breakout_public


async def lifespan(app: FastAPI):
    # Setup structured logging first
    setup_logging(
        log_level=settings.log_level,
        json_format=settings.log_format == "json",
        service_name=settings.service_name,
    )
    log = get_logger()
    log.info("application_starting", service=settings.service_name)

    # Initialize database
    init_db()
    log.info("database_initialized")

    yield

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
        {"name": "Analytics", "description": "Advanced analytics (API key required)"},
        {"name": "Streamers", "description": "Streaming player recommendations including breakout candidates"},
    ],
)

# Add rate limiter
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

# Add middlewares (order matters - first added = outermost)
app.add_middleware(CorrelationMiddleware)  # Outermost: adds correlation ID
app.add_middleware(DatabaseMiddleware)
setup_middleware(app)

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
api_v1_public.include_router(breakout_public.router)

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


# Wake up server
@app.get("/ping")
async def ping():
    return {"message": "Pong!"}