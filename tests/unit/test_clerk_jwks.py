"""
Clerk's JWKS cache (core/clerk_auth.py).

The keys used to sit behind a bare lru_cache, so after every deploy each of the
first Week open's 9 parallel requests fetched them itself (about 330 ms each in
prod). Now concurrent callers wait for one fetch and share its outcome, failure
included, a key rotation refetches once however many callers notice it, and a
failed fetch never throws away keys that work. No network: the HTTP session's
`get` is replaced.
"""

import json
import threading
import time

import jwt
import pytest
import requests
from cryptography.hazmat.primitives.asymmetric import rsa

from core import clerk_auth
from core.errors import AuthenticationError, ServiceUnavailableError


def _jwk(kid: str) -> dict:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = kid
    return jwk


@pytest.fixture(scope="module")
def jwks_v1():
    return {"keys": [_jwk("kid-1")]}


@pytest.fixture(scope="module")
def jwks_v2(jwks_v1):
    return {"keys": [*jwks_v1["keys"], _jwk("kid-2")]}


class FakeJwksEndpoint:
    """Stands in for the HTTP session: serves `bodies` in turn, slowly, and counts fetches."""

    def __init__(self, *bodies, delay=0.0):
        self.bodies = list(bodies)
        self.delay = delay
        self.fetches = 0
        self.lock = threading.Lock()

    def get(self, url, timeout=None, **kwargs):
        with self.lock:
            self.fetches += 1
            body = self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]
        time.sleep(self.delay)
        if isinstance(body, Exception):
            raise body
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(body).encode()
        return response


@pytest.fixture
def endpoint(monkeypatch):
    def install(*bodies, delay=0.0):
        fake = FakeJwksEndpoint(*bodies, delay=delay)
        monkeypatch.setattr(clerk_auth._clerk_http, "get", fake.get)
        return fake
    clerk_auth.clear_jwks_cache()
    yield install
    clerk_auth.clear_jwks_cache()


def _token(kid: str) -> str:
    # Only the header matters to the key lookup; the signature is checked later.
    return jwt.encode({"sub": "user_abc"}, "not-the-key-" * 4, algorithm="HS256", headers={"kid": kid})


