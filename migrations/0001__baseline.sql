-- Baseline schema: pg_dump --schema-only of production (PostgreSQL 16), 2026-08-23,
-- cleaned per migrations/README.md. Databases that already had this schema were
-- adopted by marking this migration applied; fresh databases execute it.

CREATE SCHEMA IF NOT EXISTS nba;

CREATE SCHEMA IF NOT EXISTS stats_s2;

CREATE SCHEMA IF NOT EXISTS usr;

CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA public;

SET default_tablespace = '';

SET default_table_access_method = heap;

CREATE TABLE nba.breakout_candidates (
    id integer NOT NULL,
    injured_player_id integer NOT NULL,
    injured_avg_min numeric(5,1) NOT NULL,
    injury_status character varying(20) NOT NULL,
    expected_return date,
    beneficiary_player_id integer NOT NULL,
    team_id character varying(3),
    depth_rank smallint NOT NULL,
    beneficiary_avg_min numeric(5,1) NOT NULL,
    beneficiary_avg_fpts numeric(6,1) NOT NULL,
    projected_min_boost numeric(4,1) NOT NULL,
    opp_min_avg numeric(5,1),
    opp_fpts_avg numeric(6,1),
    opp_game_count smallint NOT NULL,
    breakout_score numeric(6,1) NOT NULL,
    as_of_date date NOT NULL,
    pipeline_run_id uuid,
    created_at timestamp without time zone NOT NULL
);

CREATE SEQUENCE nba.breakout_candidates_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE nba.breakout_candidates_id_seq OWNED BY nba.breakout_candidates.id;

CREATE TABLE nba.cron_job_runs (
    id uuid NOT NULL,
    job_name character varying(50) NOT NULL,
    triggered_at timestamp without time zone NOT NULL,
    completed_at timestamp without time zone NOT NULL,
    duration_ms integer NOT NULL,
    result character varying(20) NOT NULL,
    http_status integer,
    attempts integer NOT NULL,
    error_message text,
    response_snippet text
);

CREATE TABLE nba.data_quality_checks (
    id integer NOT NULL,
    run_id uuid NOT NULL,
    check_name character varying(100) NOT NULL,
    status character varying(20) NOT NULL,
    severity character varying(20) NOT NULL,
    failures integer NOT NULL,
    message text,
    details_json text,
    duration_ms integer,
    created_at timestamp without time zone NOT NULL
);

CREATE SEQUENCE nba.data_quality_checks_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE nba.data_quality_checks_id_seq OWNED BY nba.data_quality_checks.id;

CREATE TABLE nba.data_quality_runs (
    id uuid NOT NULL,
    status character varying(20) NOT NULL,
    triggered_by character varying(20) NOT NULL,
    started_at timestamp without time zone NOT NULL,
    completed_at timestamp without time zone,
    total_checks integer NOT NULL,
    passed_checks integer NOT NULL,
    failed_checks integer NOT NULL,
    error_message text,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);

CREATE TABLE nba.games (
    game_id character varying(20) NOT NULL,
    game_date date NOT NULL,
    season character varying(7) NOT NULL,
    home_team_id character varying(3) NOT NULL,
    away_team_id character varying(3) NOT NULL,
    home_score integer,
    away_score integer,
    status character varying(20) NOT NULL,
    arena character varying(100),
    attendance integer,
    updated_at timestamp without time zone NOT NULL,
    start_time_et time without time zone
);

CREATE TABLE nba.live_game_score_snapshots (
    id integer NOT NULL,
    game_id character varying(20) NOT NULL,
    game_date date NOT NULL,
    home_team character varying(10) NOT NULL,
    away_team character varying(10) NOT NULL,
    home_score smallint NOT NULL,
    away_score smallint NOT NULL,
    period smallint,
    game_clock character varying(20),
    game_status smallint NOT NULL,
    captured_at timestamp without time zone NOT NULL,
    pipeline_run_id uuid
);

CREATE SEQUENCE nba.live_game_score_snapshots_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE nba.live_game_score_snapshots_id_seq OWNED BY nba.live_game_score_snapshots.id;

CREATE TABLE nba.live_player_stats (
    id integer NOT NULL,
    player_id integer NOT NULL,
    game_id character varying(20) NOT NULL,
    game_date date NOT NULL,
    period smallint,
    game_clock character varying(20),
    game_status smallint NOT NULL,
    fpts smallint NOT NULL,
    pts smallint NOT NULL,
    reb smallint NOT NULL,
    ast smallint NOT NULL,
    stl smallint NOT NULL,
    blk smallint NOT NULL,
    tov smallint NOT NULL,
    min integer NOT NULL,
    fgm smallint NOT NULL,
    fga smallint NOT NULL,
    fg3m smallint NOT NULL,
    fg3a smallint NOT NULL,
    ftm smallint NOT NULL,
    fta smallint NOT NULL,
    last_updated timestamp without time zone NOT NULL,
    pipeline_run_id uuid
);

CREATE SEQUENCE nba.live_player_stats_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE nba.live_player_stats_id_seq OWNED BY nba.live_player_stats.id;

CREATE TABLE nba.pipeline_runs (
    id uuid NOT NULL,
    pipeline_name character varying(50) NOT NULL,
    started_at timestamp with time zone NOT NULL,
    completed_at timestamp with time zone,
    status character varying(20) NOT NULL,
    records_processed integer DEFAULT 0,
    error_message text
);

