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
    CREATE TABLE IF NOT EXISTS komari_memory_scene_item (
        id BIGSERIAL PRIMARY KEY,
        set_id BIGINT NOT NULL REFERENCES komari_memory_scene_set(id) ON DELETE CASCADE,
        scene_key TEXT NOT NULL,
        scene_type TEXT NOT NULL CHECK (scene_type IN ('fixed', 'general')),
        content_text TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        order_index INT NOT NULL DEFAULT 0,
        embedding REAL[],
        embedding_dim INT,
        status TEXT NOT NULL CHECK (status IN ('PENDING', 'READY', 'FAILED')),
        error_message TEXT,
        embedded_at TIMESTAMPTZ,
        UNIQUE (set_id, scene_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_komari_memory_scene_item_set_status
    ON komari_memory_scene_item(set_id, status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_komari_memory_scene_item_reuse
    ON komari_memory_scene_item(scene_key, content_hash)
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
)

__all__ = ["SCENE_SCHEMA_STATEMENTS"]