def _outcomes(fn, n=9):
    """Run `fn` on n threads at once: each one's (result or exception, seconds taken)."""
    barrier = threading.Barrier(n)
    outcomes = []

    def run():
        barrier.wait()
        started = time.perf_counter()
        try:
            outcome = fn()
        except Exception as exc:
            outcome = exc
        outcomes.append((outcome, time.perf_counter() - started))

    threads = [threading.Thread(target=run) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(outcomes) == n
    return outcomes


def _in_parallel(fn, n=9):
    results = [outcome for outcome, _ in _outcomes(fn, n)]
    errors = [r for r in results if isinstance(r, Exception)]
    assert not errors, errors
    return results


@pytest.mark.unit
def test_concurrent_first_callers_make_one_fetch(endpoint, jwks_v1):
    fake = endpoint(jwks_v1, delay=0.05)
    keys = _in_parallel(lambda: clerk_auth.get_public_key_for_token(_token("kid-1")))
    assert fake.fetches == 1
    assert len(keys) == 9


@pytest.mark.unit
def test_cached_keys_are_reused(endpoint, jwks_v1):
    fake = endpoint(jwks_v1)
    for _ in range(5):
        clerk_auth.get_public_key_for_token(_token("kid-1"))
    assert fake.fetches == 1


@pytest.mark.unit
def test_a_rotated_key_is_fetched_once_however_many_callers_see_it(endpoint, jwks_v1, jwks_v2):
    fake = endpoint(jwks_v1, jwks_v2, delay=0.05)
    clerk_auth.get_public_key_for_token(_token("kid-1"))
    assert fake.fetches == 1

    keys = _in_parallel(lambda: clerk_auth.get_public_key_for_token(_token("kid-2")))
    assert fake.fetches == 2
    assert len(keys) == 9


@pytest.mark.unit
def test_an_unknown_key_is_refused_after_one_refetch(endpoint, jwks_v1):
    fake = endpoint(jwks_v1)
    with pytest.raises(AuthenticationError) as exc:
        clerk_auth.get_public_key_for_token(_token("kid-nope"))
    assert exc.value.error_code == "INVALID_TOKEN"
    assert fake.fetches == 2  # the first load, then the one rotation retry


@pytest.mark.unit
def test_a_failed_refetch_keeps_the_keys_that_work(endpoint, jwks_v1):
    fake = endpoint(jwks_v1, requests.ConnectionError("clerk down"))
    clerk_auth.get_public_key_for_token(_token("kid-1"))

    with pytest.raises(ServiceUnavailableError):
        clerk_auth.get_public_key_for_token(_token("kid-nope"))

    clerk_auth.get_public_key_for_token(_token("kid-1"))  # still served from the cache
    assert fake.fetches == 2


@pytest.mark.unit
def test_a_failed_first_fetch_is_503_and_is_not_cached(endpoint, jwks_v1):
    fake = endpoint(requests.ConnectionError("clerk down"), jwks_v1)
    with pytest.raises(ServiceUnavailableError) as exc:
        clerk_auth.get_public_key_for_token(_token("kid-1"))
    assert exc.value.error_code == "AUTH_UNAVAILABLE"

    clerk_auth.get_public_key_for_token(_token("kid-1"))
    assert fake.fetches == 2


@pytest.mark.unit
def test_callers_queued_behind_a_failed_fetch_share_its_failure(endpoint, jwks_v1):
    """Cold cache, Clerk failing slowly: one fetch, and nobody waits for more than it.

    Before, each queued caller made its own fetch after the one ahead of it had
    failed, so the k-th waited k fetches (k x 20 s with the real timeouts).
    """
    delay = 0.3
    fake = endpoint(requests.ConnectionError("clerk down"), delay=delay)

    outcomes = _outcomes(lambda: clerk_auth.get_public_key_for_token(_token("kid-1")))

    assert fake.fetches == 1
    assert all(isinstance(outcome, ServiceUnavailableError) for outcome, _ in outcomes)
    assert {outcome.error_code for outcome, _ in outcomes} == {"AUTH_UNAVAILABLE"}
    assert max(seconds for _, seconds in outcomes) < delay * 1.8


@pytest.mark.unit
def test_after_a_failure_the_next_caller_fetches_again(endpoint, jwks_v1):
    fake = endpoint(requests.ConnectionError("clerk down"), jwks_v1, delay=0.05)
    outcomes = _outcomes(lambda: clerk_auth.get_public_key_for_token(_token("kid-1")), n=4)
    assert all(isinstance(outcome, ServiceUnavailableError) for outcome, _ in outcomes)

    keys = _in_parallel(lambda: clerk_auth.get_public_key_for_token(_token("kid-1")), n=4)
    assert len(keys) == 4
    assert fake.fetches == 2   # the shared failure, then one fetch that worked


@pytest.mark.unit
def test_a_failed_rotation_refetch_is_shared_and_keeps_the_keys(endpoint, jwks_v1):
    fake = endpoint(jwks_v1, requests.ConnectionError("clerk down"), delay=0.1)
    clerk_auth.get_public_key_for_token(_token("kid-1"))

    outcomes = _outcomes(lambda: clerk_auth.get_public_key_for_token(_token("kid-new")), n=6)
    assert all(isinstance(outcome, ServiceUnavailableError) for outcome, _ in outcomes)
    assert fake.fetches == 2   # the first load, then one shared refetch

    clerk_auth.get_public_key_for_token(_token("kid-1"))   # still served from the cache
    assert fake.fetches == 2


@pytest.mark.unit
@pytest.mark.parametrize("body", [None, [], "keys", {"error": "nope"}, {"keys": None}, {"keys": ["kid-1"]}])
def test_a_body_that_is_not_a_key_set_is_503_and_is_not_cached(endpoint, jwks_v1, body):
    fake = endpoint(body, jwks_v1)
    with pytest.raises(ServiceUnavailableError) as exc:
        clerk_auth.get_public_key_for_token(_token("kid-1"))
    assert exc.value.error_code == "AUTH_UNAVAILABLE"

    clerk_auth.get_public_key_for_token(_token("kid-1"))
    assert fake.fetches == 2


@pytest.mark.unit
@pytest.mark.parametrize("body", [None, [], "keys"])
def test_startup_warm_up_survives_a_body_that_is_not_a_key_set(endpoint, jwks_v1, body):
    fake = endpoint(body, jwks_v1)
    clerk_auth.warm_clerk_jwks()   # logs, does not raise
    clerk_auth.get_public_key_for_token(_token("kid-1"))
    assert fake.fetches == 2


@pytest.mark.unit
def test_startup_warm_up_fills_the_cache(endpoint, jwks_v1):
    fake = endpoint(jwks_v1)
    clerk_auth.warm_clerk_jwks()
    clerk_auth.get_public_key_for_token(_token("kid-1"))
    assert fake.fetches == 1


@pytest.mark.unit
def test_startup_warm_up_failure_is_not_fatal(endpoint, jwks_v1):
    fake = endpoint(requests.ConnectionError("clerk down"), jwks_v1)
    clerk_auth.warm_clerk_jwks()  # logs, does not raise
    clerk_auth.get_public_key_for_token(_token("kid-1"))  # the first request fetches, as before
    assert fake.fetches == 2
