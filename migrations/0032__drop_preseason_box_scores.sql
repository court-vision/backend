-- Migration 0032: no preseason box scores.
--
-- The first production run of data-platform's game_history (2026-10-13, season
-- 2025-26) asked PlayerGameLogs for the regular season with no SeasonType. For
-- a whole season that returns every season type, so it also stored 1,783
-- preseason lines (63 games). Preseason rows are not meant to reach these
-- tables (game_start_times skips them for the same reason), no reader wants
-- them, and the pipeline now asks for "Regular Season" and skips preseason and
-- All-Star lines. Every such line came from that one run: none existed before.
--
-- nba.games keeps its preseason rows: they predate the history load.

DELETE FROM nba.player_game_stats
WHERE season_type IN ('preseason', 'all_star');
