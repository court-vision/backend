"""The provider boundary: adapters, capabilities, vocabulary, identity, writers,
and the shared HTTP/runtime layer under them."""

from services.providers.adapters import FantasyProviderAdapter, get_provider_adapter
from services.providers.capabilities import (
    ProviderCapabilities,
    ProviderCapabilityMissing,
    provider_name,
    unavailable_message,
)
from services.providers.http import provider_get, provider_label, provider_post
from services.providers.writers import RosterWriter, get_roster_writer

__all__ = [
    "FantasyProviderAdapter",
    "ProviderCapabilities",
    "ProviderCapabilityMissing",
    "RosterWriter",
    "get_provider_adapter",
    "get_roster_writer",
    "provider_get",
    "provider_label",
    "provider_name",
    "provider_post",
    "unavailable_message",
]
