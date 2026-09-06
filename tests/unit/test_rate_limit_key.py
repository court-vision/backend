"""Only an authenticated identity can replace the caller's IP quota."""

import pytest
from starlette.requests import Request

from core.rate_limit import get_rate_limit_key

pytestmark = pytest.mark.unit


def request(key=None):
    return Request({
        "type": "http", "client": ("203.0.113.42", 1234),
        "headers": [(b"x-api-key", key.encode())] if key else [],
    })


@pytest.mark.parametrize("key", [None, "cv_fake", "arbitrary-other-key"])
def test_unverified_headers_cannot_change_the_ip_bucket(key):
    assert get_rate_limit_key(request(key)) == "203.0.113.42"


def test_authenticated_key_has_an_independent_bucket():
    req = request("cv_secret")
    req.state.api_key_identity = "verified-digest"
    assert get_rate_limit_key(req) == "api_key:verified-digest"
