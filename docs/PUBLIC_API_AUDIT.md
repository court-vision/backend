# Public API audit — September 6, 2026

Reviewed the public FastAPI routes, service queries, normalized models, migrations
0001–0017, the data-platform pipeline registry, and the developer documentation.
The starting backend revision was `61ab397`. The PR was subsequently rebased onto
`f8fcb73`, preserving the rankings position field from migration 0018. This is a local code/database audit;
it is not a claim that every route has been exercised against production data.

## Findings and implemented fixes

| Finding | Change |
|---|---|
| Expiring API keys compare PostgreSQL `timestamptz` with naive `utcnow()`, causing a TypeError during authentication. | Use UTC-aware timestamps for creation, expiry checks, and last-use updates. Update only `last_used_at` during verification. Mirror the model change in data-platform. |
| Team, game, ownership, playoff, and analytics routes could return HTTP 200 for failure envelopes. | Apply the existing `respond()` adapter consistently, preserving error codes and real 4xx/5xx statuses. Preserve 503 database errors through the touched services' legacy catch blocks. |
| A caller can rotate arbitrary `X-API-Key` strings to reset an anonymous rate-limit bucket. Different real keys with the same display prefix also share a bucket. | Public requests use the IP; authenticated requests use a full verified key digest. Add `Retry-After` on 429 and expose it to browsers. Add the missing playoff rate limit. |
| Team rosters select one team-wide snapshot date, omitting players who missed the latest game. Filtering before choosing each player's newest row can also retain traded players. | Share a season-scoped, latest-row-per-player query; apply team filters afterward. Use it for rosters, player lists, and live-game player membership. |
| Player trends and the single-player rolling helper can present old or omitted rolling rows as current. | Use the existing rolling freshness policy and the latest complete snapshot for each window. Include the snapshot date in each returned trend period. |
| Lists/trends read the materialized rankings directly, bypassing the fallback added in migration 0006. | Reuse the rankings service's materialization freshness check and source-view fallback. |
| Game-log `opponent` and `home` are always null despite schedule data already being stored. | Resolve schedule context in one batch using historical team + date. Also return `game_id`. Missing or ambiguous schedule matches remain null. |
| Player list name search misses accents; offset pagination lacks a final stable tie-breaker. | Accent-insensitive search using the existing `unaccent` extension; NBA ID breaks ordering ties. |
| API docs describe `player_id` as an ESPN alias; the path stats route ignores window selection. | Document distinct NBA/ESPN IDs and support `window=lN` on both stats routes. Normalize the stats team filter's case. |
| Previous-season fallback is disclosed only in prose. | Add `season`/`as_of_date` metadata to player lists and `season` to team rosters. |
| Developer examples show the wrong percentage scale and imply all public API keys grant 1,000 requests/minute. | Correct examples, authentication/rate-limit guidance, and ID descriptions; remove unnecessary credentials from public examples. Refresh OpenAPI and generated TypeScript. |

Existing response field names and successful response envelopes remain compatible;
the new data fields are additive. Clients that relied on HTTP 200 for failures
must now handle the proper error statuses. The public quota is unchanged at 100
requests/minute **per endpoint per IP**; analytics remains 1,000 per verified key
per endpoint. SQLMate visual queries retain their separate 30/minute quota.

## New API surface from existing ingestion

The new ingestion is broader than rankings alone: `preseason_market` writes
`nba.draft_market` and `nba.player_projections`. It captures ESPN STANDARD editorial
rank, editorial auction value, crowd ADP/auction averages, primary position,
eligible lineup slots, injury status, projected games, and per-game projections.
It publishes a day's writes in one transaction. Unresolved ESPN players are
skipped by ingestion, so public coverage is limited to mapped NBA identities.

Implemented:

- `GET /v1/rankings/espn`: market data, source/season/date metadata, name search,
  bounded pagination, and sorting by rank, ADP, or either auction value. Mapped
  rookies do not need historical game stats to appear.
