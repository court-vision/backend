"""
Base Extractor

Abstract base class for data extractors.
"""

from abc import ABC, abstractmethod
from typing import Any

from core.logging import get_logger


class BaseExtractor(ABC):
    """
    Abstract base class for data extractors.

    Extractors are responsible for fetching data from external sources
    (APIs, databases, files) with proper error handling and resilience.

    Subclasses should:
    - Use @with_retry decorator for retryable operations
    - Use circuit breakers for external APIs
    - Return raw data (transformation is done by transformers)
    """

    def __init__(self, name: str):
        """
        Initialize extractor.

        Args:
            name: Extractor name for logging
        """
        self.name = name
        self.log = get_logger(f"extractor.{name}")

    @abstractmethod
    def extract(self, **kwargs: Any) -> Any:
        """
        Extract data from the source.

        Args:
            **kwargs: Source-specific parameters

        Returns:
            Raw extracted data

        Raises:
            NetworkError, RateLimitError, etc. for retryable failures
        """
        pass
