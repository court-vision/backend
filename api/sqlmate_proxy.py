"""Shared response adapter for the explicit SQLMate proxy routes."""

import httpx
from fastapi import Response


def passthrough(response: httpx.Response) -> Response:
    headers = {}
    content_type = response.headers.get("content-type")
    if content_type:
        headers["Content-Type"] = content_type
    retry_after = response.headers.get("retry-after")
    if retry_after:
        headers["Retry-After"] = retry_after
    return Response(content=response.content, status_code=response.status_code, headers=headers)
