"""
Resilience Patterns

Provides retry decorators, circuit breakers, and resilient HTTP client
for handling transient failures in external API calls.
"""

import logging
from typing import Any, Callable, Optional, TypeVar

import requests
from circuitbreaker import circuit, CircuitBreakerError
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
    RetryError,
)

from core.logging import get_logger


# Type variable for generic return types
T = TypeVar("T")


# -----------------------------------------------------------------------------
# Custom Exceptions
# -----------------------------------------------------------------------------


class RetryableError(Exception):
    """Base class for errors that should trigger retries."""

    pass


class RateLimitError(RetryableError):
    """Raised when rate limited (HTTP 429)."""

    def __init__(self, message: str, retry_after: Optional[int] = None):
        super().__init__(message)
        self.retry_after = retry_after


class NetworkError(RetryableError):
    """Raised on network/timeout errors."""

    pass


class ServerError(RetryableError):
    """Raised on server errors (5xx)."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class ClientError(Exception):
    """Raised on client errors (4xx). Not retryable."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


# -----------------------------------------------------------------------------
# Retry Decorators
# -----------------------------------------------------------------------------


def create_retry_decorator(
    max_attempts: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 30.0,
    logger: Optional[logging.Logger] = None,
) -> Callable:
    """
    Create a retry decorator with exponential backoff.

    Args:
        max_attempts: Maximum number of retry attempts
        base_delay: Initial delay between retries (seconds)
        max_delay: Maximum delay between retries (seconds)
        logger: Optional logger for retry notifications

    Returns:
        A tenacity retry decorator

    Example:
        @create_retry_decorator(max_attempts=3)
        def fetch_data():
            ...
    """
    return retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=base_delay, max=max_delay),
        retry=retry_if_exception_type(RetryableError),
        before_sleep=before_sleep_log(logger, logging.WARNING) if logger else None,
        reraise=True,
    )


