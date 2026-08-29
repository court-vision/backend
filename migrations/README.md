# Schema migrations

This directory is the **single source of truth for the shared PostgreSQL schema** —
`usr.*`, `nba.*`, and `stats_s2.*`. The backend and data-platform services share one
database; data-platform reads and writes these tables but never alters schema. Any
schema change for either service lands here as a new migration.

Migrations are plain SQL run by [yoyo-migrations](https://ollycope.com/software/yoyo/).
They are applied automatically at backend startup (`db.migrate.apply_migrations`, called
from `init_db()`), so a deploy that ships a migration applies it before serving traffic.
A failing migration aborts startup — Railway keeps the previous deployment serving.

## Adding a migration

1. Create `NNNN__short_snake_description.sql` with the next four-digit number.
2. Write idempotent-where-possible SQL (`IF NOT EXISTS`, `DROP VIEW IF EXISTS` + `CREATE VIEW`).
3. Optionally add `NNNN__short_snake_description.rollback.sql`.
4. Keep the Peewee model in `db/models/` in sync — models describe the schema, they do not create it.
5. Run it locally: `.venv/bin/yoyo apply --batch --database "$DATABASE_URL" ./migrations`
   (or just start the app — it applies pending migrations on boot).

Only the schema version table lives in `public` (`_yoyo_migration`, `_yoyo_log`,
`_yoyo_version`, `yoyo_lock`). Never point a second migration chain at this database.

## Notes

- `0001__baseline.sql` is a cleaned `pg_dump --schema-only` of production taken on
  2026-08-23 and is what fresh environments are built from. Databases that already had
  the schema (production, staging) were adopted by marking the chain as applied without
  executing it — see `_predates_migrations` in `db/migrate.py`.
- Views (e.g. `nba.rankings`) are kept out of the baseline and defined in their own
  migration so revisions are a new numbered file rather than an edit to the dump.
- A migration needing `CREATE INDEX CONCURRENTLY` must start with
  `-- transactional: false`.
- **Dropping a column takes two releases.** Both services write the `nba.*` tables
  from their own copy of the Peewee models, and only the backend applies
  migrations — at startup. A drop shipped alongside the code that stopped writing
  the column will meet the *other* service's previous image still writing it, and
  that pipeline run fails with `UndefinedColumn`. Ship the code first, confirm a
  clean run, then ship the drop. `0008__drop_player_season_stats_rank.sql` is the
  worked example.
