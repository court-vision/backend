-- Rollback 0057: Dime's recorded picks go with their column.
ALTER TABLE usr.ai_draft_notes DROP COLUMN IF EXISTS pick;
