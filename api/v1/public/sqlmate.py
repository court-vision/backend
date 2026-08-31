"""Public SQLMate reads and anonymous visual-query execution."""

from typing import Any

from fastapi import APIRouter, Body, Request

from api.sqlmate_proxy import passthrough
from core.rate_limit import PUBLIC_RATE_LIMIT, SQLMATE_QUERY_RATE_LIMIT, limiter
from services import sqlmate_client

router = APIRouter(prefix="/sqlmate", tags=["SQLMate"])


@router.get("/schema", summary="Get the query-builder schema")
@limiter.limit(PUBLIC_RATE_LIMIT)
async def get_schema(request: Request):
    upstream = await sqlmate_client.proxy_request(
        "GET",
        "/schema",
        authorization=request.headers.get("Authorization"),
    )
    return passthrough(upstream)


@router.post("/query", summary="Run a visual query")
@limiter.limit(SQLMATE_QUERY_RATE_LIMIT)
async def run_query(request: Request, payload: dict[str, Any] = Body(...)):
    upstream = await sqlmate_client.proxy_request(
        "POST",
        "/query",
        json=payload,
        authorization=request.headers.get("Authorization"),
    )
    return passthrough(upstream)
