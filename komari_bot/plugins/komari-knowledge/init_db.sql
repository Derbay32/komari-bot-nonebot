-- Komari Memory 常识库 - PostgreSQL 数据库初始化脚本
--
-- 使用说明：
-- 1. 确保 PostgreSQL 已安装 pgvector 扩展
-- 2. 执行以下命令启用扩展：
--    CREATE EXTENSION IF NOT EXISTS vector;
-- 3. 执行此脚本创建表和索引

-- ============================================
-- 1. 创建知识库主表
-- ============================================

CREATE TABLE IF NOT EXISTS komari_knowledge (
    -- 主键
    id SERIAL PRIMARY KEY,

    -- 分类信息
    category VARCHAR(50) DEFAULT 'general',

    -- 关键词数组：用于 Layer 1 极速匹配
    -- 示例：'{小鞠,布丁,喜欢}'
    keywords TEXT[],

    -- 实际内容：注入到 Prompt 的文本
    content TEXT NOT NULL,

    -- 向量数据：用于 Layer 2 语义检索
    -- 维度：512 (bge-small-zh-v1.5)
    embedding VECTOR(512),

    -- 元数据
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    -- 备注
    notes TEXT
);

-- ============================================
-- 2. 创建索引
-- ============================================

-- 向量相似度索引（HNSW 算法）
-- 使用余弦相似度（cosine operations）
CREATE INDEX IF NOT EXISTS idx_komari_knowledge_embedding
ON komari_knowledge
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);

-- 关键词倒排索引（GIN）
-- 加速关键词数组查询
CREATE INDEX IF NOT EXISTS idx_komari_knowledge_keywords
ON komari_knowledge
USING gin (keywords);

-- 分类索引
CREATE INDEX IF NOT EXISTS idx_komari_knowledge_category
ON komari_knowledge(category);

-- 创建时间索引（用于数据清理）
CREATE INDEX IF NOT EXISTS idx_komari_knowledge_created_at
ON komari_knowledge(created_at DESC);

-- ============================================
-- 3. 创建更新时间触发器
-- ============================================

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_komari_knowledge_updated_at
BEFORE UPDATE ON komari_knowledge
FOR EACH ROW
EXECUTE FUNCTION update_updated_at_column();

-- ============================================
-- 4. 预置示例数据（可选）
-- ============================================

-- 注意：由于无法在此 SQL 中生成向量，示例数据需要在 WebUI 中录入
-- 建议的初始知识：
-- - 小鞠喜欢布丁
-- - 小鞠是机器人
-- - 温水 = 氪金
-- 等等...

-- ============================================
-- 5. 查询示例
-- ============================================

-- Layer 1: 关键词匹配
-- SELECT id, content FROM komari_knowledge
-- WHERE EXISTS (
--     SELECT 1 FROM unnest(keywords) k
--     WHERE k ILIKE '%布丁%'
-- );

-- Layer 2: 向量相似度检索（需要在应用中生成查询向量）
-- SELECT id, content, 1 - (embedding <=> '[0.1, 0.2, ...]'::vector) as similarity
-- FROM komari_knowledge
-- ORDER BY embedding <=> '[0.1, 0.2, ...]'::vector
-- LIMIT 5;

-- ============================================
-- 完成
-- ============================================

COMMENT ON TABLE komari_knowledge IS '小鞠常识库 - 存储 Bot 的人物设定和世界知识';
COMMENT ON COLUMN komari_knowledge.category IS '知识分类：general/character/setting/plot 等';
COMMENT ON COLUMN komari_knowledge.keywords IS '关键词数组，用于快速匹配';
COMMENT ON COLUMN komari_knowledge.content IS '实际注入到 Prompt 的内容';
COMMENT ON COLUMN komari_knowledge.embedding IS '向量嵌入，用于语义检索';
