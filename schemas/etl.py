from pydantic import BaseModel
from typing import Optional
from .common import BaseRequest, BaseResponse

# ------------------------------- ETL Models ------------------------------- #

class FPTSPlayer(BaseModel):
    rank: int
    player_id: int
    player_name: str
    total_fpts: float
    avg_fpts: float
    rank_change: int | None = None

#                          ------- Incoming -------                           #

class ETLUpdateFTPSReq(BaseRequest):
    cron_token: str

#                          ------- Outgoing -------                           #

class ETLUpdateFTPSResp(BaseResponse):
    success: bool
    data: list[FPTSPlayer] | None
