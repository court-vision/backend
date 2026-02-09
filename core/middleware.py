from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi import FastAPI, Request
from schemas.common import error_response, ApiStatus

def setup_middleware(app: FastAPI):
    """Setup CORS and global exception handlers"""
    
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
        "https://courtvisionaries.live", # Production
        "https://www.courtvision.dev", # Production
        "https://courtvision.dev", # Production
        "https://sqlmate.courtvision.dev" # SQLMate
    ]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