- `GET /v1/players/{player_id}/projection`: projected games and per-game raw stat
  inputs, with shooting rates recomputed from makes/attempts. NBA IDs only.

Both accept `season` and `as_of`. Historical reads select the newest snapshot on
or before the requested date and return its actual date. Season keys require
consecutive years. Neither route silently falls back to a previous season or an
older per-player row within a newer batch. Missing provider measurements stay
null. A known player without a projection returns 200 with `data: null`; unknown
players return 404. Projection percentages use 0–1. These routes expose curated
fields, not raw provider payloads, pipeline IDs, or private draft/league state.

## Completed follow-up — bulk projections and market movement

Completed September 8, 2026, without new ingestion or a database migration:

- `GET /v1/players/projections` exposes the selected global ESPN projection
  snapshot with optional season/date/name filters and bounded pagination. Rows
  use the same serializer as the single-player projection route, so per-game
  units, 0–1 shooting rates, and null handling cannot diverge.
- `GET /v1/rankings/espn/movement` compares two required on-or-before date
  selectors. It returns the union of both snapshots using a full outer join,
  preserves absent rows and values as null, and calculates all four market
  changes. Positive rank/ADP values mean improvement; positive auction values
  mean an increase. Metric/direction controls produce stable, NBA-ID-tiebroken
  pagination.

Both response payloads report the actual selected dates. If the requested
season has no applicable projection snapshot, or the two movement selectors do
not resolve to distinct snapshots, the endpoint returns a successful empty
collection with an explanatory message. Existing market and single-player
projection routes remain unchanged; these new reads use the same public
per-endpoint/IP rate limit and deliberately add no cache.

Example requests:

```text
/v1/players/projections?season=2026-27&as_of=2026-09-05&name=Jokic&limit=25
/v1/rankings/espn/movement?season=2026-27&from_as_of=2026-09-01&to_as_of=2026-09-08&metric=rank&direction=up
```

Representative movement response (the selected snapshots may predate the
requested selectors):

```json
{
  "status": "success",
  "message": "ESPN draft market movement",
  "data": {
    "season": "2026-27",
    "source": "espn",
    "from_as_of_date": "2026-09-01",
    "to_as_of_date": "2026-09-07",
    "metric": "rank",
    "direction": "up",
    "players": [
      {
        "player_id": 203999,
        "espn_id": 3112335,
        "name": "Nikola Jokic",
        "before": {"overall_rank": 8, "adp": 7.4, "auction_value": 54.0, "auction_value_avg": 52.1},
        "after": {"overall_rank": 5, "adp": 5.8, "auction_value": 58.0, "auction_value_avg": 55.2},
        "changes": {"overall_rank": 3, "adp": 1.6, "auction_value": 4.0, "auction_value_avg": 3.1}
      }
    ],
    "total": 1,
    "limit": 50,
    "offset": 0
  }
}
```

## Completed follow-up — dimension player directory

Completed September 7, 2026:

- `GET /v1/players/search`: accent-insensitive name search plus exact NBA/ESPN
  ID lookup over `nba.players`, with bounded pagination and stable ordering. It
  does not join season-stat facts, so mapped rookies and players without games
  are included. Results carry player/profile freshness timestamps.
- `GET /v1/players/{player_id}/profile`: NBA/ESPN identity, dimension timestamps,
  and the latest `nba.player_profiles` biography. Known identities without an
  ingested profile return 200 with `profile: null`; unknown NBA IDs return 404.

The existing `GET /v1/players/` contract remains stats-backed and unchanged.
Profile team values are labeled as snapshot metadata rather than authoritative
current-roster assignments; roster provenance remains a separate follow-up.
The complete backend suite passes with 898 tests and 4 expected legacy-calendar
skips, including the PostgreSQL integration suite. The refreshed OpenAPI
snapshot generates cleanly and the frontend typecheck passes.

Example requests:

