"""Shared HTTP/runtime boundaries for external data providers."""

from services.providers.adapters import FantasyProviderAdapter, get_provider_adapter
from services.providers.http import provider_get, provider_label, provider_post

__all__ = [
    "FantasyProviderAdapter",
    "get_provider_adapter",
    "provider_get",
    "provider_post",
    "provider_label",
]
