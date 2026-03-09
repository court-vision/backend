"""
Unit tests for get_rate_limit_key.

Verifies that:
- Requests with X-API-Key use a prefixed key (not the IP)
- Requests without X-API-Key fall back to IP address
- Only the first 11 chars of the API key are used (privacy/memory)
"""

import pytest
from unittest.mock import MagicMock

from core.rate_limit import get_rate_limit_key


def _make_request(headers: dict, client_ip: str = "1.2.3.4") -> MagicMock:
    """Build a minimal mock Request with the given headers and client IP."""
    request = MagicMock()
    request.headers = headers
    request.client.host = client_ip
    return request


@pytest.mark.unit
class TestGetRateLimitKey:

    def test_api_key_header_uses_prefixed_key(self):
        key = "sk_live_abcdefghijklmnopqrstuvwxyz"
        request = _make_request({"X-API-Key": key})
        result = get_rate_limit_key(request)
        assert result == f"api_key:{key[:11]}"

    def test_api_key_prefix_is_11_chars(self):
        key = "sk_live_abc123xyz"
        request = _make_request({"X-API-Key": key})
        result = get_rate_limit_key(request)
        prefix = result.removeprefix("api_key:")
        assert len(prefix) == 11

    def test_no_api_key_falls_back_to_ip(self):
        request = _make_request({}, client_ip="203.0.113.42")
        result = get_rate_limit_key(request)
        assert result == "203.0.113.42"

    def test_api_key_overrides_ip(self):
        key = "sk_live_testkey"
        request = _make_request({"X-API-Key": key}, client_ip="203.0.113.42")
        result = get_rate_limit_key(request)
        assert result.startswith("api_key:")
        assert "203.0.113.42" not in result
