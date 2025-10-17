from fastapi import FastAPI, APIRouter
from app.core.middleware import setup_middleware
from app.db.base import init_db, close_db
from app.api.v1.internal import auth, users, teams, lineups, espn, admin

async def lifespan(app: FastAPI):
    # Initialize database
    init_db()
    yield
    # Close database connection
    close_db()

app = FastAPI(title="Court Vision API", version="1.0.0", lifespan=lifespan)

setup_middleware(app)

# API v1 Internal routes
api_v1_internal = APIRouter(prefix="/v1/internal")
api_v1_internal.include_router(auth.router)
api_v1_internal.include_router(users.router)
api_v1_internal.include_router(teams.router)
api_v1_internal.include_router(lineups.router)
api_v1_internal.include_router(espn.router)
api_v1_internal.include_router(admin.router)

app.include_router(api_v1_internal)

@app.get("/")
async def root():
    return {"message": "Hello World"}