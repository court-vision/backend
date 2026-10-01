-- Migration 0026: the AI router's question log (docs/AI_PHASE1_PLAN.md § 5).
--
-- One row per routed question that reached the model. It is the roadmap as much
-- as an audit trail: every `statmuse` row is a stat question users asked that
-- no Court Vision view covers, and every `cannot` is one nothing covers.
--
-- Retention: the question text and its context are nulled after 90 days (the
-- sweep runs with each insert -- see services/ai/questions.py) while the
-- counts, kinds and token meters stay. The partial index keeps that sweep to
-- the rows that still carry text.

CREATE TABLE IF NOT EXISTS usr.ai_questions (
    id                          bigserial PRIMARY KEY,
    user_id                     integer NOT NULL REFERENCES usr.users (user_id) ON DELETE CASCADE,
    created_at                  timestamp with time zone NOT NULL DEFAULT now(),
    question                    text,
    context                     jsonb,
    kind                        text,
    target                      jsonb,
    statmuse_query              text,
    gap                         text,
    missing                     text,
    tool_calls                  jsonb NOT NULL DEFAULT '[]'::jsonb,
    outcome                     text NOT NULL,
    model_calls                 integer NOT NULL DEFAULT 0,
    input_tokens                integer NOT NULL DEFAULT 0,
    output_tokens               integer NOT NULL DEFAULT 0,
    cache_read_input_tokens     integer NOT NULL DEFAULT 0,
    cache_creation_input_tokens integer NOT NULL DEFAULT 0,
    duration_ms                 integer,
    ungrounded_numbers          integer,
    feedback                    text,
    CONSTRAINT ai_questions_kind_check CHECK (kind IS NULL OR kind IN ('show', 'statmuse', 'cannot')),
    CONSTRAINT ai_questions_feedback_check CHECK (feedback IS NULL OR feedback IN ('up', 'down'))
);

CREATE INDEX IF NOT EXISTS ai_questions_user_created_idx
    ON usr.ai_questions (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS ai_questions_text_retention_idx
    ON usr.ai_questions (created_at)
    WHERE question IS NOT NULL;

COMMENT ON TABLE usr.ai_questions IS
  'Questions routed by the AI layer (POST /v1/internal/ai/route). Question text and context are nulled after 90 days.';
