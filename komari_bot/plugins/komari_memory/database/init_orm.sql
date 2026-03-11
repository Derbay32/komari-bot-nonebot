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
-- Scene 持久化表结构已迁移到 komari_decision 运行时维护
-- 避免与 memory 插件中的历史 SQL 副本继续漂移
-- ============================================

-- ============================================
-- 表注释
-- ============================================
COMMENT ON TABLE komari_memory_conversations IS 'Komari Memory 对话总结表 - 存储对话总结和向量表示';
COMMENT ON TABLE komari_memory_entity IS 'Komari Memory 实体表 - 存储用户/群组结构化信息';
