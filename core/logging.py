"""
Structured Logging Configuration

Provides structured logging using structlog with:
- Correlation ID support for request tracing
- JSON or console output formats
- Automatic context injection
"""

import logging
import sys
from contextvars import ContextVar
from typing import Any, Optional

import structlog


# Context variable for correlation ID - accessible across async contexts
correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")


def get_correlation_id() -> str:
    """Get the current correlation ID."""
    return correlation_id_var.get()


def set_correlation_id(cid: str) -> None:
    """Set the correlation ID for the current context."""
    correlation_id_var.set(cid)


def add_correlation_id(
    logger: logging.Logger, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Structlog processor to add correlation ID to log events."""
    cid = correlation_id_var.get()
    if cid:
        event_dict["correlation_id"] = cid
    return event_dict


def add_service_info(
    service_name: str,
) -> structlog.typing.Processor:
    """Create a processor that adds service name to all log events."""

    def processor(
        logger: logging.Logger, method_name: str, event_dict: dict[str, Any]
    ) -> dict[str, Any]:
        event_dict["service"] = service_name
        return event_dict

    return processor


def setup_logging(
    log_level: str = "INFO",
    json_format: bool = True,
    service_name: str = "court-vision-api",
) -> None:
    """
    Configure structlog for the application.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        json_format: If True, output JSON logs. If False, use console format.
        service_name: Name of the service to include in logs
    """
    # Configure standard library logging
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, log_level.upper()),
    )

    # Shared processors for all log entries
    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        add_service_info(service_name),
        add_correlation_id,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if json_format:
        # JSON output for production
        processors = shared_processors + [
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    else:
        # Console output for development
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(
                colors=True,
                exception_formatter=structlog.dev.plain_traceback,
            ),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level.upper())
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: Optional[str] = None) -> structlog.BoundLogger:
    """
    Get a configured logger instance.

    Args:
        name: Optional logger name for categorization

    Returns:
        A bound structlog logger

    Example:
        log = get_logger("pipeline")
        log.info("pipeline_started", pipeline_name="daily_stats")
    """
    logger = structlog.get_logger(name)
    return logger


class LoggerAdapter:
    """
    Adapter to provide a familiar logging interface.

    Wraps structlog to provide debug/info/warning/error/critical methods
    with automatic key-value context support.
    """

    def __init__(self, name: Optional[str] = None):
        self._logger = get_logger(name)
        self._context: dict[str, Any] = {}

    def bind(self, **kwargs: Any) -> "LoggerAdapter":
        """Add context that will be included in all subsequent log calls."""
        new_adapter = LoggerAdapter.__new__(LoggerAdapter)
        new_adapter._logger = self._logger.bind(**kwargs)
        new_adapter._context = {**self._context, **kwargs}
        return new_adapter

    def debug(self, event: str, **kwargs: Any) -> None:
        """Log at DEBUG level."""
        self._logger.debug(event, **kwargs)

    def info(self, event: str, **kwargs: Any) -> None:
        """Log at INFO level."""
        self._logger.info(event, **kwargs)

    def warning(self, event: str, **kwargs: Any) -> None:
        """Log at WARNING level."""
        self._logger.warning(event, **kwargs)

    def error(self, event: str, **kwargs: Any) -> None:
        """Log at ERROR level."""
        self._logger.error(event, **kwargs)

    def critical(self, event: str, **kwargs: Any) -> None:
        """Log at CRITICAL level."""
        self._logger.critical(event, **kwargs)

    def exception(self, event: str, **kwargs: Any) -> None:
        """Log at ERROR level with exception info."""
        self._logger.exception(event, **kwargs)