def with_retry(
    max_attempts: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 30.0,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Decorator factory for retrying functions on RetryableError.

    Args:
        max_attempts: Maximum number of retry attempts
        base_delay: Initial delay between retries (seconds)
        max_delay: Maximum delay between retries (seconds)

    Example:
        @with_retry(max_attempts=3)
        def fetch_player_stats():
            ...
    """
    log = get_logger("retry")

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @retry(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=base_delay, max=max_delay),
            retry=retry_if_exception_type(RetryableError),
            reraise=True,
        )
        def wrapper(*args: Any, **kwargs: Any) -> T:
            try:
                return func(*args, **kwargs)
            except RetryableError as e:
                log.warning(
                    "retry_attempt",
                    function=func.__name__,
                    error=str(e),
                    error_type=type(e).__name__,
                )
                raise

        return wrapper

    return decorator


# -----------------------------------------------------------------------------
# Circuit Breakers
# -----------------------------------------------------------------------------


def create_circuit_breaker(
    name: str,
    failure_threshold: int = 5,
    recovery_timeout: int = 60,
) -> Callable:
    """
    Create a circuit breaker decorator.

    Args:
        name: Name of the circuit breaker for identification
        failure_threshold: Number of failures before opening circuit
        recovery_timeout: Seconds to wait before attempting recovery

    Returns:
        A circuit breaker decorator
    """
    return circuit(
        failure_threshold=failure_threshold,
        recovery_timeout=recovery_timeout,
        expected_exception=RetryableError,
        name=name,
    )


# Pre-configured circuit breakers for external APIs
nba_api_circuit = create_circuit_breaker(
    name="nba_api",
    failure_threshold=5,
    recovery_timeout=60,
)

espn_api_circuit = create_circuit_breaker(
    name="espn_api",
    failure_threshold=5,
    recovery_timeout=60,
)


# -----------------------------------------------------------------------------
# Resilient HTTP Client
# -----------------------------------------------------------------------------


def classify_response_error(response: requests.Response) -> None:
    """
    Classify HTTP response errors and raise appropriate exceptions.

    Args:
        response: The HTTP response to classify

    Raises:
        RateLimitError: For 429 responses
        ServerError: For 5xx responses
        ClientError: For 4xx responses
    """
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        retry_seconds = int(retry_after) if retry_after else 60
        raise RateLimitError(
            f"Rate limited, retry after {retry_seconds}s",
            retry_after=retry_seconds,
        )

    if response.status_code >= 500:
        raise ServerError(
            f"Server error: {response.status_code}",
            status_code=response.status_code,
        )

    if response.status_code >= 400:
        raise ClientError(
            f"Client error: {response.status_code} - {response.text[:200]}",
            status_code=response.status_code,
        )


def resilient_request(
    method: str,
    url: str,
    timeout: int = 30,
    **kwargs: Any,
) -> requests.Response:
    """
    Make an HTTP request with retry-aware error handling.

    Args:
        method: HTTP method (GET, POST, etc.)
        url: Request URL
        timeout: Request timeout in seconds
        **kwargs: Additional arguments passed to requests

    Returns:
        The HTTP response

    Raises:
        NetworkError: On connection or timeout errors
        RateLimitError: On 429 responses
        ServerError: On 5xx responses
        ClientError: On 4xx responses
    """
    log = get_logger("http")

    try:
        log.debug("http_request", method=method, url=url)
        response = requests.request(method, url, timeout=timeout, **kwargs)
        classify_response_error(response)
        response.raise_for_status()
        log.debug("http_response", method=method, url=url, status=response.status_code)
        return response

    except requests.exceptions.Timeout:
        log.warning("http_timeout", method=method, url=url)
        raise NetworkError(f"Request timed out: {url}")

    except requests.exceptions.ConnectionError as e:
        log.warning("http_connection_error", method=method, url=url, error=str(e))
        raise NetworkError(f"Connection failed: {url}")

    except (RateLimitError, ServerError, ClientError):
        # Re-raise our custom exceptions
        raise

    except requests.exceptions.RequestException as e:
        log.error("http_error", method=method, url=url, error=str(e))
        raise NetworkError(f"Request failed: {url} - {e}")


class ResilientHTTPClient:
    """
    HTTP client with built-in retry and circuit breaker support.

    Example:
        client = ResilientHTTPClient(
            max_retries=3,
            timeout=30,
            circuit_breaker=nba_api_circuit
        )
        response = client.get("https://api.nba.com/stats")
    """

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 2.0,
        max_delay: float = 30.0,
        timeout: int = 30,
        circuit_breaker: Optional[Callable] = None,
    ):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.timeout = timeout
        self.circuit_breaker = circuit_breaker
        self.log = get_logger("http_client")

    def _make_request(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response:
        """Internal method to make a single request."""
        return resilient_request(
            method=method,
            url=url,
            timeout=kwargs.pop("timeout", self.timeout),
            **kwargs,
        )

    def _request_with_retry(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response:
        """Make request with retry logic."""

        @retry(
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=self.base_delay, max=self.max_delay),
            retry=retry_if_exception_type(RetryableError),
            reraise=True,
        )
        def _do_request() -> requests.Response:
            return self._make_request(method, url, **kwargs)

        return _do_request()

    def request(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response:
        """
        Make an HTTP request with retry and optional circuit breaker.

        Args:
            method: HTTP method
            url: Request URL
            **kwargs: Additional request arguments

        Returns:
            The HTTP response
        """
        if self.circuit_breaker:

            @self.circuit_breaker
            def _protected_request() -> requests.Response:
                return self._request_with_retry(method, url, **kwargs)

            return _protected_request()
        else:
            return self._request_with_retry(method, url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        """Make a GET request."""
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        """Make a POST request."""
        return self.request("POST", url, **kwargs)


# -----------------------------------------------------------------------------
# Convenience Functions
# -----------------------------------------------------------------------------


def is_circuit_open(circuit_name: str) -> bool:
    """Check if a circuit breaker is currently open."""
    try:
        from circuitbreaker import CircuitBreaker

        cb = CircuitBreaker.get_circuit_breaker(circuit_name)
        return cb.state.name == "open" if cb else False
    except Exception:
        return False


# Re-export for convenience
__all__ = [
    "RetryableError",
    "RateLimitError",
    "NetworkError",
    "ServerError",
    "ClientError",
    "CircuitBreakerError",
    "RetryError",
    "create_retry_decorator",
    "with_retry",
    "create_circuit_breaker",
    "nba_api_circuit",
    "espn_api_circuit",
    "resilient_request",
    "ResilientHTTPClient",
    "is_circuit_open",
]
