"""HTTP layer shared by the fantasy providers (ESPN, Yahoo) and the NBA CDN."""

from services.providers.http import provider_get, provider_label, provider_post

__all__ = ["provider_get", "provider_post", "provider_label"]
