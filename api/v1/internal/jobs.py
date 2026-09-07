"""
Service-to-service jobs, authenticated with the shared pipeline bearer token
(the same `PIPELINE_API_TOKEN` cron-runner presents to data-platform). No user
identity: the caller names the user and team, and ownership is re-checked here.
"""

from fastapi import APIRouter, Security

from core.pipeline_auth import verify_pipeline_token
from core.responses import respond
from schemas.lineup_editor import LineupEvaluateReq, LineupEvaluationResp
from services.lineup_editor_service import LineupEditorService

router = APIRouter(prefix="/jobs", tags=["internal jobs"], dependencies=[Security(verify_pipeline_token)])


@router.post("/lineup/evaluate", response_model=LineupEvaluationResp)
async def evaluate_lineup(req: LineupEvaluateReq):
    """Plan today's fill-only moves for one team; apply them when `apply` is set.

    Called by data-platform's lineup-alerts pipeline for every opted-in team.
    Business outcomes are reported in `data.outcome`, never raised.
    """
    return respond(await LineupEditorService.evaluate(req))
