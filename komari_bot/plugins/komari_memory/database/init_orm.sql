-- Komari Memory 插件数据库初始化脚本
-- 前置条件:
-- 1. 已安装 pgvector 扩展: CREATE EXTENSION IF NOT EXISTS vector;
-- 2. 数据库已创建
-- 3. 可选：通过 psql 变量覆盖 embedding 维度，例如：
--    psql -v embedding_dimension=1536 -f komari_bot/plugins/komari_memory/database/init_orm.sql

\if :{?embedding_dimension}
\else
\set embedding_dimension 512
\endif

CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================
-- 对话总结表 (conversations)
-- 存储对话的向量表示和总结
-- ============================================
CREATE TABLE IF NOT EXISTS komari_memory_conversations (
    id SERIAL PRIMARY KEY,
    group_id VARCHAR(64) NOT NULL,
    summary TEXT NOT NULL,
    embedding VECTOR(:embedding_dimension),
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
-- 用户画像表
-- 存储用户稳定画像
-- ============================================
CREATE TABLE IF NOT EXISTS komari_memory_user_profile (
    user_id VARCHAR(64) NOT NULL,
    group_id VARCHAR(64) NOT NULL,
    version INT NOT NULL DEFAULT 1 CHECK (version >= 1),
    display_name TEXT NOT NULL,
    traits JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    importance INT DEFAULT 4 CHECK (importance BETWEEN 1 AND 5),
    access_count INT DEFAULT 0 CHECK (access_count >= 0),
    last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_komari_memory_user_profile_traits_object
        CHECK (jsonb_typeof(traits) = 'object'),
    PRIMARY KEY (user_id, group_id)
);

-- ============================================
-- 互动历史表
-- 存储用户近期互动记录
-- ============================================
CREATE TABLE IF NOT EXISTS komari_memory_interaction_history (
    user_id VARCHAR(64) NOT NULL,
    group_id VARCHAR(64) NOT NULL,
    version INT NOT NULL DEFAULT 1 CHECK (version >= 1),
    display_name TEXT NOT NULL,
    file_type TEXT NOT NULL DEFAULT '用户的近期对鞠行为备忘录',
    description TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    records JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    importance INT DEFAULT 5 CHECK (importance BETWEEN 1 AND 5),
    access_count INT DEFAULT 0 CHECK (access_count >= 0),
    last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_komari_memory_interaction_history_records_array
        CHECK (jsonb_typeof(records) = 'array'),
    PRIMARY KEY (user_id, group_id)
);

-- ============================================
-- 创建画像表索引
-- ============================================
CREATE INDEX IF NOT EXISTS idx_komari_memory_user_profile_group
ON komari_memory_user_profile(group_id);

CREATE INDEX IF NOT EXISTS idx_komari_memory_user_profile_importance
ON komari_memory_user_profile(importance DESC);

CREATE INDEX IF NOT EXISTS idx_komari_memory_user_profile_display_name
ON komari_memory_user_profile(display_name);

-- ============================================
-- 创建互动历史表索引
-- ============================================
CREATE INDEX IF NOT EXISTS idx_komari_memory_interaction_history_group
ON komari_memory_interaction_history(group_id);

CREATE INDEX IF NOT EXISTS idx_komari_memory_interaction_history_importance
ON komari_memory_interaction_history(importance DESC);

CREATE INDEX IF NOT EXISTS idx_komari_memory_interaction_history_display_name
ON komari_memory_interaction_history(display_name);

-- ============================================
-- Scene 持久化表结构已迁移到 komari_decision 运行时维护
-- 避免与 memory 插件中的历史 SQL 副本继续漂移
-- ============================================

-- ============================================
-- 表注释
-- ============================================
COMMENT ON TABLE komari_memory_conversations IS 'Komari Memory 对话总结表 - 存储对话总结和向量表示';
COMMENT ON TABLE komari_memory_user_profile IS 'Komari Memory 用户画像表 - 存储用户稳定画像';
COMMENT ON TABLE komari_memory_interaction_history IS 'Komari Memory 互动历史表 - 存储用户近期互动记录';
