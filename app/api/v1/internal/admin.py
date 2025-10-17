from fastapi import APIRouter, BackgroundTasks
from app.services.etl_service import ETLService
from app.schemas.etl import ETLUpdateFTPSReq

router = APIRouter(prefix="/admin", tags=["admin"])

@router.post('/etl/start-update-fpts')
async def start_ETL_update_fpts(req: ETLUpdateFTPSReq):
    return await ETLService.start_etl_update_fpts(req.cron_token)

@router.get("/etl/get_fpts_data")
async def get_fpts_data(cron_token: str):
    return await ETLService.get_fpts_data(cron_token)

@router.post('/etl/start-update-rostered')
async def start_ETL_update_rostered(req: ETLUpdateFTPSReq, background_tasks: BackgroundTasks):
    background_tasks.add_task(trigger_ETL_update_rostered, req.cron_token)
    return {"message": "ETL process started"}

# Async trigger
async def trigger_ETL_update_rostered(cron_token: str):
    await ETLService.update_rostered(cron_token)
