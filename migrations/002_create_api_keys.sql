-- Migration: Create API keys table
-- Created: 2025-01-30

CREATE TABLE IF NOT EXISTS usr.api_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id INTEGER REFERENCES usr.users(user_id) ON DELETE SET NULL,
    key_hash VARCHAR(64) NOT NULL UNIQUE,
    key_prefix VARCHAR(11) NOT NULL,
    name VARCHAR(100) NOT NULL,
    scopes TEXT[] DEFAULT '{}',
    rate_limit INTEGER DEFAULT 1000,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    is_active BOOLEAN DEFAULT TRUE
);

-- Index for fast key lookup
CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON usr.api_keys(key_hash);

-- Index for user's API keys
CREATE INDEX IF NOT EXISTS idx_api_keys_user ON usr.api_keys(user_id);

-- Index for active keys
CREATE INDEX IF NOT EXISTS idx_api_keys_active ON usr.api_keys(is_active) WHERE is_active = TRUE;

COMMENT ON TABLE usr.api_keys IS 'API keys for programmatic access to protected endpoints';
COMMENT ON COLUMN usr.api_keys.key_hash IS 'SHA-256 hash of the API key';
COMMENT ON COLUMN usr.api_keys.key_prefix IS 'First 11 chars of key for display (e.g., cv_abc12...)';
COMMENT ON COLUMN usr.api_keys.scopes IS 'Array of permission scopes (read, optimize, admin)';
COMMENT ON COLUMN usr.api_keys.rate_limit IS 'Requests per minute allowed for this key';
