"""Authenticated SQLMate operations for a user's saved tables."""

from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request

from api.sqlmate_proxy import passthrough
from core.clerk_auth import get_current_user
from services import sqlmate_client

router = APIRouter(
    prefix="/sqlmate",
    tags=["SQLMate"],
    dependencies=[Depends(get_current_user)],
)


def _authorization(request: Request) -> str:
    # The router dependency has already verified this bearer token.
    return request.headers["Authorization"]


@router.post("/users/save_table", summary="Save a query as a user table")
async def save_table(request: Request, payload: dict[str, Any] = Body(...)):
    return passthrough(await sqlmate_client.proxy_request(
        "POST", "/users/save_table", json=payload, authorization=_authorization(request)
    ))


@router.get("/users/get_tables", summary="List the caller's saved tables")
async def get_tables(request: Request):
    return passthrough(await sqlmate_client.proxy_request(
        "GET", "/users/get_tables", authorization=_authorization(request)
    ))


@router.get("/users/get_table_data", summary="Get one saved table")
async def get_table_data(request: Request, table_name: str = Query(...)):
    return passthrough(await sqlmate_client.proxy_request(
        "GET",
        "/users/get_table_data",
        params={"table_name": table_name},
        authorization=_authorization(request),
    ))


@router.post("/users/delete_table", summary="Delete saved user tables")
async def delete_table(request: Request, payload: dict[str, Any] = Body(...)):
    return passthrough(await sqlmate_client.proxy_request(
        "POST", "/users/delete_table", json=payload, authorization=_authorization(request)
    ))


@router.post("/users/update_table", summary="Update a saved user table")
async def update_table(request: Request, payload: dict[str, Any] = Body(...)):
    return passthrough(await sqlmate_client.proxy_request(
        "POST", "/users/update_table", json=payload, authorization=_authorization(request)
    ))
