"""
Sentry initialisation and event scrubbing.

`init_sentry(settings)` is called once, before the FastAPI app is created.
Without `SENTRY_DSN` it does nothing (local dev, tests). What gets captured:
unhandled 500s (ASGI integration), `OperationalError` 503s and every
`log.exception(...)` site (via `core.logging.sentry_processor`), plus explicit
`sentry_sdk.capture_exception` calls. INFO+ structlog lines become breadcrumbs.

`scrub_request` is the `before_send` hook: it drops cookies and blanks any key
that looks like a credential anywhere in the event (headers, extra, breadcrumb
data, local variables), and redacts `token=...`-style query fragments inside
strings. The SDK's own `EventScrubber` runs first with an extended denylist;
this hook is the belt to its braces.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from sentry_sdk.scrubber import DEFAULT_DENYLIST, EventScrubber

FILTERED = "[Filtered]"

# Substrings that mark a key as sensitive (case-insensitive).
SENSITIVE_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "cookie",
    "authorization",
    "api_key",
    "api-key",
    "apikey",
    "espn_s2",
    "swid",
    "email",
    "credential",
)

# Extra exact-match keys for the SDK's scrubber (it lower-cases and compares whole keys).
EXTRA_DENYLIST = [
    "espn_s2",
    "swid",
    "cookie",
    "email",
    "x-api-key",
    "x_api_key",
    "yahoo_access_token",
    "yahoo_refresh_token",
    "yahoo_token",
    "refresh_token",
    "access_token",
]

# `espn_s2=...`, `access_token=...` etc. inside URLs / messages.
_QUERY_SECRET_RE = re.compile(
    r"(?i)\b(espn_s2|swid|[a-z_]*token|api[_-]?key|password|secret|client_secret|code)=([^&\s\"'<>]+)"
)

_MAX_DEPTH = 16


def is_sensitive_key(key: Any) -> bool:
    lowered = str(key).lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def scrub_string(value: str) -> str:
    return _QUERY_SECRET_RE.sub(lambda m: f"{m.group(1)}={FILTERED}", value)


def scrub_value(value: Any, depth: int = 0) -> Any:
    """Recursively blank sensitive keys and redact secrets inside strings."""
    if depth > _MAX_DEPTH:
        return value
    if isinstance(value, dict):
        return {
            k: (FILTERED if is_sensitive_key(k) else scrub_value(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [scrub_value(v, depth + 1) for v in value]
    if isinstance(value, tuple):
        return tuple(scrub_value(v, depth + 1) for v in value)
    if isinstance(value, str):
        return scrub_string(value)
    return value


def scrub_request(event: dict, hint: Optional[dict] = None) -> dict:
    """`before_send`: cookies are dropped outright, everything else is scrubbed in place."""
    request = event.get("request")
    if isinstance(request, dict):
        request.pop("cookies", None)
        env = request.get("env")
        if isinstance(env, dict):
            env.pop("REMOTE_ADDR", None)
    return scrub_value(event)


def init_sentry(
    settings: Any,
    *,
    process: Optional[str] = None,
    ignore_errors: Iterable[type] = (),
) -> bool:
    """Initialise the SDK when a DSN is configured. Returns True when it did."""
    dsn = settings.sentry_dsn.get_secret_value() if settings.sentry_dsn else ""
    if not dsn:
        return False

    sentry_sdk.init(
        dsn=dsn,
        environment=settings.sentry_environment,
        release=settings.railway_git_commit_sha or None,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        send_default_pii=False,
        integrations=[StarletteIntegration(), FastApiIntegration()],
        event_scrubber=EventScrubber(denylist=list(DEFAULT_DENYLIST) + EXTRA_DENYLIST, recursive=True),
        ignore_errors=list(ignore_errors),
        before_send=scrub_request,
    )

    scope = sentry_sdk.get_global_scope()
    scope.set_tag("service", settings.service_name)
    if process:
        scope.set_tag("process", process)
    return True
