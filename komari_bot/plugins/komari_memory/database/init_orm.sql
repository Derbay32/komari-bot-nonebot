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
    embedding VECTOR(512),  -- fastembed bge-small-zh-v1.5
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
-- 创建向量索引 (HNSW 算法)
-- ============================================
CREATE INDEX IF NOT EXISTS idx_komari_memory_conv_embedding
ON komari_memory_conversations
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);

-- ============================================
-- 创建其他索引
-- ============================================
CREATE INDEX IF NOT EXISTS idx_komari_memory_conv_group
ON komari_memory_conversations(group_id);

CREATE INDEX IF NOT EXISTS idx_komari_memory_conv_time
ON komari_memory_conversations(start_time DESC);

-- ============================================
-- 表注释
-- ============================================
COMMENT ON TABLE komari_memory_conversations IS 'Komari Memory 对话总结表 - 存储对话总结和向量表示';
