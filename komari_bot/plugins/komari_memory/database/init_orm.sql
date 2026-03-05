-- Komari Memory 插件数据库初始化脚本
-- 前置条件:
-- 1. 已安装 pgvector 扩展: CREATE EXTENSION IF NOT EXISTS vector;
-- 2. 数据库已创建

-- ============================================
-- 对话总结表 (conversations)
-- 存储对话的向量表示和总结
-- ============================================
CREATE TABLE IF NOT EXISTS komari_memory_conversations (
    id SERIAL PRIMARY KEY,
    group_id VARCHAR(64) NOT NULL,
    summary TEXT NOT NULL,
    embedding VECTOR(4096),  -- Qwen/Qwen3-Embedding-8B
    participants TEXT[],
    start_time TIMESTAMP NOT NULL,
    end_time TIMESTAMP NOT NULL,
    importance INT DEFAULT 3 CHECK (importance BETWEEN 1 AND 5),
    importance_initial INT DEFAULT 3 CHECK (importance_initial BETWEEN 1 AND 5),
    importance_current INT DEFAULT 3 CHECK (importance_current BETWEEN 0 AND 5),
    last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_fuzzy BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================
-- 创建其他索引
-- ============================================
CREATE INDEX IF NOT EXISTS idx_komari_memory_conv_group
ON komari_memory_conversations(group_id);

CREATE INDEX IF NOT EXISTS idx_komari_memory_conv_time
ON komari_memory_conversations(start_time DESC);

-- ============================================
-- 实体表 (entity)
-- 存储用户/群组结构化信息（如偏好、属性等）
-- ============================================
CREATE TABLE IF NOT EXISTS komari_memory_entity (
    user_id VARCHAR(64) NOT NULL,
    group_id VARCHAR(64) NOT NULL,
    key VARCHAR(100) NOT NULL CHECK (key IN ('user_profile', 'interaction_history')),
    value TEXT NOT NULL,
    category VARCHAR(50) NOT NULL CHECK (category IN ('profile_json', 'interaction_history')),
    importance INT DEFAULT 3 CHECK (importance BETWEEN 1 AND 5),
    access_count INT DEFAULT 0,
    last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_komari_memory_entity_two_row_model CHECK (
        (key = 'user_profile' AND category = 'profile_json')
        OR
        (key = 'interaction_history' AND category = 'interaction_history')
    ),
    PRIMARY KEY (user_id, group_id, key)
);

-- ============================================
-- 创建实体表索引
-- ============================================
CREATE INDEX IF NOT EXISTS idx_komari_memory_entity_group
ON komari_memory_entity(group_id);

CREATE INDEX IF NOT EXISTS idx_komari_memory_entity_importance
ON komari_memory_entity(importance DESC);

-- ============================================
-- Scene 持久化: 版本集表 (scene_set)
-- 记录一次场景构建的元信息与状态
-- ============================================
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
);

CREATE INDEX IF NOT EXISTS idx_komari_memory_scene_set_status
ON komari_memory_scene_set(status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_komari_memory_scene_set_source_hash
ON komari_memory_scene_set(source_hash);

-- ============================================
-- Scene 持久化: 条目表 (scene_item)
-- 保存每个 scene 的文本、哈希和 embedding
-- ============================================
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
);

CREATE INDEX IF NOT EXISTS idx_komari_memory_scene_item_set_status
ON komari_memory_scene_item(set_id, status);

CREATE INDEX IF NOT EXISTS idx_komari_memory_scene_item_reuse
ON komari_memory_scene_item(scene_key, content_hash);

-- ============================================
-- Scene 持久化: 运行时指针表 (scene_runtime)
-- 仅保留一行，指向当前 active set
-- ============================================
CREATE TABLE IF NOT EXISTS komari_memory_scene_runtime (
    id INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    active_set_id BIGINT REFERENCES komari_memory_scene_set(id),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO komari_memory_scene_runtime (id, active_set_id)
VALUES (1, NULL)
ON CONFLICT (id) DO NOTHING;

-- ============================================
-- 表注释
-- ============================================
COMMENT ON TABLE komari_memory_conversations IS 'Komari Memory 对话总结表 - 存储对话总结和向量表示';
COMMENT ON TABLE komari_memory_entity IS 'Komari Memory 实体表 - 存储用户/群组结构化信息';
COMMENT ON TABLE komari_memory_scene_set IS 'Komari Memory Scene 版本集表';
COMMENT ON TABLE komari_memory_scene_item IS 'Komari Memory Scene 条目与 embedding 表';
COMMENT ON TABLE komari_memory_scene_runtime IS 'Komari Memory Scene 运行时 active set 指针';
