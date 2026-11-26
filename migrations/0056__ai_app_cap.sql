-- Migration 0056: one monthly cap on the whole app's AI spend.
--
-- James (2026-11-23): his Claude plan's $100 a month should cap the app's AI
-- spend in all. The three lines in usr.ai_spend (desk, autopilot, analyst) keep
-- their own caps; a reservation on any of them is now also held to what all
-- three spent this month together. Its default is AI_APP_MONTHLY_BUDGET_USD
-- ($100), and a usr.ai_caps row with line 'app' (no user: it is everyone's)
-- replaces it, set from the data platform's AI spend page like the others.
-- Numbered after 0055 (Dime's scheduled asks, in review) so the two don't clash.

ALTER TABLE usr.ai_caps DROP CONSTRAINT IF EXISTS ai_caps_line_check;
ALTER TABLE usr.ai_caps
    ADD CONSTRAINT ai_caps_line_check CHECK (line IN ('desk', 'autopilot', 'analyst', 'app'));

ALTER TABLE usr.ai_caps DROP CONSTRAINT IF EXISTS ai_caps_user_check;
ALTER TABLE usr.ai_caps
    ADD CONSTRAINT ai_caps_user_check CHECK (user_id IS NULL OR line IN ('desk', 'autopilot'));