CREATE TABLE nba.player_advanced_stats (
    id integer NOT NULL,
    player_id integer NOT NULL,
    team_id character varying(3),
    as_of_date date NOT NULL,
    season character varying(7) NOT NULL,
    gp smallint,
    min numeric(6,1),
    off_rating numeric(5,1),
    def_rating numeric(5,1),
    net_rating numeric(5,1),
    ts_pct numeric(5,3),
    efg_pct numeric(5,3),
    usg_pct numeric(6,3),
    ast_pct numeric(6,3),
    ast_to_tov numeric(5,2),
    ast_ratio numeric(6,3),
    reb_pct numeric(6,3),
    oreb_pct numeric(6,3),
    dreb_pct numeric(6,3),
    tov_pct numeric(6,3),
    pace numeric(5,1),
    pie numeric(5,3),
    poss integer,
    plus_minus numeric(6,1),
    pipeline_run_id uuid,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);

CREATE SEQUENCE nba.player_advanced_stats_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE nba.player_advanced_stats_id_seq OWNED BY nba.player_advanced_stats.id;

CREATE TABLE nba.player_game_stats (
    id integer NOT NULL,
    player_id integer NOT NULL,
    team_id character varying(3),
    game_date date NOT NULL,
    fpts smallint NOT NULL,
    pts smallint NOT NULL,
    reb smallint NOT NULL,
    ast smallint NOT NULL,
    stl smallint NOT NULL,
    blk smallint NOT NULL,
    tov smallint NOT NULL,
    min integer NOT NULL,
    fgm smallint NOT NULL,
    fga smallint NOT NULL,
    fg3m smallint NOT NULL,
    fg3a smallint NOT NULL,
    ftm smallint NOT NULL,
    fta smallint NOT NULL,
    pipeline_run_id uuid,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);

CREATE SEQUENCE nba.player_game_stats_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE nba.player_game_stats_id_seq OWNED BY nba.player_game_stats.id;

CREATE TABLE nba.player_injuries (
    id integer NOT NULL,
    player_id integer NOT NULL,
    report_date date NOT NULL,
    status character varying(20) NOT NULL,
    injury_type character varying(100),
    injury_detail text,
    expected_return date,
    pipeline_run_id uuid,
    created_at timestamp without time zone NOT NULL
);

CREATE SEQUENCE nba.player_injuries_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE nba.player_injuries_id_seq OWNED BY nba.player_injuries.id;

CREATE TABLE nba.player_ownership (
    id integer NOT NULL,
    player_id integer NOT NULL,
    snapshot_date date NOT NULL,
    rost_pct numeric(7,4) NOT NULL,
    pipeline_run_id uuid,
    created_at timestamp without time zone NOT NULL
);

CREATE SEQUENCE nba.player_ownership_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE nba.player_ownership_id_seq OWNED BY nba.player_ownership.id;

CREATE TABLE nba.player_profiles (
    player_id integer NOT NULL,
    first_name character varying(50),
    last_name character varying(50),
    birthdate date,
    height character varying(10),
    weight integer,
    "position" character varying(20),
    jersey_number character varying(5),
    team_id character varying(3),
    draft_year integer,
    draft_round integer,
    draft_number integer,
    season_exp integer,
    country character varying(50),
    school character varying(100),
    from_year integer,
    to_year integer,
    updated_at timestamp without time zone NOT NULL
);

CREATE TABLE nba.player_rolling_stats (
    id integer NOT NULL,
    player_id integer NOT NULL,
    team_id character varying(3),
    as_of_date date NOT NULL,
    window_days smallint NOT NULL,
    gp smallint NOT NULL,
    fpts numeric(6,2) NOT NULL,
    pts numeric(5,2) NOT NULL,
    reb numeric(5,2) NOT NULL,
    ast numeric(5,2) NOT NULL,
    stl numeric(5,2) NOT NULL,
    blk numeric(5,2) NOT NULL,
    tov numeric(5,2) NOT NULL,
    min numeric(5,2) NOT NULL,
    fgm numeric(5,2) NOT NULL,
    fga numeric(5,2) NOT NULL,
    fg_pct numeric(5,4) NOT NULL,
    fg3m numeric(5,2) NOT NULL,
    fg3a numeric(5,2) NOT NULL,
    fg3_pct numeric(5,4) NOT NULL,
    ftm numeric(5,2) NOT NULL,
    fta numeric(5,2) NOT NULL,
    ft_pct numeric(5,4) NOT NULL,
    pipeline_run_id uuid,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);

CREATE SEQUENCE nba.player_rolling_stats_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE nba.player_rolling_stats_id_seq OWNED BY nba.player_rolling_stats.id;

CREATE TABLE nba.player_season_stats (
    id integer NOT NULL,
    player_id integer NOT NULL,
    team_id character varying(3),
    as_of_date date NOT NULL,
    season character varying(7) NOT NULL,
    gp smallint NOT NULL,
    fpts integer NOT NULL,
    pts integer NOT NULL,
    reb integer NOT NULL,
    ast integer NOT NULL,
    stl smallint NOT NULL,
    blk smallint NOT NULL,
    tov smallint NOT NULL,
    min integer NOT NULL,
    fgm integer NOT NULL,
    fga integer NOT NULL,
    fg3m integer NOT NULL,
    fg3a integer NOT NULL,
    ftm integer NOT NULL,
    fta integer NOT NULL,
    rank smallint,
    rost_pct numeric(7,4),
    pipeline_run_id uuid,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);

CREATE SEQUENCE nba.player_season_stats_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE nba.player_season_stats_id_seq OWNED BY nba.player_season_stats.id;

CREATE TABLE nba.players (
    id integer NOT NULL,
    espn_id integer,
    name character varying(100) NOT NULL,
    name_normalized character varying(100) NOT NULL,
    "position" character varying(10),
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);

