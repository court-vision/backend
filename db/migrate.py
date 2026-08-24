"""
Schema migrations, applied at application startup with yoyo-migrations.

backend/migrations/ is the single source of truth for the shared database
(usr.*, nba.*, stats_s2.*). See migrations/README.md for the conventions.
"""

from pathlib import Path

from yoyo import get_backend, read_migrations

from core.logging import get_logger

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"

# Migrations whose end state already existed before the chain was introduced.
# A database created by the old create_tables() path has these applied in
# substance, so they are recorded as applied rather than executed.
ADOPTION_IDS = {"0001__baseline", "0002__create_nba_rankings_view"}

log = get_logger()


def apply_migrations(database_url: str) -> list[str]:
    """Bring the database schema up to date. Returns the ids applied this call."""
    migrations = read_migrations(str(MIGRATIONS_DIR))

    with get_backend(database_url) as backend:
        with backend.lock():
            if _predates_migrations(backend):
                adopted = [m for m in migrations if m.id in ADOPTION_IDS]
                backend.mark_migrations(adopted)
                log.warning("migrations_adopted_existing_schema", ids=[m.id for m in adopted])

            pending = backend.to_apply(migrations)
            backend.apply_migrations(pending)

    applied = [m.id for m in pending]
    if applied:
        log.info("migrations_applied", ids=applied)
    return applied


def _predates_migrations(backend) -> bool:
    """True for a schema built before this chain existed (no yoyo state, but usr.users present)."""
    if backend.get_applied_migration_hashes():
        return False
    row = backend.execute("SELECT to_regclass('usr.users')").fetchone()
    return row is not None and row[0] is not None
