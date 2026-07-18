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

CREATE TABLE IF NOT EXISTS komari_user_ban_cache_state (
    singleton_id SMALLINT PRIMARY KEY CHECK (singleton_id = 1),
    revision BIGINT NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO komari_user_ban_cache_state (singleton_id, revision)
VALUES (1, 1)
ON CONFLICT (singleton_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS komari_user_ban_notification_outbox (
    notification_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    notification_kind TEXT NOT NULL CHECK (notification_kind = 'natural_expiry'),
    records JSONB,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'sent')),
    owner_token TEXT,
    lease_expires_at TIMESTAMPTZ,
    available_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_komari_user_ban_notification_outbox_claim
ON komari_user_ban_notification_outbox (available_at, created_at)
WHERE status IN ('pending', 'processing');