CREATE TABLE nba.playoff_series (
    id integer NOT NULL,
    season character varying(7) NOT NULL,
    series_id character varying(20) NOT NULL,
    conference character varying(10) NOT NULL,
    round_num smallint NOT NULL,
    top_seed_team_id integer,
    top_seed_name character varying(50),
    top_seed_abbr character varying(5) NOT NULL,
    top_seed_wins smallint NOT NULL,
    bottom_seed_team_id integer,
    bottom_seed_name character varying(50),
    bottom_seed_abbr character varying(5) NOT NULL,
    bottom_seed_wins smallint NOT NULL,
    series_complete boolean NOT NULL,
    series_leader_abbr character varying(5),
    updated_at timestamp without time zone NOT NULL
);

CREATE SEQUENCE nba.playoff_series_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE nba.playoff_series_id_seq OWNED BY nba.playoff_series.id;

CREATE TABLE nba.team_stats (
    id integer NOT NULL,
    team_id character varying(3) NOT NULL,
    as_of_date date NOT NULL,
    season character varying(7) NOT NULL,
    gp smallint,
    w smallint,
    l smallint,
    w_pct numeric(5,3),
    pts numeric(5,1),
    reb numeric(5,1),
    ast numeric(5,1),
    stl numeric(5,1),
    blk numeric(5,1),
    tov numeric(5,1),
    fg_pct numeric(5,3),
    fg3_pct numeric(5,3),
    ft_pct numeric(5,3),
    off_rating numeric(5,1),
    def_rating numeric(5,1),
    net_rating numeric(5,1),
    pace numeric(5,1),
    ts_pct numeric(5,3),
    efg_pct numeric(5,3),
    ast_pct numeric(6,3),
    oreb_pct numeric(6,3),
    dreb_pct numeric(6,3),
    reb_pct numeric(6,3),
    tov_pct numeric(6,3),
    pie numeric(5,3),
    pipeline_run_id uuid,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);

CREATE SEQUENCE nba.team_stats_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE nba.team_stats_id_seq OWNED BY nba.team_stats.id;

CREATE TABLE nba.teams (
    id character varying(3) NOT NULL,
    name character varying(50) NOT NULL,
    conference character varying(4) NOT NULL,
    division character varying(20) NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);

