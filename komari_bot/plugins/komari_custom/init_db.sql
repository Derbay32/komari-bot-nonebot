CREATE TABLE IF NOT EXISTS komari_custom_proposals (
    id              SERIAL PRIMARY KEY,
    group_id        BIGINT NOT NULL,
    proposer_id     BIGINT NOT NULL,
    proposer_name   TEXT,
    title           TEXT NOT NULL,
    content         TEXT NOT NULL,
    status          VARCHAR(20) DEFAULT 'voting' NOT NULL,
    vote_message_id BIGINT,
    vote_count      INT DEFAULT 0 NOT NULL,
    required_votes  INT NOT NULL,
    voted_users     TEXT[] DEFAULT '{}' NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW() NOT NULL,
    updated_at      TIMESTAMPTZ DEFAULT NOW() NOT NULL,
    approved_at     TIMESTAMPTZ,
    knowledge_id    INT,
    expired_at      TIMESTAMPTZ
);

ALTER TABLE komari_custom_proposals
    ADD COLUMN IF NOT EXISTS proposer_name TEXT;

ALTER TABLE komari_custom_proposals
    ADD COLUMN IF NOT EXISTS approval_token TEXT;

ALTER TABLE komari_custom_proposals
    ADD COLUMN IF NOT EXISTS approval_started_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_custom_proposals_status
    ON komari_custom_proposals(status);

CREATE INDEX IF NOT EXISTS idx_custom_proposals_group_id
    ON komari_custom_proposals(group_id);

CREATE INDEX IF NOT EXISTS idx_custom_proposals_proposer_status
    ON komari_custom_proposals(proposer_id, status);

CREATE INDEX IF NOT EXISTS idx_custom_proposals_vote_message_id
    ON komari_custom_proposals(vote_message_id)
    WHERE vote_message_id IS NOT NULL;
