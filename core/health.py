"""
Health endpoint.

    GET /health -> 200 {"status": "ok", ...} | 503 {"status": "degraded", ...}

Body: `service`, `version` (git SHA[:7], "dev" locally), `environment`,
`uptime_s`, `checks` = {name: {"ok": bool, "latency_ms"?, "error"?, ...}}.
`database` gates the status; `calendar` is informational. Extra checks a
caller passes (the data-platform public port's `private_server`) gate too.
`/ping` stays a static liveness probe; Railway / Better Stack / CI use
`/health` (`.status == "ok"`).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Iterable, Optional

from fastapi.responses import JSONResponse

from core.logging import get_logger
from core.settings import settings
from db.base import db
from db.base import run_db
from services.schedule_service import get_max_week

DB_TIMEOUT_S = settings.db_queue_timeout_seconds

_STARTED_AT = time.monotonic()
_DEGRADED_LOG_INTERVAL_S = 60.0
_last_degraded_log_at = 0.0


def _probe_database() -> float:
    """`SELECT 1` inside the caller's DB-worker connection; returns latency in ms."""
    started = time.perf_counter()
    db.execute_sql("SELECT 1").fetchone()
    return (time.perf_counter() - started) * 1000


async def database_check(timeout: float = DB_TIMEOUT_S) -> dict[str, Any]:
    try:
        latency_ms = await asyncio.wait_for(run_db("health.database", _probe_database), timeout)
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"timeout after {timeout:g}s"}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}
    return {"ok": True, "latency_ms": round(latency_ms, 1)}


def calendar_check() -> dict[str, Any]:
    """The season's fantasy calendar shipped with the image (cached after first load)."""
    try:
        weeks = get_max_week()
    except Exception as exc:
        return {"ok": False, "season": settings.nba_season, "error": type(exc).__name__}
    return {"ok": weeks > 0, "season": settings.nba_season, "weeks": weeks}


async def build_health(
    extra_checks: Optional[dict[str, dict[str, Any]]] = None,
    *,
    gating: Iterable[str] = ("database",),
) -> dict[str, Any]:
    checks: dict[str, dict[str, Any]] = {
        "database": await database_check(),
        "calendar": calendar_check(),
    }
    if extra_checks:
        checks.update(extra_checks)

    gating_names = set(gating) | set((extra_checks or {}).keys())
    ok = all(bool(checks[name].get("ok")) for name in gating_names if name in checks)

    return {
        "status": "ok" if ok else "degraded",
        "service": settings.service_name,
        "version": settings.version,
        "environment": settings.environment,
        "uptime_s": round(time.monotonic() - _STARTED_AT),
        "checks": checks,
    }


def _log_degraded(payload: dict[str, Any]) -> None:
    """One `health_degraded` line per minute, however often the pollers ask."""
    global _last_degraded_log_at
    now = time.monotonic()
    if now - _last_degraded_log_at < _DEGRADED_LOG_INTERVAL_S:
        return
    _last_degraded_log_at = now
    failing = {name: check for name, check in payload["checks"].items() if not check.get("ok")}
    get_logger("health").error("health_degraded", checks=failing, version=payload["version"])


async def health_response(extra_checks: Optional[dict[str, dict[str, Any]]] = None) -> JSONResponse:
    payload = await build_health(extra_checks)
    status_code = 200 if payload["status"] == "ok" else 503
    if status_code != 200:
        _log_degraded(payload)
    return JSONResponse(status_code=status_code, content=payload, headers={"Cache-Control": "no-store"})
