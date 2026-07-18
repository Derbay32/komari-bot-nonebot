"""Scene persistence schema definitions."""

from __future__ import annotations

SCENE_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS komari_memory_scene_set (
        id BIGSERIAL PRIMARY KEY,
        source_path TEXT NOT NULL,
        source_hash TEXT NOT NULL,
        embedding_model TEXT NOT NULL,
        embedding_instruction_hash TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('BUILDING', 'READY', 'FAILED')),
        item_total INT NOT NULL DEFAULT 0 CHECK (item_total >= 0),
        item_ready INT NOT NULL DEFAULT 0 CHECK (item_ready >= 0),
        item_failed INT NOT NULL DEFAULT 0 CHECK (item_failed >= 0),
        error_message TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        ready_at TIMESTAMPTZ
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_komari_memory_scene_set_status
    ON komari_memory_scene_set(status, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_komari_memory_scene_set_source_hash
    ON komari_memory_scene_set(source_hash)
    """,
    """
    CREATE TABLE IF NOT EXISTS komari_decision_scenes (
        id BIGSERIAL PRIMARY KEY,
        scene_key TEXT NOT NULL UNIQUE,
        scene_type TEXT NOT NULL CHECK (scene_type IN ('fixed', 'general')),
        content_text TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        order_index INT NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_komari_decision_scenes_type_order
    ON komari_decision_scenes(scene_type, enabled, order_index)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_komari_decision_scenes_content_hash
    ON komari_decision_scenes(content_hash)
    """,
    """
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'komari_memory_scene_item'
              AND column_name = 'scene_key'
        ) THEN
            DROP TABLE komari_memory_scene_item CASCADE;
        END IF;
    END $$
    """,
    """
    CREATE TABLE IF NOT EXISTS komari_memory_scene_item (
        id BIGSERIAL PRIMARY KEY,
        set_id BIGINT NOT NULL REFERENCES komari_memory_scene_set(id) ON DELETE CASCADE,
        scene_id BIGINT NOT NULL REFERENCES komari_decision_scenes(id) ON DELETE CASCADE,
        scene_key_snapshot TEXT NOT NULL,
        scene_type_snapshot TEXT NOT NULL,
        content_text_snapshot TEXT NOT NULL,
        enabled_snapshot BOOLEAN NOT NULL,
        order_index_snapshot INT NOT NULL,
        content_hash TEXT NOT NULL,
        embedding REAL[],
        embedding_dim INT,
        status TEXT NOT NULL,
        error_message TEXT,
        last_error_code TEXT,
        attempt_count INT NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        next_retry_at TIMESTAMPTZ,
        lease_owner TEXT,
        lease_expires_at TIMESTAMPTZ,
        embedded_at TIMESTAMPTZ,
        CONSTRAINT ck_komari_memory_scene_item_status
            CHECK (status IN ('PENDING', 'PROCESSING', 'READY', 'FAILED')),
        UNIQUE (set_id, scene_id)
    )
    """,
    """
    ALTER TABLE komari_memory_scene_item
    ADD COLUMN IF NOT EXISTS scene_key_snapshot TEXT
    """,
    """
    ALTER TABLE komari_memory_scene_item
    ADD COLUMN IF NOT EXISTS scene_type_snapshot TEXT
    """,
    """
    ALTER TABLE komari_memory_scene_item
    ADD COLUMN IF NOT EXISTS content_text_snapshot TEXT
    """,
    """
    ALTER TABLE komari_memory_scene_item
    ADD COLUMN IF NOT EXISTS enabled_snapshot BOOLEAN
    """,
    """
    ALTER TABLE komari_memory_scene_item
    ADD COLUMN IF NOT EXISTS order_index_snapshot INT
    """,
    """
    UPDATE komari_memory_scene_item item
    SET scene_key_snapshot = scene.scene_key,
        scene_type_snapshot = scene.scene_type,
        content_text_snapshot = scene.content_text,
        enabled_snapshot = scene.enabled,
        order_index_snapshot = scene.order_index
    FROM komari_decision_scenes scene
    WHERE item.scene_id = scene.id
      AND (
          item.scene_key_snapshot IS NULL
          OR item.scene_type_snapshot IS NULL
          OR item.content_text_snapshot IS NULL
          OR item.enabled_snapshot IS NULL
          OR item.order_index_snapshot IS NULL
      )
    """,
    """
    ALTER TABLE komari_memory_scene_item
        ALTER COLUMN scene_key_snapshot SET NOT NULL,
        ALTER COLUMN scene_type_snapshot SET NOT NULL,
        ALTER COLUMN content_text_snapshot SET NOT NULL,
        ALTER COLUMN enabled_snapshot SET NOT NULL,
        ALTER COLUMN order_index_snapshot SET NOT NULL
    """,
    """
    ALTER TABLE komari_memory_scene_item
    ADD COLUMN IF NOT EXISTS last_error_code TEXT
    """,
    """
    ALTER TABLE komari_memory_scene_item
    ADD COLUMN IF NOT EXISTS attempt_count INT NOT NULL DEFAULT 0
    CHECK (attempt_count >= 0)
    """,
    """
    ALTER TABLE komari_memory_scene_item
    ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ
    """,
    """
    ALTER TABLE komari_memory_scene_item
    ADD COLUMN IF NOT EXISTS lease_owner TEXT
    """,
    """
    ALTER TABLE komari_memory_scene_item
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ
    """,
    """
    ALTER TABLE komari_memory_scene_item
    DROP CONSTRAINT IF EXISTS komari_memory_scene_item_status_check
    """,
    """
    ALTER TABLE komari_memory_scene_item
    DROP CONSTRAINT IF EXISTS ck_komari_memory_scene_item_status
    """,
    """
    ALTER TABLE komari_memory_scene_item
    ADD CONSTRAINT ck_komari_memory_scene_item_status
    CHECK (status IN ('PENDING', 'PROCESSING', 'READY', 'FAILED'))
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_komari_memory_scene_item_scene_id
    ON komari_memory_scene_item(scene_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_komari_memory_scene_item_set_status
    ON komari_memory_scene_item(set_id, status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_komari_memory_scene_item_claim
    ON komari_memory_scene_item(set_id, status, next_retry_at, lease_expires_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_komari_memory_scene_item_reuse
    ON komari_memory_scene_item(scene_id, content_hash)
    """,
    """
    CREATE TABLE IF NOT EXISTS komari_memory_scene_runtime (
        id INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
        active_set_id BIGINT REFERENCES komari_memory_scene_set(id),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    INSERT INTO komari_memory_scene_runtime (id, active_set_id)
    VALUES (1, NULL)
    ON CONFLICT (id) DO NOTHING
    """,
    """
    WITH ranked_sets AS (
        SELECT
            s.id,
            ROW_NUMBER() OVER (
                PARTITION BY
                    s.source_hash,
                    s.embedding_model,
                    s.embedding_instruction_hash
                ORDER BY
                    CASE
                        WHEN s.id = r.active_set_id THEN 0
                        WHEN s.status = 'READY' THEN 1
                        WHEN s.status = 'BUILDING' THEN 2
                        ELSE 3
                    END,
                    COALESCE(s.ready_at, s.created_at) DESC,
                    s.id DESC
            ) AS duplicate_rank
        FROM komari_memory_scene_set s
        CROSS JOIN komari_memory_scene_runtime r
        WHERE r.id = 1
    )
    DELETE FROM komari_memory_scene_set target
    USING ranked_sets ranked
    WHERE target.id = ranked.id
      AND ranked.duplicate_rank > 1
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_komari_memory_scene_set_fingerprint
    ON komari_memory_scene_set(
        source_hash,
        embedding_model,
        embedding_instruction_hash
    )
    """,
)

__all__ = ["SCENE_SCHEMA_STATEMENTS"]
