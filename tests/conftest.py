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
# Deterministic season for the suite (settings would derive it from today's date)
os.environ.setdefault("NBA_SEASON", "2025-26")


# ---- Module-level read caches ----

@pytest.fixture(autouse=True)
def empty_public_read_caches():
    """The public read caches and the percentile pool live at module level; an
    entry left by one test would answer for the next test's stubs or rows."""
    from core import public_read_cache
    from services.player_service import reset_percentile_pool_cache

    public_read_cache.clear_all()
    reset_percentile_pool_cache()
    yield
    public_read_cache.clear_all()
    reset_percentile_pool_cache()

# ---- Per-process caches ----

@pytest.fixture(autouse=True)
def empty_category_value_cache():
    """Category pools and scores are cached per process (services.player_value_service);
    one test's stubbed or seeded pool must never be served to the next. Looked up in
    sys.modules so a test that never imports the service does not import it here."""
    import sys

    def clear():
        module = sys.modules.get("services.player_value_service")
        if module is not None:
            module.PlayerValueService.clear_category_cache()

    clear()
    yield
    clear()


@pytest.fixture(autouse=True)
def empty_draft_live_cache():
    """The stored boards' facts beside an in-season draft board are cached per process
    (services.draft_live); one test's seeded days must never answer for the next."""
    import sys

    def clear():
        module = sys.modules.get("services.draft_live")
        if module is not None:
            module.clear_cache()

    clear()
    yield
    clear()


# ---- Dime's holds ----

@pytest.fixture(autouse=True)
def no_team_holds(request, monkeypatch):
    """Every fill and lane plan reads the team's holds (services/steering.py). Outside the
    database-backed suite a test sees none unless it stubs its own, so no unit test reaches
    for a database through them."""
    if request.node.get_closest_marker("integration"):
        yield
        return
    from services import steering

    async def none(team_id, today=None):
        return []

    monkeypatch.setattr(steering, "live_rows", none)
    yield


# ---- The live matchup's kept provider reads ----

@pytest.fixture(autouse=True)
def no_kept_matchups():
    """The live matchup keeps its provider read per process for a few seconds
    (services/matchup_hold.py); one test's must never answer for the next."""
    import sys

    def clear():
        module = sys.modules.get("services.matchup_hold")
        if module is not None:
            module.clear()

    clear()
    yield
    clear()


# ---- ESPN's pro schedule (a per-game-day add limit's matchup total) ----

@pytest.fixture(autouse=True)
def no_espn_pro_schedule(monkeypatch):
    """A per-game-day add limit counts the matchup's game days on ESPN's public pro schedule
    (services/acquisitions.py). No test reads ESPN: the read answers None (no total, as when
    ESPN can't be read), unless a test stubs `espn_game_days` itself."""
    from services import acquisitions

    async def unread(year):
        return None

    monkeypatch.setattr(acquisitions, "espn_game_days", unread)
    acquisitions._pro_schedule.clear()
    yield


# ---- The roster lock ----

@pytest.fixture(autouse=True)
def no_roster_protections(request, monkeypatch):
    """Every add/drop, lane decision and drop suggestion reads the team's protected players
    (services/roster_protection.py). Outside the database-backed suite a test sees none unless
    it stubs its own (`protect` below), so no unit test reaches for a database through them."""
    if request.node.get_closest_marker("integration"):
        yield
        return
    from services import roster_protection

    async def none(team_id):
        return frozenset()

    async def no_rows(team_id):
        return []

    async def no_teams(team_ids):
        return {}

    async def no_seed(team_id, user_id):
        return []

    monkeypatch.setattr(roster_protection, "ids", none)
    monkeypatch.setattr(roster_protection, "for_team", no_rows)
    monkeypatch.setattr(roster_protection, "for_teams", no_teams)
    # A check-in's start protects a team's first draft picks once (`seed`): not in a unit test
    monkeypatch.setattr(roster_protection, "seed_for", no_seed)
    yield


@pytest.fixture
def protect(monkeypatch):
    """`protect({team_id: [espn ids]})` makes those players protected for the reads above (names
    for `for_team`/`for_teams` come from `names`, an ESPN id to name map, when given)."""
    from services import roster_protection

    def apply(by_team, names=None, nba_ids=None):
        names, nba_ids = names or {}, nba_ids or {}

        async def ids(team_id):
            return frozenset(by_team.get(team_id, ()))

        def rows(team_id):
            return [roster_protection.Protected(player_id=pid, nba_player_id=nba_ids.get(pid),
                                                name=names.get(pid, f"player {pid}"), team=None)
                    for pid in by_team.get(team_id, ())]

        async def for_team(team_id):
            return rows(team_id)

        async def for_teams(team_ids):
            return {tid: rows(tid) for tid in team_ids if by_team.get(tid)}

        monkeypatch.setattr(roster_protection, "ids", ids)
        monkeypatch.setattr(roster_protection, "for_team", for_team)
        monkeypatch.setattr(roster_protection, "for_teams", for_teams)

    return apply


# ---- Reading a write back ----

@pytest.fixture(autouse=True)
def one_readback(monkeypatch):
    """A write chain reads ESPN again while its read-back doesn't show the write yet
    (services/write_readback.py), sleeping up to 5 s. A test reads back once unless it
    sets its own `READBACK_DELAYS_S`, so a stubbed read queue sized for one read-back
    still fits and no test waits."""
    from services import write_readback

    monkeypatch.setattr(write_readback, "READBACK_DELAYS_S", ())
    yield


# ---- The captured draft ----

@pytest.fixture(scope="session")
def replay():
    """A real 52-pick ESPN snake draft, vendored under tests/fixtures/.

    Its provenance is in the file's own `_source`. Both the fast arithmetic
    conformance check and the end-to-end replay read it, so the two can never
    drift apart on what the draft actually was.
    """
    import json
    from pathlib import Path

    path = Path(__file__).parent / "fixtures" / "draft_replay_4team_13round.json"
    return json.loads(path.read_text())


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
