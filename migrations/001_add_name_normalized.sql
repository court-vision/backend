-- Migration: Add name_normalized column for diacritic-insensitive lookups
-- Run this against your PostgreSQL database

-- Step 1: Enable unaccent extension (if not already enabled)
CREATE EXTENSION IF NOT EXISTS unaccent;

-- Step 2: Add the new column
ALTER TABLE stats_s2.daily_player_stats
ADD COLUMN IF NOT EXISTS name_normalized VARCHAR(50);

-- Step 3: Populate existing rows with normalized names
UPDATE stats_s2.daily_player_stats
SET name_normalized = lower(unaccent(name))
WHERE name_normalized IS NULL;

-- Step 4: Create index for fast lookups
CREATE INDEX IF NOT EXISTS idx_daily_player_stats_name_normalized
ON stats_s2.daily_player_stats(name_normalized);

-- Step 5: Create composite index for name + team lookups
CREATE INDEX IF NOT EXISTS idx_daily_player_stats_name_normalized_team
ON stats_s2.daily_player_stats(name_normalized, team);
