from fastapi import FastAPI
from routers import data, db

app = FastAPI()

app.include_router(data.router, prefix='/data', tags=['data'])
app.include_router(db.router, prefix='/db', tags=['db'])

@app.get("/")
async def root():
		return {"message": "Hello World"}

if __name__ == "__main__":
	import uvicorn
	uvicorn.run(app, host="localhost", port=8000)