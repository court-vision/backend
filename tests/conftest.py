"""
Root conftest for backend tests.

IMPORTANT: Environment variables must be set before ANY backend module is imported.
- db/base.py calls parse(DATABASE_URL) at module import time
- core/settings.py instantiates Settings() at module import time
Both require DATABASE_URL, CLERK_JWKS_URL, CLERK_SECRET_KEY, and PIPELINE_API_TOKEN.
"""

import os
from datetime import date

import pytest
from freezegun import freeze_time

# Set defaults BEFORE any backend imports.
os.environ.setdefault("DATABASE_URL", "postgresql://cv:cv@localhost:5432/cv_test")
os.environ.setdefault("CLERK_JWKS_URL", "https://fake.clerk.dev/.well-known/jwks.json")
os.environ.setdefault("CLERK_SECRET_KEY", "sk_test_fake")
os.environ.setdefault("PIPELINE_API_TOKEN", "test-token")


# ---- Time-freezing fixtures ----

@pytest.fixture
def freeze_game_night():
    """9:00 PM ET on March 4, 2026 (02:00 UTC March 5). Games in progress."""
    with freeze_time("2026-03-05T02:00:00Z"):
        yield date(2026, 3, 4)


@pytest.fixture
def freeze_morning():
    """10:00 AM ET on March 4, 2026 (15:00 UTC). No games yet."""
    with freeze_time("2026-03-04T15:00:00Z"):
        yield date(2026, 3, 4)


@pytest.fixture
def freeze_post_midnight():
    """3:00 AM ET on March 5, 2026 (08:00 UTC). NBA date = March 4 (before 6 AM ET)."""
    with freeze_time("2026-03-05T08:00:00Z"):
        yield date(2026, 3, 4)
