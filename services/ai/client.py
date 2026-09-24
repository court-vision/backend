"""The Anthropic client, built on first use so an app without a key still boots."""

from __future__ import annotations

from typing import Optional

from anthropic import AsyncAnthropic

from core.errors import ServiceUnavailableError
from core.settings import settings

_client: Optional[AsyncAnthropic] = None


def get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        if settings.anthropic_api_key is None:
            raise ServiceUnavailableError("AI_DISABLED", "The assistant is turned off")
        # One retry: the request-level deadline (ai_request_timeout_seconds) is
        # the real bound, and a second retry would mostly spend it waiting.
        _client = AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            timeout=60.0,
            max_retries=1,
        )
    return _client
