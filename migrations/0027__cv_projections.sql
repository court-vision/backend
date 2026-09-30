-- Court Vision's own projection: its inputs (docs/CV_RANKINGS_PLAN.md §2.1-2.2).
--
-- nba.player_history: one row per player per NBA regular season — totals, age,
-- usage — from nba_api's LeagueDashPlayerStats. The projection reads the last
-- three seasons, and the age curve is fitted on all of them. Deliberately NOT
-- nba.player_season_stats: that table's walk-back (baseline_records) would put
-- any player with an old row back on the draft board, retired or not.
-- player_id is nba_api's PLAYER_ID, the nba.players.id space, but carries no
-- foreign key: history holds players who never reached nba.players.
--
-- nba.projection_adjustments: the curated layer — per-player input changes a
-- box score cannot see (a role after a trade, a return date, a second-year
-- jump). Append-only: an edit is a new row that supersedes the old one, so the
-- history of every judgment call is kept. Minutes and games are absolute
-- targets, not deltas, so an adjustment cannot compound when the projection is
-- re-run or when ESPN's line moves underneath it.

CREATE TABLE IF NOT EXISTS nba.player_history (
    player_id       INTEGER      NOT NULL,
    season          VARCHAR(7)   NOT NULL,              -- '2025-26'
    player_name     VARCHAR(100) NOT NULL,
    team            VARCHAR(5),                         -- last team that season
    age             NUMERIC(4, 1),
    from_year       SMALLINT,                           -- first NBA season (start year)
    gp              SMALLINT     NOT NULL,
    min             NUMERIC(7, 1) NOT NULL DEFAULT 0,   -- totals from here on
    pts             SMALLINT     NOT NULL DEFAULT 0,
    reb             SMALLINT     NOT NULL DEFAULT 0,
    ast             SMALLINT     NOT NULL DEFAULT 0,
    stl             SMALLINT     NOT NULL DEFAULT 0,
    blk             SMALLINT     NOT NULL DEFAULT 0,
    tov             SMALLINT     NOT NULL DEFAULT 0,
    fgm             SMALLINT     NOT NULL DEFAULT 0,
    fga             SMALLINT     NOT NULL DEFAULT 0,
    fg3m            SMALLINT     NOT NULL DEFAULT 0,
    fg3a            SMALLINT     NOT NULL DEFAULT 0,
    ftm             SMALLINT     NOT NULL DEFAULT 0,
    fta             SMALLINT     NOT NULL DEFAULT 0,
    oreb            SMALLINT     NOT NULL DEFAULT 0,
    dreb            SMALLINT     NOT NULL DEFAULT 0,
    dd2             SMALLINT     NOT NULL DEFAULT 0,
    td3             SMALLINT     NOT NULL DEFAULT 0,
    usg_pct         NUMERIC(5, 3),
    pipeline_run_id UUID,
    created_at      TIMESTAMP    NOT NULL DEFAULT now(),
    updated_at      TIMESTAMP    NOT NULL DEFAULT now(),
    PRIMARY KEY (player_id, season)
);

CREATE INDEX IF NOT EXISTS player_history_season_idx ON nba.player_history (season);

CREATE TABLE IF NOT EXISTS nba.projection_adjustments (
    id              SERIAL       PRIMARY KEY,
    player_id       INTEGER      NOT NULL REFERENCES nba.players (id) ON DELETE CASCADE,
    season          VARCHAR(7)   NOT NULL,              -- the season being projected
    kind            VARCHAR(16)  NOT NULL,
    minutes         NUMERIC(4, 1),                      -- target minutes per game
    games           SMALLINT,                           -- target games played
    return_date     DATE,                               -- games derived from the team schedule after it
    usage           NUMERIC(4, 3),                      -- multiplier on the scoring/usage stats together
    rates           JSONB,                              -- per-stat multipliers, e.g. {"blk": 1.1}
    note            TEXT         NOT NULL,
    source_url      TEXT,
    author          VARCHAR(64)  NOT NULL,
    created_at      TIMESTAMP    NOT NULL DEFAULT now(),
    -- Deferred so an edit can run in the only order the unique index below
    -- allows: reserve the new id, point the old row at it, then insert.
    superseded_by   INTEGER      REFERENCES nba.projection_adjustments (id) DEFERRABLE INITIALLY DEFERRED,
    retired_at      TIMESTAMP,                          -- withdrawn without a replacement
    CONSTRAINT projection_adjustments_kind_check CHECK (
        kind IN ('year2', 'trade', 'role', 'injury_return', 'injury_current', 'age', 'other')
    ),
    CONSTRAINT projection_adjustments_minutes_check CHECK (minutes IS NULL OR (minutes >= 0 AND minutes <= 48)),
    CONSTRAINT projection_adjustments_games_check CHECK (games IS NULL OR (games >= 0 AND games <= 82)),
    CONSTRAINT projection_adjustments_usage_check CHECK (usage IS NULL OR (usage > 0 AND usage <= 2)),
    CONSTRAINT projection_adjustments_changes_something CHECK (
        minutes IS NOT NULL OR games IS NOT NULL OR return_date IS NOT NULL
        OR usage IS NOT NULL OR rates IS NOT NULL
    )
);

-- One live adjustment per player per season: the row nothing supersedes and
-- nobody retired. Editing, in one transaction: new_id = nextval(the id
-- sequence); UPDATE old SET superseded_by = new_id; INSERT the new row with id
-- new_id. The foreign key is checked at commit, when the row exists.
CREATE UNIQUE INDEX IF NOT EXISTS projection_adjustments_one_active
    ON nba.projection_adjustments (player_id, season)
    WHERE superseded_by IS NULL AND retired_at IS NULL;
