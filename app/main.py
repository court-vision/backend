from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from app.routers import data, db
from app.routers.base_models import error_response, ApiStatus
from fastapi import FastAPI, Request
from app.db.base import init_db, close_db

async def lifespan(app: FastAPI):
    # Initialize database
    init_db()
    yield
    # Close database connection
    close_db()

app = FastAPI(lifespan=lifespan)

# Global exception handler for validation errors
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
	print(f"Validation error on {request.url}: {exc.errors()}")
	return JSONResponse(
		status_code=422,
		content=error_response(
			message="Request validation failed",
			status=ApiStatus.VALIDATION_ERROR,
			error_code="VALIDATION_ERROR",
			data={"errors": exc.errors()}
		)
	)

origins = [
	"http://localhost:3000", # Frontend
	"http://localhost:8080", # Features server
	"https://www.courtvisionaries.live", # Production
	"https://www.courtvisionaries.live", # Production
	"https://courtvisionaries.live", # Production
	"https://courtvisionaries.live", # Production
	"https://www.courtvision.dev", # Production
	"https://courtvision.dev" # Production
]

app.add_middleware(
	CORSMiddleware,
	allow_origins=origins,
	allow_credentials=True,
	allow_methods=["*"],
	allow_headers=["*"],
)

app.include_router(data.router, prefix='/data', tags=['data'])
app.include_router(db.router, prefix='/db', tags=['db'])

@app.get("/")
async def root():
		return {"message": "Hello World"}