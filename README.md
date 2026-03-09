# Court Vision — Backend API

FastAPI REST API for [Court Vision](https://courtvision.dev), a fantasy basketball analytics platform. This service handles all user-facing API routes, reads from PostgreSQL, integrates with Clerk for authentication, and proxies requests to the features service for lineup generation.

Runs on port **8000** locally (port **8080** in Docker/Railway).

---

## Tech Stack

| Layer | Technology |
|---|---|
| Framework | FastAPI 0.128, Uvicorn, Starlette |
| Language | Python 3.12 |
| ORM | Peewee 3.18 + psycopg2 |
| Database | PostgreSQL (pooled connections via `playhouse.pool`) |
| Auth | Clerk JWT (RS256 via JWKS) + scoped API keys |
| Data validation | Pydantic v2, pydantic-settings |
| Rate limiting | slowapi (100 req/min public, 1000 req/min API key) |
| Resilience | tenacity (retries) + circuitbreaker |
| Logging | structlog (JSON in production, console in dev) |
| NBA data | nba_api 1.9 |
| Email | Resend |
| Containerization | Docker (multi-stage, linux/amd64, non-root user) |

---

## Directory Structure

```
backend/
├── main.py                  # App entry point: FastAPI setup, middleware, router registration
├── requirements.txt         # Pinned production dependencies
├── requirements-dev.txt     # Dev/test dependencies
├── Dockerfile               # Multi-stage build (builder + runtime)
├── pytest.ini               # Test configuration
│
├── api/
│   └── v1/
│       ├── internal/        # Authenticated routes (Clerk JWT required)
│       │   ├── auth.py
│       │   ├── users.py
│       │   ├── teams.py
│       │   ├── lineups.py
│       │   ├── matchups.py
│       │   ├── streamers.py
│       │   ├── espn.py
│       │   ├── yahoo.py
│       │   ├── notifications.py
│       │   └── api_keys.py
│       └── public/          # Unauthenticated routes (rate-limited)
│           ├── rankings.py
│           ├── players.py
│           ├── games.py
│           ├── teams.py
│           ├── ownership.py
│           ├── analytics.py  # API key required (scoped)
│           ├── schedule.py
│           └── live.py
│
├── core/
│   ├── settings.py          # Pydantic Settings, all env var config
│   ├── clerk_auth.py        # Clerk JWT verification + JWKS caching
│   ├── api_key_auth.py      # Scoped API key verification
│   ├── resilience.py        # Retry decorators, circuit breakers, ResilientHTTPClient
│   ├── logging.py           # structlog setup
│   ├── rate_limit.py        # slowapi limiter, rate limit constants
│   ├── middleware.py        # CORS, GZip setup
│   ├── db_middleware.py     # DB connection lifecycle per request
│   ├── correlation_middleware.py  # Request correlation IDs
│   └── security.py
│
├── db/
│   ├── base.py              # DB pool init, table registration, create_tables
│   └── models/
│       ├── users.py
│       ├── teams.py
│       ├── lineups.py
│       ├── api_keys.py
│       ├── notifications.py
│       ├── pipeline_run.py
│       ├── nba/             # Normalized NBA schema models
│       │   ├── players.py
│       │   ├── teams.py
│       │   ├── games.py
│       │   ├── player_game_stats.py
│       │   ├── player_season_stats.py
│       │   ├── player_rolling_stats.py
│       │   ├── player_advanced_stats.py
│       │   ├── player_profiles.py
│       │   ├── player_ownership.py
│       │   ├── player_injuries.py
│       │   ├── live_player_stats.py
│       │   ├── team_stats.py
│       │   └── breakout_candidates.py
│       └── stats/           # Legacy stats_s2 schema (being deprecated)
│           ├── daily_player_stats.py
│           ├── cumulative_player_stats.py
│           ├── daily_matchup_score.py
│           └── rankings.py
│
├── services/                # Business logic layer
│   ├── player_service.py
│   ├── players_list_service.py
│   ├── rankings_service.py
│   ├── matchup_service.py   # Live matchup scoring (ESPN baseline + live overlay)
│   ├── team_service.py
│   ├── team_insights_service.py
│   ├── lineup_service.py
│   ├── streamer_service.py
│   ├── breakout_service.py
│   ├── ownership_service.py
│   ├── games_service.py
│   ├── schedule_service.py
│   ├── espn_service.py
│   ├── yahoo_service.py
│   ├── notification_service.py
│   ├── lineup_check_service.py
│   ├── optimize_service.py
│   ├── trends_service.py
│   ├── user_service.py
│   ├── user_sync_service.py
│   ├── player_games_service.py
│   └── auth_service.py
│
├── schemas/                 # Pydantic request/response models
│   ├── common.py            # BaseApiResponse, ApiStatus
│   ├── player.py
│   ├── matchup.py
│   ├── lineup.py
│   ├── streamer.py
│   └── ...
│
├── pipelines/               # Stub only — extractors used by live/games routes
│   └── extractors/          # NBAApiExtractor, ESPNExtractor
│
├── static/                  # Static data files (NBA schedule JSON, etc.)
├── scripts/                 # One-off utility scripts
├── migrations/              # DB migration scripts
└── tests/
```

---

## Setup & Installation

### Prerequisites

- Python 3.12
- PostgreSQL (with `nba` and `stats_s2` schemas)
- A [Clerk](https://clerk.com) application

### 1. Create and activate a virtual environment

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt

# For development/testing:
pip install -r requirements-dev.txt
```

### 3. Configure environment variables

Create a `.env` file in `backend/` (see [Key Environment Variables](#key-environment-variables)):

```bash
cp secrets.env .env   # if a template exists, otherwise create from scratch
```

### 4. Start the server

```bash
uvicorn main:app --reload --port 8000
```

The API will be available at `http://localhost:8000`.

Interactive docs:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

### Running with Docker

```bash
docker build -t cv-backend .
docker run -p 8000:8080 --env-file .env cv-backend
```

---

## Key Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | Yes | — | PostgreSQL connection URL (`postgresql://user:pass@host/dbname`) |
| `CLERK_JWKS_URL` | Yes | — | Clerk JWKS endpoint (`https://<your-clerk-api>.clerk.accounts.dev/.well-known/jwks.json`) |
| `CLERK_SECRET_KEY` | Yes | — | Clerk Backend API secret key (`sk_...`) |
| `PIPELINE_API_TOKEN` | Yes | — | Shared bearer token for service-to-service calls (not used by user routes) |
| `ESPN_YEAR` | No | `2026` | Active ESPN fantasy season year |
| `ESPN_LEAGUE_ID` | No | `993431466` | Default ESPN league ID |
| `NBA_SEASON` | No | `2025-26` | Active NBA season string |
| `BALLDONTLIE_API_KEY` | No | — | [BallDontLie](https://app.balldontlie.io) API key (injury data) |
| `YAHOO_CLIENT_ID` | No | — | Yahoo OAuth app client ID |
| `YAHOO_CLIENT_SECRET` | No | — | Yahoo OAuth app client secret |
| `YAHOO_REDIRECT_URI` | No | `http://localhost:8000/v1/internal/yahoo/callback` | Yahoo OAuth redirect URI |
| `FRONTEND_URL` | No | `http://localhost:3000` | Frontend URL (used for OAuth redirects) |
| `RESEND_API_KEY` | No | — | [Resend](https://resend.com) API key for email notifications |
| `NOTIFICATION_FROM_EMAIL` | No | `alerts@courtvision.dev` | From address for notification emails |
| `LOG_LEVEL` | No | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `LOG_FORMAT` | No | `json` | Log format: `json` (production) or `console` (development) |
| `DEVELOPMENT_MODE` | No | `false` | Enables development-only behavior |
| `RETRY_MAX_ATTEMPTS` | No | `3` | Max retry attempts for external API calls |
| `CIRCUIT_BREAKER_THRESHOLD` | No | `5` | Failures before circuit opens |
| `HTTP_TIMEOUT` | No | `30` | HTTP client timeout in seconds |

---

## API Endpoints

All routes are prefixed under `/v1`. The server exposes two route groups:

### Public Routes (`/v1/...`)

No authentication required. Rate-limited to **100 requests/minute** per IP.

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/rankings/` | Fantasy player rankings (full season or rolling N-day window) |
| `GET` | `/v1/players/` | List NBA players with optional filters (team, position, name) |
| `GET` | `/v1/players/stats` | Player stats by ESPN ID, NBA ID, or name+team |
| `GET` | `/v1/players/{player_id}/stats` | Player stats by NBA player ID |
| `GET` | `/v1/players/{player_id}/games` | Recent game log (box scores) |
| `GET` | `/v1/players/{player_id}/trends` | Performance trends and ownership change |
| `GET` | `/v1/players/{player_id}/percentiles` | Percentile ranks vs. all qualifying players |
| `GET` | `/v1/players/{player_id}/status` | Injury/availability status |
| `GET` | `/v1/players/{player_id}/ownership` | Fantasy ownership percentage + trend |
| `GET` | `/v1/games/{game_date}` | NBA games on a specific date (with live overlay for today) |
| `GET` | `/v1/teams/` | NBA team list and schedules |
| `GET` | `/v1/ownership/trending` | Players with significant ownership changes (velocity-ranked) |
| `GET` | `/v1/schedule/weeks` | All NBA schedule weeks with dates and current week |
| `GET` | `/v1/live/players/today` | Live box score stats for all players with games today (~60s cadence) |
| `GET` | `/v1/live/schedule/today` | Today's game schedule (first tip-off time, used by cron-runner) |
| `GET` | `/v1/live/scoreboard` | Live scoreboard from NBA CDN (real-time game status) |

#### Analytics Routes (API Key Required)

These routes live under `/v1/analytics/` and require an `X-API-Key` header with the `analytics` scope. Rate-limited to **1000 requests/minute** per API key.

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/analytics/generate-lineup` | Generate optimized lineup using stored team credentials |
| `GET` | `/v1/analytics/breakout-streamers` | Breakout streamer candidates (injury-driven opportunity players) |

---

### Internal Routes (`/v1/internal/...`)

Require a valid **Clerk JWT** in the `Authorization: Bearer <token>` header.

#### Teams

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/internal/teams/` | Get all saved teams for the authenticated user |
| `POST` | `/v1/internal/teams/add` | Save a new ESPN/Yahoo team |
| `DELETE` | `/v1/internal/teams/remove` | Remove a saved team |
| `PUT` | `/v1/internal/teams/update` | Update team credentials |
| `GET` | `/v1/internal/teams/view` | Fetch live roster from ESPN/Yahoo for a saved team |
| `GET` | `/v1/internal/teams/{team_id}/insights` | Category strengths, score history, streamer recommendations |

#### Matchups

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/internal/matchups/current` | Current week matchup with projections (pass league credentials) |
| `GET` | `/v1/internal/matchups/current/{team_id}` | Current matchup for a saved team |
| `GET` | `/v1/internal/matchups/live/{team_id}` | Live matchup with per-player in-game stats (ESPN baseline + live overlay) |
| `GET` | `/v1/internal/matchups/history/{team_id}` | Daily score history for charting matchup progression |
| `GET` | `/v1/internal/matchups/week/{team_id}` | All days in the current matchup period in one request |
| `GET` | `/v1/internal/matchups/daily/{team_id}` | Per-day drill-down (past box scores, today's live stats, future schedules) |

#### Lineups

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/internal/lineups/generate` | Generate optimized lineup via the features service |
| `GET` | `/v1/internal/lineups` | Get saved lineups for a team |
| `PUT` | `/v1/internal/lineups/save` | Save a lineup |
| `DELETE` | `/v1/internal/lineups/remove` | Delete a saved lineup |

#### Streamers

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/internal/streamers/breakout` | Breakout candidates for logged-in users |
| `POST` | `/v1/internal/streamers/find` | Rank free agents for streaming (week or daily mode) |

#### Notifications

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/internal/notifications/preferences` | Get notification preferences |
| `PUT` | `/v1/internal/notifications/preferences` | Update notification preferences |
| `GET` | `/v1/internal/notifications/team-preferences` | List team-level preference overrides |
| `PUT` | `/v1/internal/notifications/team-preferences/{team_id}` | Upsert a team-level override |
| `DELETE` | `/v1/internal/notifications/team-preferences/{team_id}` | Remove a team-level override |
| `GET` | `/v1/internal/notifications/check-lineup/{team_id}` | On-demand lineup issue check (no email sent) |
| `POST` | `/v1/internal/notifications/send-test/{team_id}` | Force-send a test lineup alert email |
| `GET` | `/v1/internal/notifications/history` | Recent notification log for the user |

#### API Keys

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/internal/api-keys/` | List active API keys for the user |
| `POST` | `/v1/internal/api-keys/` | Create a new API key (raw key returned once) |
| `DELETE` | `/v1/internal/api-keys/{key_id}` | Revoke an API key (soft-delete) |

#### Other Internal Routes

| Module | Routes |
|---|---|
| `auth` | Clerk user auth helpers |
| `users` | User profile management |
| `espn` | ESPN OAuth flow and data fetch |
| `yahoo` | Yahoo OAuth callback and token management |

---

## Authentication

### Clerk JWT (internal routes)

Internal routes (`/v1/internal/`) use Clerk's RS256 JWT. The backend fetches Clerk's JWKS on first request and caches the public keys. On token expiry or key rotation, the cache is cleared and re-fetched.

**Usage in protected routes:**

```python
from core.clerk_auth import get_current_user

@router.get("/my-endpoint")
async def handler(current_user: dict = Depends(get_current_user)):
    clerk_user_id = current_user["clerk_user_id"]
    email = current_user["email"]
```

The `clerk_user_id` is used to look up or lazily create a local `User` row via `UserSyncService.get_or_create_user()`.

### Scoped API Keys (analytics routes)

The `/v1/analytics/` endpoints use `X-API-Key` header authentication. Keys are scoped (e.g., `analytics`) and stored as bcrypt hashes. Rate limit is tracked per key prefix.

```bash
curl -H "X-API-Key: cv_live_..." https://api.courtvision.dev/v1/analytics/breakout-streamers
```

Authenticated users can manage their own keys via `/v1/internal/api-keys/`.

---

## Response Format

All endpoints follow the `BaseApiResponse<T>` pattern:

```json
{
  "status": "success",
  "message": "Human-readable description",
  "data": { ... }
}
```

`status` values: `success`, `error`, `not_found`, `rate_limited`

---

## Resilience

External API calls (nba_api, ESPN, Yahoo) are protected by:

- **Retry with exponential backoff** (`@with_retry`) via tenacity — retries on `RetryableError` subclasses (rate limit, network, 5xx)
- **Circuit breakers** (`nba_api_circuit`, `espn_api_circuit`) via circuitbreaker — opens after 5 failures, recovers after 60s
- **`ResilientHTTPClient`** — composable HTTP client with both retry and circuit breaker support

Error classification: `RateLimitError` (429), `ServerError` (5xx), `NetworkError` (timeout/connection), `ClientError` (4xx, not retried).

---

## Middleware Stack

Middleware is applied outermost-first (first added = outermost wrapper):

1. `CorrelationMiddleware` — attaches a `X-Correlation-ID` to every request/response
2. `DatabaseMiddleware` — manages Peewee connection lifecycle per request
3. CORS + GZip (via `setup_middleware`)

---

## How It Fits Into Court Vision

```
┌─────────────┐     Clerk JWT      ┌─────────────────────┐
│   Frontend  │ ─────────────────► │  backend (port 8000) │
│  (Next.js)  │ ◄───────────────── │  FastAPI + Peewee    │
└─────────────┘   JSON responses   └──────────┬──────────┘
                                              │  reads
                                   ┌──────────▼──────────┐
                                   │     PostgreSQL        │
                                   │  nba.* + stats_s2.*  │
                                   └──────────▲──────────┘
                                              │  writes
                                   ┌──────────┴──────────┐
                                   │  data-platform       │
                                   │  (port 8001, ETL)    │
                                   └──────────▲──────────┘
                                              │  triggers
                                   ┌──────────┴──────────┐
                                   │  cron-runner (Go)    │
                                   └─────────────────────┘
```

- **cron-runner** triggers ETL jobs in **data-platform** on schedule
- **data-platform** fetches from NBA API / ESPN and writes to PostgreSQL
- **backend** reads from PostgreSQL and serves user-facing API routes
- For lineup generation, backend proxies to the **features** service (Go, port 8080)

The backend has no pipeline or ETL code — it is a pure read API. The `pipelines/extractors/` stub exists only for the live scoreboard and games endpoints that need direct NBA CDN access without going through the data-platform pipeline.

---

## Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=. --cov-report=term-missing
```

Test configuration is in `pytest.ini`.
