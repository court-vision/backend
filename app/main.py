from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI
from routers import data, db

app = FastAPI()

origins = [
	"http://localhost:3000", # Frontend
	"http://localhost:8080", # Features server
	"https://www.courtvisionaries.live" # Production
	"https://courtvisionaries.live" # Production
	"https://www.courtvision.dev" # Production
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