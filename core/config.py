"""
Legacy configuration module.

This module is deprecated. Use core.settings instead.
Kept for backwards compatibility with existing imports.
"""

# Re-export settings from the new module for backwards compatibility
from core.settings import settings, Settings, get_settings

__all__ = ["settings", "Settings", "get_settings"]