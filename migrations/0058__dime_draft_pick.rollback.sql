-- Rollback 0058: Dime's recorded picks go with their column.
ALTER TABLE usr.ai_draft_notes DROP COLUMN IF EXISTS pick;
