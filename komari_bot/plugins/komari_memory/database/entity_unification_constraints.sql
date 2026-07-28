-- Komari Memory 实体两行模型约束脚本
-- 适用于已完成数据迁移后的库。

ALTER TABLE komari_memory_entity
DROP CONSTRAINT IF EXISTS ck_komari_memory_entity_two_row_model;

ALTER TABLE komari_memory_entity
ADD CONSTRAINT ck_komari_memory_entity_two_row_model
CHECK (
    (key = 'user_profile' AND category = 'profile_json')
    OR
    (key = 'interaction_history' AND category = 'interaction_history')
) NOT VALID;

ALTER TABLE komari_memory_entity
VALIDATE CONSTRAINT ck_komari_memory_entity_two_row_model;

-- 验收 SQL：应返回 0
SELECT count(*) AS invalid_key_or_category
FROM komari_memory_entity
WHERE key NOT IN ('user_profile', 'interaction_history')
   OR (
        key = 'user_profile' AND category != 'profile_json'
   )
   OR (
        key = 'interaction_history' AND category != 'interaction_history'
   );

-- 验收 SQL：应返回 0（每个用户仅两行）
SELECT count(*) AS invalid_user_pair_rows
FROM (
    SELECT group_id, user_id, count(*) AS c
    FROM komari_memory_entity
    GROUP BY group_id, user_id
    HAVING count(*) != 2
) t;
