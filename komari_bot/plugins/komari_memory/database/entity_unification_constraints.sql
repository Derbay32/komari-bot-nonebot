-- Komari Memory 实体两行模型约束脚本
-- 适用于已完成画像列迁移、并准备切换到最终严格约束的库。

ALTER TABLE komari_memory_entity
ADD COLUMN IF NOT EXISTS profile_version INT;

ALTER TABLE komari_memory_entity
ADD COLUMN IF NOT EXISTS profile_payload_user_id VARCHAR(64);

ALTER TABLE komari_memory_entity
ADD COLUMN IF NOT EXISTS profile_display_name TEXT;

ALTER TABLE komari_memory_entity
ADD COLUMN IF NOT EXISTS profile_traits JSONB;

ALTER TABLE komari_memory_entity
ADD COLUMN IF NOT EXISTS profile_updated_at TIMESTAMPTZ;

ALTER TABLE komari_memory_entity
ALTER COLUMN value DROP NOT NULL;

ALTER TABLE komari_memory_entity
DROP CONSTRAINT IF EXISTS ck_komari_memory_entity_two_row_model;

ALTER TABLE komari_memory_entity
ADD CONSTRAINT ck_komari_memory_entity_two_row_model
CHECK (
    (
        key = 'user_profile'
        AND category = 'profile_json'
        AND value IS NULL
        AND profile_version IS NOT NULL
        AND profile_payload_user_id IS NOT NULL
        AND profile_display_name IS NOT NULL
        AND profile_traits IS NOT NULL
        AND profile_updated_at IS NOT NULL
    )
    OR
    (
        key = 'interaction_history'
        AND category = 'interaction_history'
        AND value IS NOT NULL
        AND profile_version IS NULL
        AND profile_payload_user_id IS NULL
        AND profile_display_name IS NULL
        AND profile_traits IS NULL
        AND profile_updated_at IS NULL
    )
) NOT VALID;

ALTER TABLE komari_memory_entity
VALIDATE CONSTRAINT ck_komari_memory_entity_two_row_model;

-- 验收 SQL：应返回 0
SELECT count(*) AS invalid_two_row_model
FROM komari_memory_entity
WHERE NOT (
    (
        key = 'user_profile'
        AND category = 'profile_json'
        AND value IS NULL
        AND profile_version IS NOT NULL
        AND profile_payload_user_id IS NOT NULL
        AND profile_display_name IS NOT NULL
        AND profile_traits IS NOT NULL
        AND profile_updated_at IS NOT NULL
    )
    OR
    (
        key = 'interaction_history'
        AND category = 'interaction_history'
        AND value IS NOT NULL
        AND profile_version IS NULL
        AND profile_payload_user_id IS NULL
        AND profile_display_name IS NULL
        AND profile_traits IS NULL
        AND profile_updated_at IS NULL
    )
);

-- 验收 SQL：应返回 0（每个用户仅两行，且仅一条画像）
SELECT count(*) AS invalid_user_pair_rows
FROM (
    SELECT group_id, user_id, count(*) AS c
    FROM komari_memory_entity
    GROUP BY group_id, user_id
    HAVING count(*) != 2
) t;
