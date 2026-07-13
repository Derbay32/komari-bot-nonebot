CREATE TABLE IF NOT EXISTS komari_user_bans (
    user_id TEXT NOT NULL,
    ban_scope TEXT NOT NULL CHECK (ban_scope IN ('chat', 'command')),
    operator_id TEXT NOT NULL,
    reason TEXT,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, ban_scope)
);

ALTER TABLE komari_user_bans
ADD COLUMN IF NOT EXISTS reason TEXT;

ALTER TABLE komari_user_bans
ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_komari_user_bans_updated_at
ON komari_user_bans (updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_komari_user_bans_expires_at
ON komari_user_bans (expires_at)
WHERE expires_at IS NOT NULL;
