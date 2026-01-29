from fastapi import FastAPI, APIRouter

# Apply NBA API patch early, before any nba_api imports elsewhere
import utils.patches  # noqa: F401 - imported for side effect (patches nba_api)

from core.middleware import setup_middleware
from core.db_middleware import DatabaseMiddleware
from db.base import init_db, close_db
from api.v1.internal import auth, users, teams, lineups, espn, matchups, streamers, pipelines
from api.v1.public import rankings, players

async def lifespan(app: FastAPI):
    # Initialize database
    init_db()
    yield
    # Close database connection
    close_db()

app = FastAPI(title="Court Vision API", version="1.0.0", lifespan=lifespan)

app.add_middleware(DatabaseMiddleware)
setup_middleware(app)

# API v1 Public routes
api_v1_public = APIRouter(prefix="/v1")
api_v1_public.include_router(rankings.router)
api_v1_public.include_router(players.router)

app.include_router(api_v1_public)

# API v1 Internal routes
api_v1_internal = APIRouter(prefix="/v1/internal")
api_v1_internal.include_router(auth.router)
api_v1_internal.include_router(users.router)
api_v1_internal.include_router(teams.router)
api_v1_internal.include_router(lineups.router)
api_v1_internal.include_router(espn.router)
api_v1_internal.include_router(matchups.router)
api_v1_internal.include_router(streamers.router)
api_v1_internal.include_router(pipelines.router)

app.include_router(api_v1_internal)

@app.get("/")
async def root():
    return {"message": "Hello, Court Visionary!"}


# Wake up server
@app.get("/ping")
async def ping():
    return {"message": "Pong!"}