CREATE TABLE stats_s2.cumulative_player_stats (
    id integer NOT NULL,
    name character varying(50) NOT NULL,
    team character varying(3) NOT NULL,
    date date NOT NULL,
    fpts smallint NOT NULL,
    pts smallint NOT NULL,
    reb smallint NOT NULL,
    ast smallint NOT NULL,
    stl smallint NOT NULL,
    blk smallint NOT NULL,
    tov smallint NOT NULL,
    fgm smallint NOT NULL,
    fga smallint NOT NULL,
    fg3m smallint NOT NULL,
    fg3a smallint NOT NULL,
    ftm smallint NOT NULL,
    fta smallint NOT NULL,
    min integer NOT NULL,
    gp smallint NOT NULL,
    rank smallint,
    rost_pct numeric(7,4),
    pipeline_run_id uuid,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE stats_s2.daily_matchup_scores (
    team_id integer NOT NULL,
    team_name character varying(100) NOT NULL,
    matchup_period smallint NOT NULL,
    opponent_team_name character varying(100) NOT NULL,
    date date NOT NULL,
    day_of_matchup smallint NOT NULL,
    current_score numeric(8,2) NOT NULL,
    opponent_current_score numeric(8,2) NOT NULL,
    pipeline_run_id uuid,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    scoring_period_id smallint
);

CREATE TABLE stats_s2.daily_player_stats (
    id integer,
    name character varying(50) NOT NULL,
    team character(3) NOT NULL,
    date date,
    fpts smallint NOT NULL,
    pts smallint NOT NULL,
    reb smallint NOT NULL,
    ast smallint NOT NULL,
    stl smallint NOT NULL,
    blk smallint NOT NULL,
    tov smallint NOT NULL,
    fgm smallint NOT NULL,
    fga smallint NOT NULL,
    fg3m smallint NOT NULL,
    fg3a smallint NOT NULL,
    ftm smallint NOT NULL,
    fta smallint NOT NULL,
    min integer NOT NULL,
    rost_pct numeric(7,4) DEFAULT NULL::numeric,
    espn_id integer,
    name_normalized character varying(50),
    pipeline_run_id uuid,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);

CREATE VIEW stats_s2.standings AS
 SELECT id,
    curr_rank,
    name,
    team,
    fpts,
    round(((1.0 * (fpts)::numeric) / (gp)::numeric), 2) AS avg_fpts,
    (COALESCE(prev_rank, curr_rank) - curr_rank) AS rank_change
   FROM ( SELECT DISTINCT ON (cumulative_player_stats.id) cumulative_player_stats.id,
            cumulative_player_stats.name,
            cumulative_player_stats.gp,
            cumulative_player_stats.team,
            cumulative_player_stats.fpts,
            cumulative_player_stats.rank AS curr_rank,
            lead(cumulative_player_stats.rank, 5) OVER (PARTITION BY cumulative_player_stats.id ORDER BY cumulative_player_stats.date DESC) AS prev_rank
           FROM stats_s2.cumulative_player_stats
          ORDER BY cumulative_player_stats.id, cumulative_player_stats.date DESC) unnamed_subquery
  ORDER BY curr_rank;

CREATE TABLE usr.api_keys (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id integer,
    key_hash character varying(64) NOT NULL,
    key_prefix character varying(11) NOT NULL,
    name character varying(100) NOT NULL,
    scopes text[] DEFAULT '{}'::text[],
    rate_limit integer DEFAULT 1000,
    created_at timestamp with time zone DEFAULT now(),
    expires_at timestamp with time zone,
    last_used_at timestamp with time zone,
    is_active boolean DEFAULT true
);

CREATE TABLE usr.lineups (
    lineup_id integer NOT NULL,
    team_id integer NOT NULL,
    lineup_info text NOT NULL,
    lineup_hash character varying(32) NOT NULL
);

CREATE SEQUENCE usr.lineups_lineup_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE usr.lineups_lineup_id_seq OWNED BY usr.lineups.lineup_id;

CREATE TABLE usr.notification_log (
    id uuid NOT NULL,
    user_id integer NOT NULL,
    team_id integer NOT NULL,
    notification_type character varying(50) NOT NULL,
    notification_date date NOT NULL,
    alert_data text,
    status character varying(20) NOT NULL,
    resend_message_id character varying(100),
    error_message text,
    created_at timestamp without time zone NOT NULL,
    sent_at timestamp without time zone
);

CREATE TABLE usr.notification_preferences (
    id integer NOT NULL,
    user_id integer NOT NULL,
    lineup_alerts_enabled boolean NOT NULL,
    alert_benched_starters boolean NOT NULL,
    alert_active_non_playing boolean NOT NULL,
    alert_injured_active boolean NOT NULL,
    alert_minutes_before integer NOT NULL,
    email character varying(255),
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);

CREATE SEQUENCE usr.notification_preferences_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE usr.notification_preferences_id_seq OWNED BY usr.notification_preferences.id;

CREATE TABLE usr.notification_team_preferences (
    id integer NOT NULL,
    user_id integer NOT NULL,
    team_id integer NOT NULL,
    lineup_alerts_enabled boolean,
    alert_benched_starters boolean,
    alert_active_non_playing boolean,
    alert_injured_active boolean,
    alert_minutes_before integer,
    email character varying(255),
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);

CREATE SEQUENCE usr.notification_team_preferences_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE usr.notification_team_preferences_id_seq OWNED BY usr.notification_team_preferences.id;

CREATE TABLE usr.teams (
    team_id integer NOT NULL,
    user_id integer NOT NULL,
    team_identifier character varying(255) NOT NULL,
    league_info text NOT NULL
);

CREATE SEQUENCE usr.teams_team_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE usr.teams_team_id_seq OWNED BY usr.teams.team_id;

CREATE TABLE usr.users (
    user_id integer NOT NULL,
    email character varying(255) NOT NULL,
    password character varying(255),
    created_at timestamp without time zone,
    clerk_user_id character varying(255)
);

CREATE SEQUENCE usr.users_user_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE usr.users_user_id_seq OWNED BY usr.users.user_id;

CREATE TABLE usr.verifications (
    id integer NOT NULL,
    email character varying(255) NOT NULL,
    code character varying(6) NOT NULL,
    hashed_password character varying(255) NOT NULL,
    "timestamp" integer NOT NULL,
    type character varying(50) NOT NULL
);

CREATE SEQUENCE usr.verifications_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE usr.verifications_id_seq OWNED BY usr.verifications.id;

ALTER TABLE ONLY nba.breakout_candidates ALTER COLUMN id SET DEFAULT nextval('nba.breakout_candidates_id_seq'::regclass);

ALTER TABLE ONLY nba.data_quality_checks ALTER COLUMN id SET DEFAULT nextval('nba.data_quality_checks_id_seq'::regclass);

ALTER TABLE ONLY nba.live_game_score_snapshots ALTER COLUMN id SET DEFAULT nextval('nba.live_game_score_snapshots_id_seq'::regclass);

ALTER TABLE ONLY nba.live_player_stats ALTER COLUMN id SET DEFAULT nextval('nba.live_player_stats_id_seq'::regclass);

ALTER TABLE ONLY nba.player_advanced_stats ALTER COLUMN id SET DEFAULT nextval('nba.player_advanced_stats_id_seq'::regclass);

ALTER TABLE ONLY nba.player_game_stats ALTER COLUMN id SET DEFAULT nextval('nba.player_game_stats_id_seq'::regclass);

ALTER TABLE ONLY nba.player_injuries ALTER COLUMN id SET DEFAULT nextval('nba.player_injuries_id_seq'::regclass);

ALTER TABLE ONLY nba.player_ownership ALTER COLUMN id SET DEFAULT nextval('nba.player_ownership_id_seq'::regclass);

ALTER TABLE ONLY nba.player_rolling_stats ALTER COLUMN id SET DEFAULT nextval('nba.player_rolling_stats_id_seq'::regclass);

ALTER TABLE ONLY nba.player_season_stats ALTER COLUMN id SET DEFAULT nextval('nba.player_season_stats_id_seq'::regclass);

ALTER TABLE ONLY nba.playoff_series ALTER COLUMN id SET DEFAULT nextval('nba.playoff_series_id_seq'::regclass);

ALTER TABLE ONLY nba.team_stats ALTER COLUMN id SET DEFAULT nextval('nba.team_stats_id_seq'::regclass);

ALTER TABLE ONLY usr.lineups ALTER COLUMN lineup_id SET DEFAULT nextval('usr.lineups_lineup_id_seq'::regclass);

ALTER TABLE ONLY usr.notification_preferences ALTER COLUMN id SET DEFAULT nextval('usr.notification_preferences_id_seq'::regclass);

ALTER TABLE ONLY usr.notification_team_preferences ALTER COLUMN id SET DEFAULT nextval('usr.notification_team_preferences_id_seq'::regclass);

ALTER TABLE ONLY usr.teams ALTER COLUMN team_id SET DEFAULT nextval('usr.teams_team_id_seq'::regclass);

ALTER TABLE ONLY usr.users ALTER COLUMN user_id SET DEFAULT nextval('usr.users_user_id_seq'::regclass);

ALTER TABLE ONLY usr.verifications ALTER COLUMN id SET DEFAULT nextval('usr.verifications_id_seq'::regclass);

ALTER TABLE ONLY nba.breakout_candidates
    ADD CONSTRAINT breakout_candidates_pkey PRIMARY KEY (id);

ALTER TABLE ONLY nba.cron_job_runs
    ADD CONSTRAINT cron_job_runs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY nba.data_quality_checks
    ADD CONSTRAINT data_quality_checks_pkey PRIMARY KEY (id);

ALTER TABLE ONLY nba.data_quality_runs
    ADD CONSTRAINT data_quality_runs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY nba.games
    ADD CONSTRAINT games_pkey PRIMARY KEY (game_id);

ALTER TABLE ONLY nba.live_game_score_snapshots
    ADD CONSTRAINT live_game_score_snapshots_pkey PRIMARY KEY (id);

ALTER TABLE ONLY nba.live_player_stats
    ADD CONSTRAINT live_player_stats_pkey PRIMARY KEY (id);

ALTER TABLE ONLY nba.pipeline_runs
    ADD CONSTRAINT pipeline_runs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY nba.player_advanced_stats
    ADD CONSTRAINT player_advanced_stats_pkey PRIMARY KEY (id);

ALTER TABLE ONLY nba.player_game_stats
    ADD CONSTRAINT player_game_stats_pkey PRIMARY KEY (id);

ALTER TABLE ONLY nba.player_injuries
    ADD CONSTRAINT player_injuries_pkey PRIMARY KEY (id);

ALTER TABLE ONLY nba.player_ownership
    ADD CONSTRAINT player_ownership_pkey PRIMARY KEY (id);

ALTER TABLE ONLY nba.player_profiles
    ADD CONSTRAINT player_profiles_pkey PRIMARY KEY (player_id);

ALTER TABLE ONLY nba.player_rolling_stats
    ADD CONSTRAINT player_rolling_stats_pkey PRIMARY KEY (id);

ALTER TABLE ONLY nba.player_season_stats
    ADD CONSTRAINT player_season_stats_pkey PRIMARY KEY (id);

ALTER TABLE ONLY nba.players
    ADD CONSTRAINT players_pkey PRIMARY KEY (id);

ALTER TABLE ONLY nba.playoff_series
    ADD CONSTRAINT playoff_series_pkey PRIMARY KEY (id);

ALTER TABLE ONLY nba.team_stats
    ADD CONSTRAINT team_stats_pkey PRIMARY KEY (id);

ALTER TABLE ONLY nba.teams
    ADD CONSTRAINT teams_pkey PRIMARY KEY (id);

ALTER TABLE ONLY stats_s2.cumulative_player_stats
    ADD CONSTRAINT cumulative_player_stats_pkey UNIQUE (id, date);

ALTER TABLE ONLY usr.api_keys
    ADD CONSTRAINT api_keys_key_hash_key UNIQUE (key_hash);

ALTER TABLE ONLY usr.api_keys
    ADD CONSTRAINT api_keys_pkey PRIMARY KEY (id);

ALTER TABLE ONLY usr.lineups
    ADD CONSTRAINT lineups_pkey PRIMARY KEY (lineup_id);

ALTER TABLE ONLY usr.notification_log
    ADD CONSTRAINT notification_log_pkey PRIMARY KEY (id);

ALTER TABLE ONLY usr.notification_preferences
    ADD CONSTRAINT notification_preferences_pkey PRIMARY KEY (id);

ALTER TABLE ONLY usr.notification_team_preferences
    ADD CONSTRAINT notification_team_preferences_pkey PRIMARY KEY (id);

ALTER TABLE ONLY usr.teams
    ADD CONSTRAINT teams_pkey PRIMARY KEY (team_id);

ALTER TABLE ONLY usr.users
    ADD CONSTRAINT users_clerk_user_id_key UNIQUE (clerk_user_id);

ALTER TABLE ONLY usr.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (user_id);

ALTER TABLE ONLY usr.verifications
    ADD CONSTRAINT verifications_pkey PRIMARY KEY (id);

CREATE INDEX breakoutcandidate_as_of_date ON nba.breakout_candidates USING btree (as_of_date);

CREATE INDEX breakoutcandidate_as_of_date_breakout_score ON nba.breakout_candidates USING btree (as_of_date, breakout_score);

CREATE INDEX breakoutcandidate_beneficiary_player_id ON nba.breakout_candidates USING btree (beneficiary_player_id);

CREATE UNIQUE INDEX breakoutcandidate_beneficiary_player_id_as_of_date ON nba.breakout_candidates USING btree (beneficiary_player_id, as_of_date);

CREATE INDEX breakoutcandidate_injured_player_id ON nba.breakout_candidates USING btree (injured_player_id);

CREATE INDEX breakoutcandidate_injured_player_id_as_of_date ON nba.breakout_candidates USING btree (injured_player_id, as_of_date);

CREATE INDEX breakoutcandidate_pipeline_run_id ON nba.breakout_candidates USING btree (pipeline_run_id);

CREATE INDEX breakoutcandidate_team_id ON nba.breakout_candidates USING btree (team_id);

CREATE INDEX cronjobrun_job_name ON nba.cron_job_runs USING btree (job_name);

CREATE INDEX cronjobrun_result ON nba.cron_job_runs USING btree (result);

CREATE INDEX cronjobrun_triggered_at ON nba.cron_job_runs USING btree (triggered_at);

CREATE INDEX dataqualitycheck_check_name ON nba.data_quality_checks USING btree (check_name);

CREATE INDEX dataqualitycheck_run_id ON nba.data_quality_checks USING btree (run_id);

CREATE INDEX dataqualitycheck_run_id_check_name ON nba.data_quality_checks USING btree (run_id, check_name);

CREATE INDEX dataqualitycheck_status ON nba.data_quality_checks USING btree (status);

CREATE INDEX dataqualityrun_started_at ON nba.data_quality_runs USING btree (started_at);

CREATE INDEX dataqualityrun_status ON nba.data_quality_runs USING btree (status);

CREATE INDEX game_away_team_id ON nba.games USING btree (away_team_id);

CREATE INDEX game_game_date ON nba.games USING btree (game_date);

CREATE INDEX game_game_date_away_team_id ON nba.games USING btree (game_date, away_team_id);

CREATE INDEX game_game_date_home_team_id ON nba.games USING btree (game_date, home_team_id);

CREATE INDEX game_home_team_id ON nba.games USING btree (home_team_id);

CREATE INDEX game_season ON nba.games USING btree (season);

CREATE INDEX game_season_game_date ON nba.games USING btree (season, game_date);

CREATE INDEX idx_pipeline_runs_pipeline_name ON nba.pipeline_runs USING btree (pipeline_name);

CREATE INDEX idx_pipeline_runs_status ON nba.pipeline_runs USING btree (status);

CREATE INDEX livegamescoresnapshot_captured_at ON nba.live_game_score_snapshots USING btree (captured_at);

CREATE INDEX livegamescoresnapshot_game_date ON nba.live_game_score_snapshots USING btree (game_date);

CREATE INDEX livegamescoresnapshot_game_id ON nba.live_game_score_snapshots USING btree (game_id);

CREATE INDEX livegamescoresnapshot_game_id_captured_at ON nba.live_game_score_snapshots USING btree (game_id, captured_at);

CREATE INDEX liveplayerstats_game_date ON nba.live_player_stats USING btree (game_date);

CREATE INDEX liveplayerstats_pipeline_run_id ON nba.live_player_stats USING btree (pipeline_run_id);

CREATE INDEX liveplayerstats_player_id ON nba.live_player_stats USING btree (player_id);

CREATE UNIQUE INDEX liveplayerstats_player_id_game_id ON nba.live_player_stats USING btree (player_id, game_id);

CREATE INDEX pipelinerun_pipeline_name ON nba.pipeline_runs USING btree (pipeline_name);

CREATE INDEX pipelinerun_status ON nba.pipeline_runs USING btree (status);

CREATE UNIQUE INDEX player_espn_id ON nba.players USING btree (espn_id);

CREATE INDEX player_name_normalized ON nba.players USING btree (name_normalized);

CREATE INDEX playeradvancedstats_as_of_date ON nba.player_advanced_stats USING btree (as_of_date);

CREATE INDEX playeradvancedstats_pipeline_run_id ON nba.player_advanced_stats USING btree (pipeline_run_id);

CREATE INDEX playeradvancedstats_player_id ON nba.player_advanced_stats USING btree (player_id);

CREATE UNIQUE INDEX playeradvancedstats_player_id_as_of_date ON nba.player_advanced_stats USING btree (player_id, as_of_date);

CREATE INDEX playeradvancedstats_season ON nba.player_advanced_stats USING btree (season);

CREATE INDEX playeradvancedstats_season_as_of_date ON nba.player_advanced_stats USING btree (season, as_of_date);

CREATE INDEX playeradvancedstats_team_id ON nba.player_advanced_stats USING btree (team_id);

CREATE INDEX playergamestats_game_date ON nba.player_game_stats USING btree (game_date);

CREATE INDEX playergamestats_pipeline_run_id ON nba.player_game_stats USING btree (pipeline_run_id);

CREATE INDEX playergamestats_player_id ON nba.player_game_stats USING btree (player_id);

CREATE UNIQUE INDEX playergamestats_player_id_game_date ON nba.player_game_stats USING btree (player_id, game_date);

CREATE INDEX playergamestats_team_id ON nba.player_game_stats USING btree (team_id);

CREATE INDEX playerinjury_pipeline_run_id ON nba.player_injuries USING btree (pipeline_run_id);

CREATE INDEX playerinjury_player_id ON nba.player_injuries USING btree (player_id);

CREATE UNIQUE INDEX playerinjury_player_id_report_date ON nba.player_injuries USING btree (player_id, report_date);

CREATE INDEX playerinjury_report_date ON nba.player_injuries USING btree (report_date);

CREATE INDEX playerinjury_report_date_status ON nba.player_injuries USING btree (report_date, status);

CREATE INDEX playerinjury_status ON nba.player_injuries USING btree (status);

CREATE INDEX playerownership_pipeline_run_id ON nba.player_ownership USING btree (pipeline_run_id);

CREATE INDEX playerownership_player_id ON nba.player_ownership USING btree (player_id);

CREATE UNIQUE INDEX playerownership_player_id_snapshot_date ON nba.player_ownership USING btree (player_id, snapshot_date);

CREATE INDEX playerownership_snapshot_date ON nba.player_ownership USING btree (snapshot_date);

CREATE INDEX playerownership_snapshot_date_rost_pct ON nba.player_ownership USING btree (snapshot_date, rost_pct);

CREATE INDEX playerprofile_team_id ON nba.player_profiles USING btree (team_id);

CREATE INDEX playerrollingstats_as_of_date_window_days ON nba.player_rolling_stats USING btree (as_of_date, window_days);

CREATE INDEX playerrollingstats_pipeline_run_id ON nba.player_rolling_stats USING btree (pipeline_run_id);

CREATE INDEX playerrollingstats_player_id ON nba.player_rolling_stats USING btree (player_id);

CREATE UNIQUE INDEX playerrollingstats_player_id_as_of_date_window_days ON nba.player_rolling_stats USING btree (player_id, as_of_date, window_days);

CREATE INDEX playerrollingstats_player_id_window_days ON nba.player_rolling_stats USING btree (player_id, window_days);

CREATE INDEX playerrollingstats_team_id ON nba.player_rolling_stats USING btree (team_id);

CREATE INDEX playerseasonstats_as_of_date ON nba.player_season_stats USING btree (as_of_date);

CREATE INDEX playerseasonstats_as_of_date_rank ON nba.player_season_stats USING btree (as_of_date, rank);

CREATE INDEX playerseasonstats_pipeline_run_id ON nba.player_season_stats USING btree (pipeline_run_id);

CREATE INDEX playerseasonstats_player_id ON nba.player_season_stats USING btree (player_id);

CREATE UNIQUE INDEX playerseasonstats_player_id_as_of_date ON nba.player_season_stats USING btree (player_id, as_of_date);

CREATE INDEX playerseasonstats_rank ON nba.player_season_stats USING btree (rank);

CREATE INDEX playerseasonstats_season ON nba.player_season_stats USING btree (season);

CREATE INDEX playerseasonstats_season_as_of_date ON nba.player_season_stats USING btree (season, as_of_date);

CREATE INDEX playerseasonstats_team_id ON nba.player_season_stats USING btree (team_id);

CREATE INDEX playoffseries_season ON nba.playoff_series USING btree (season);

CREATE UNIQUE INDEX playoffseries_season_series_id ON nba.playoff_series USING btree (season, series_id);

CREATE INDEX playoffseries_series_id ON nba.playoff_series USING btree (series_id);

CREATE INDEX teamstats_as_of_date ON nba.team_stats USING btree (as_of_date);

CREATE INDEX teamstats_pipeline_run_id ON nba.team_stats USING btree (pipeline_run_id);

CREATE INDEX teamstats_season ON nba.team_stats USING btree (season);

CREATE INDEX teamstats_team_id ON nba.team_stats USING btree (team_id);

CREATE UNIQUE INDEX teamstats_team_id_as_of_date ON nba.team_stats USING btree (team_id, as_of_date);

CREATE UNIQUE INDEX cumulativeplayerstats_id_date ON stats_s2.cumulative_player_stats USING btree (id, date);

CREATE INDEX cumulativeplayerstats_pipeline_run_id ON stats_s2.cumulative_player_stats USING btree (pipeline_run_id);

CREATE INDEX dailymatchupscore_pipeline_run_id ON stats_s2.daily_matchup_scores USING btree (pipeline_run_id);

CREATE UNIQUE INDEX dailymatchupscore_team_id_matchup_period_date ON stats_s2.daily_matchup_scores USING btree (team_id, matchup_period, date);

CREATE UNIQUE INDEX dailyplayerstats_id_date ON stats_s2.daily_player_stats USING btree (id, date);

CREATE INDEX dailyplayerstats_pipeline_run_id ON stats_s2.daily_player_stats USING btree (pipeline_run_id);

CREATE INDEX idx_cumulative_player_stats_pipeline_run_id ON stats_s2.cumulative_player_stats USING btree (pipeline_run_id);

CREATE INDEX idx_daily_matchup_scores_pipeline_run_id ON stats_s2.daily_matchup_scores USING btree (pipeline_run_id);

CREATE INDEX idx_daily_player_stats_name_normalized ON stats_s2.daily_player_stats USING btree (name_normalized);

CREATE INDEX idx_daily_player_stats_name_normalized_team ON stats_s2.daily_player_stats USING btree (name_normalized, team);

CREATE INDEX idx_daily_player_stats_pipeline_run_id ON stats_s2.daily_player_stats USING btree (pipeline_run_id);

CREATE UNIQUE INDEX apikey_key_hash ON usr.api_keys USING btree (key_hash);

CREATE INDEX apikey_scopes ON usr.api_keys USING gin (scopes);

CREATE INDEX apikey_user_id ON usr.api_keys USING btree (user_id);

CREATE INDEX idx_api_keys_active ON usr.api_keys USING btree (is_active) WHERE (is_active = true);

CREATE INDEX idx_api_keys_hash ON usr.api_keys USING btree (key_hash);

CREATE INDEX idx_api_keys_user ON usr.api_keys USING btree (user_id);

CREATE INDEX idx_users_clerk_user_id ON usr.users USING btree (clerk_user_id);

CREATE UNIQUE INDEX lineup_lineup_hash ON usr.lineups USING btree (lineup_hash);

CREATE INDEX lineup_team_id ON usr.lineups USING btree (team_id);

CREATE INDEX notificationlog_notification_date ON usr.notification_log USING btree (notification_date);

CREATE INDEX notificationlog_user_id ON usr.notification_log USING btree (user_id);

CREATE UNIQUE INDEX notificationlog_user_id_team_id_notification_type_notifi_a447c4 ON usr.notification_log USING btree (user_id, team_id, notification_type, notification_date);

CREATE UNIQUE INDEX notificationpreference_user_id ON usr.notification_preferences USING btree (user_id);

CREATE INDEX notificationteampreference_user_id ON usr.notification_team_preferences USING btree (user_id);

CREATE UNIQUE INDEX notificationteampreference_user_id_team_id ON usr.notification_team_preferences USING btree (user_id, team_id);

CREATE INDEX team_user_id ON usr.teams USING btree (user_id);

CREATE UNIQUE INDEX user_clerk_user_id ON usr.users USING btree (clerk_user_id);

CREATE UNIQUE INDEX user_email ON usr.users USING btree (email);

ALTER TABLE ONLY nba.breakout_candidates
    ADD CONSTRAINT breakout_candidates_beneficiary_player_id_fkey FOREIGN KEY (beneficiary_player_id) REFERENCES nba.players(id) ON DELETE CASCADE;

ALTER TABLE ONLY nba.breakout_candidates
    ADD CONSTRAINT breakout_candidates_injured_player_id_fkey FOREIGN KEY (injured_player_id) REFERENCES nba.players(id) ON DELETE CASCADE;

ALTER TABLE ONLY nba.breakout_candidates
    ADD CONSTRAINT breakout_candidates_team_id_fkey FOREIGN KEY (team_id) REFERENCES nba.teams(id) ON DELETE SET NULL;

ALTER TABLE ONLY nba.data_quality_checks
    ADD CONSTRAINT data_quality_checks_run_id_fkey FOREIGN KEY (run_id) REFERENCES nba.data_quality_runs(id) ON DELETE CASCADE;

ALTER TABLE ONLY nba.games
    ADD CONSTRAINT games_away_team_id_fkey FOREIGN KEY (away_team_id) REFERENCES nba.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY nba.games
    ADD CONSTRAINT games_home_team_id_fkey FOREIGN KEY (home_team_id) REFERENCES nba.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY nba.live_player_stats
    ADD CONSTRAINT live_player_stats_player_id_fkey FOREIGN KEY (player_id) REFERENCES nba.players(id) ON DELETE CASCADE;

ALTER TABLE ONLY nba.player_advanced_stats
    ADD CONSTRAINT player_advanced_stats_player_id_fkey FOREIGN KEY (player_id) REFERENCES nba.players(id) ON DELETE CASCADE;

ALTER TABLE ONLY nba.player_advanced_stats
    ADD CONSTRAINT player_advanced_stats_team_id_fkey FOREIGN KEY (team_id) REFERENCES nba.teams(id) ON DELETE SET NULL;

ALTER TABLE ONLY nba.player_game_stats
    ADD CONSTRAINT player_game_stats_player_id_fkey FOREIGN KEY (player_id) REFERENCES nba.players(id) ON DELETE CASCADE;

ALTER TABLE ONLY nba.player_game_stats
    ADD CONSTRAINT player_game_stats_team_id_fkey FOREIGN KEY (team_id) REFERENCES nba.teams(id) ON DELETE RESTRICT;

ALTER TABLE ONLY nba.player_injuries
    ADD CONSTRAINT player_injuries_player_id_fkey FOREIGN KEY (player_id) REFERENCES nba.players(id) ON DELETE CASCADE;

ALTER TABLE ONLY nba.player_ownership
    ADD CONSTRAINT player_ownership_player_id_fkey FOREIGN KEY (player_id) REFERENCES nba.players(id) ON DELETE CASCADE;

ALTER TABLE ONLY nba.player_profiles
    ADD CONSTRAINT player_profiles_player_id_fkey FOREIGN KEY (player_id) REFERENCES nba.players(id) ON DELETE CASCADE;

ALTER TABLE ONLY nba.player_profiles
    ADD CONSTRAINT player_profiles_team_id_fkey FOREIGN KEY (team_id) REFERENCES nba.teams(id) ON DELETE SET NULL;

ALTER TABLE ONLY nba.player_rolling_stats
    ADD CONSTRAINT player_rolling_stats_player_id_fkey FOREIGN KEY (player_id) REFERENCES nba.players(id) ON DELETE CASCADE;

ALTER TABLE ONLY nba.player_rolling_stats
    ADD CONSTRAINT player_rolling_stats_team_id_fkey FOREIGN KEY (team_id) REFERENCES nba.teams(id) ON DELETE RESTRICT;

ALTER TABLE ONLY nba.player_season_stats
    ADD CONSTRAINT player_season_stats_player_id_fkey FOREIGN KEY (player_id) REFERENCES nba.players(id) ON DELETE CASCADE;

ALTER TABLE ONLY nba.player_season_stats
    ADD CONSTRAINT player_season_stats_team_id_fkey FOREIGN KEY (team_id) REFERENCES nba.teams(id) ON DELETE RESTRICT;

ALTER TABLE ONLY nba.team_stats
    ADD CONSTRAINT team_stats_team_id_fkey FOREIGN KEY (team_id) REFERENCES nba.teams(id) ON DELETE RESTRICT;

ALTER TABLE ONLY usr.api_keys
    ADD CONSTRAINT api_keys_user_id_fkey FOREIGN KEY (user_id) REFERENCES usr.users(user_id) ON DELETE SET NULL;

ALTER TABLE ONLY usr.lineups
    ADD CONSTRAINT lineups_team_id_fkey FOREIGN KEY (team_id) REFERENCES usr.teams(team_id) ON DELETE CASCADE;

ALTER TABLE ONLY usr.notification_log
    ADD CONSTRAINT notification_log_user_id_fkey FOREIGN KEY (user_id) REFERENCES usr.users(user_id) ON DELETE CASCADE;

ALTER TABLE ONLY usr.notification_preferences
    ADD CONSTRAINT notification_preferences_user_id_fkey FOREIGN KEY (user_id) REFERENCES usr.users(user_id) ON DELETE CASCADE;

ALTER TABLE ONLY usr.notification_team_preferences
    ADD CONSTRAINT notification_team_preferences_user_id_fkey FOREIGN KEY (user_id) REFERENCES usr.users(user_id) ON DELETE CASCADE;

ALTER TABLE ONLY usr.teams
    ADD CONSTRAINT teams_user_id_fkey FOREIGN KEY (user_id) REFERENCES usr.users(user_id) ON DELETE CASCADE;