```text
/v1/rankings/espn?season=2026-27&sort_by=adp&limit=25
/v1/rankings/espn?season=2026-27&as_of=2026-09-01&name=Jokic
/v1/rankings/espn/movement?from_as_of=2026-09-01&to_as_of=2026-09-08
/v1/players/search?q=Jokic&limit=10
/v1/players/203999/profile
/v1/players/projections?season=2026-27&limit=25
/v1/players/203999/projection?season=2026-27
/v1/players/203999/stats?window=l10
```

## Migration cleanup

- Move the canonical rankings models to `db/models/nba/rankings.py`. Keep the old
  `db.models.stats.rankings` import as a compatibility shim for scripts/tests.
- Reuse `nba.rankings_source` when the materialized copy lags. Do not reintroduce
  `player_season_stats.rank`: migration 0008 removed a cohort artifact, not a
  league-wide rank.
- Preserve the mirrored API-key and season-stat models in both services; the
  data-platform mirror guard checks them byte-for-byte.
- No new migration is needed for these read-path changes. The new endpoints use
  tables and indexes from migrations 0011/0012.
- **Do not drop `stats_s2` wholesale.** Daily matchup scores remain an active
  application/pipeline dependency. The old daily/cumulative player-stat models
  are still used by `scripts/migrate_to_nba_schema.py`, and the baseline retains
  the legacy ranking view. Retiring those objects needs a separate migration,
  an archival decision, and coordination with external SQL consumers. The
  migration guide requires two releases for column drops across the services.

## Scoped follow-ups

These need no new provider ingestion unless noted:

| Priority | Addition | Scope and acceptance criteria |
|---|---|---|
| Next | Team roster provenance | Evaluate `player_profiles.team_id` and its update cadence against season-stat assignments. Define when profile data is authoritative before using it for current rosters. The current route deliberately reports last known statistical membership. |
| Later | Advanced-stat comparison | Advanced player stats are already embedded in `/players/stats`; a bounded batch comparison endpoint can reuse them. Specify season/freshness and percent units before adding another representation. |
| Later | Historical performance queries | Add explicit season/date bounds and game-log pagination. Avoid silently mixing regular season, playoffs, or career totals. Existing game facts have team/date but no game ID/season FK, so persistent normalization is a separate migration/backfill. |
| Later | Public schema and docs coverage | Give the SQLMate passthrough a concrete response contract; derive developer endpoint inventory from OpenAPI to avoid maintaining two catalogs. |
| Later | Cache and quota policy | Measure the uncached public reads before extending the rankings byte-cache pattern. Verified-key upgrades for ordinary public reads, if desired, need optional authentication plus an explicit quota policy; an arbitrary header must never grant one. |

The registry also retains advanced stats, profiles, team stats, ownership/injuries,
live games, playoffs, and schedules. Most already have public representations;
adding a standalone advanced endpoint is an API packaging choice, not new data.

## Validation

Completed the backend unit/API suite plus the integration suite against a disposable
PostgreSQL 16 database built from the real migration chain. The new regressions
cover sparse rosters, trades, stale rankings, rolling snapshot omissions, accent
search, historical matchup joins, projections for players with no games, bulk
projection pagination, independently resolved market comparisons, movement sign
conventions, entrants/exits, null semantics, timezone-aware API-key expiry, HTTP
errors, request validation, and attempted quota bypasses.

- Backend: **1,025 passed, 4 expected legacy-calendar skips**.
- Data-platform unit/API suite: **280 passed** with the disposable test DB. Six
  tests initially required a DB despite their unit/API placement; pointing them
  at the local migrated database resolved those failures.
- Frontend: OpenAPI and TypeScript regenerated; typecheck and targeted ESLint
  passed. All 20 developer response examples parse as JSON.
- Model mirror: **38 mirrored + 1 renamed pairs identical**.
- Backend and frontend worktrees pass `git diff --check`.

No production schema, ingestion jobs, credentials, or deployments were changed.
