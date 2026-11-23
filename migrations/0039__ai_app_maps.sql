-- Migration 0039: the app map, CV AI's map of the website (docs/APP_MAP_PLAN.md § 4).
--
-- One row per crawled frontend build: the frontend's App Map workflow crawls
-- each commit on main (and once a night during games) and uploads the map to
-- PUT /v1/internal/app-map/maps/{build}. The AI desk reads the map of the build
-- the user has open, or the newest when that build's crawl hasn't landed yet.
-- `map` is the crawler's JSON as uploaded; `stats` its counts, for a glance.
-- Maps crawled more than 30 days ago are pruned at each upload, except the
-- newest. Backend-only; data-platform never reads or writes it.

CREATE TABLE IF NOT EXISTS usr.ai_app_maps (
    build        text                     PRIMARY KEY,
    crawled_at   timestamp with time zone NOT NULL,
    map          jsonb                    NOT NULL,
    stats        jsonb                    NOT NULL DEFAULT '{}'::jsonb,
    uploaded_at  timestamp with time zone NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ai_app_maps_crawled_at_idx
    ON usr.ai_app_maps (crawled_at DESC);
