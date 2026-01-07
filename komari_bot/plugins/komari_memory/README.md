# 小鞠记忆 (Komari Memory)

智能记忆与对话插件，支持向量检索和常识库集成。

## 功能特性

- **智能记忆管理**: 自动存储和检索群组对话历史
- **向量语义搜索**: 使用 pgvector 和 fastembed 实现高效的语义检索
- **动态提示词注入**: 结合记忆和常识库生成上下文相关的回复
- **主动回复**: 基于消息评分自动触发对话
- **对话总结**: 自动总结长对话并提取实体信息
- **常识库集成**: 与 komari_knowledge 插件联动

## 依赖要求

### 系统服务

- **PostgreSQL** (>= 12) 需要安装 pgvector 扩展
- **Redis** (>= 5.0)
- **BERT 评分服务** (可选，推荐)

## 安装步骤

### 1. 初始化数据库

连接到 PostgreSQL 数据库并执行以下 SQL：

```sql
-- 安装 pgvector 扩展
CREATE EXTENSION IF NOT EXISTS vector;

-- 创建对话表
CREATE TABLE IF NOT EXISTS komari_memory_conversations (
    id SERIAL PRIMARY KEY,
    group_id VARCHAR(64) NOT NULL,
    summary TEXT NOT NULL,
    embedding VECTOR(512),
    participants TEXT[],
    start_time TIMESTAMP NOT NULL,
    end_time TIMESTAMP NOT NULL,
    importance INT DEFAULT 3,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 创建向量索引
CREATE INDEX idx_komari_memory_conv_embedding ON komari_memory_conversations
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);

-- 创建实体表（由 ORM 自动管理）
-- komari_memory_entity 表会在插件启动时自动创建
```

### 2. 配置插件

在 `config/config_manager/komari_memory_config.json` 中配置：

```json
{
  "plugin_enable": true,
  "pg_host": "localhost",
  "pg_port": 5432,
  "pg_database": "komari_bot",
  "pg_user": "your_username",
  "pg_password": "your_password",
  "redis_host": "localhost",
  "redis_port": 6379,
  "redis_db": 1,
  "redis_password": "",
  "embedding_model": "BAAI/bge-small-zh-v1.5",
  "bert_service_url": "http://localhost:8000/api/v1/score",
  "bert_timeout": 2.0,
  "llm_provider": "gemini",
  "llm_model_chat": "gemini-3-flash-preview",
  "llm_temperature_chat": 1.0,
  "llm_max_tokens_chat": 500,
  "llm_model_summary": "gemini-2.5-flash-lite",
  "llm_temperature_summary": 0.3,
  "llm_max_tokens_summary": 2048,
  "knowledge_enabled": true,
  "knowledge_limit": 3,
  "summary_token_threshold": 1000,
  "summary_time_threshold": 3600,
  "message_buffer_size": 200,
  "proactive_enabled": false,
  "proactive_score_threshold": 0.8,
  "proactive_cooldown": 300,
  "proactive_max_per_hour": 3,
  "system_prompt": "你是小鞠，一个友好的 AI 助手",
  "memory_injection_template": "参考以下信息回复：\n{{CONTEXT}}\n\n用户消息："
}
```

## 配置说明

### 基础配置

| 配置项            | 类型      | 默认值 | 说明                             |
| ----------------- | --------- | ------ | -------------------------------- |
| `plugin_enable`   | bool      | false  | 是否启用插件                     |
| `user_whitelist`  | list[str] | []     | 用户白名单（为空则允许所有用户） |
| `group_whitelist` | list[str] | []     | 群组白名单（为空则禁用所有功能） |

### 数据库配置

| 配置项           | 类型 | 默认值     | 说明                |
| ---------------- | ---- | ---------- | ------------------- |
| `pg_host`        | str  | localhost  | PostgreSQL 主机地址 |
| `pg_port`        | int  | 5432       | PostgreSQL 端口     |
| `pg_database`    | str  | komari_bot | 数据库名称          |
| `pg_user`        | str  | -          | 数据库用户名        |
| `pg_password`    | str  | -          | 数据库密码          |
| `redis_host`     | str  | localhost  | Redis 主机地址      |
| `redis_port`     | int  | 6379       | Redis 端口          |
| `redis_db`       | int  | 1          | Redis 数据库编号    |
| `redis_password` | str  | -          | Redis 密码          |

### BERT 评分服务配置

| 配置项             | 类型  | 默认值                             | 说明               |
| ------------------ | ----- | ---------------------------------- | ------------------ |
| `bert_service_url` | str   | http://localhost:8000/api/v1/score | BERT 服务地址      |
| `bert_timeout`     | float | 2.0                                | 请求超时时间（秒） |

### LLM 配置

#### 对话模型（用于生成回复）

| 配置项                 | 类型  | 默认值                 | 说明               |
| ---------------------- | ----- | ---------------------- | ------------------ |
| `llm_provider`         | str   | gemini                 | LLM 提供商         |
| `llm_model_chat`       | str   | gemini-3-flash-preview | 对话模型名称       |
| `llm_temperature_chat` | float | 1.0                    | 温度参数 (0.0-2.0) |
| `llm_max_tokens_chat`  | int   | 500                    | 最大 token 数      |

#### 总结模型（用于总结对话）

| 配置项                    | 类型  | 默认值                | 说明          |
| ------------------------- | ----- | --------------------- | ------------- |
| `llm_model_summary`       | str   | gemini-2.5-flash-lite | 总结模型名称  |
| `llm_temperature_summary` | float | 0.3                   | 温度参数      |
| `llm_max_tokens_summary`  | int   | 2048                  | 最大 token 数 |

### 记忆管理配置

| 配置项                    | 类型 | 默认值                 | 说明                     |
| ------------------------- | ---- | ---------------------- | ------------------------ |
| `embedding_model`         | str  | BAAI/bge-small-zh-v1.5 | 向量嵌入模型             |
| `summary_token_threshold` | int  | 1000                   | 触发总结的 Token 阈值    |
| `summary_time_threshold`  | int  | 3600                   | 触发总结的时间阈值（秒） |
| `message_buffer_size`     | int  | 200                    | Redis 消息缓存大小       |

### 常识库集成配置

| 配置项              | 类型 | 默认值 | 说明               |
| ------------------- | ---- | ------ | ------------------ |
| `knowledge_enabled` | bool | true   | 是否启用常识库集成 |
| `knowledge_limit`   | int  | 3      | 常识库检索数量限制 |

### 主动回复配置

| 配置项                      | 类型  | 默认值 | 说明                   |
| --------------------------- | ----- | ------ | ---------------------- |
| `proactive_enabled`         | bool  | false  | 是否启用主动回复       |
| `proactive_score_threshold` | float | 0.8    | 触发主动回复的评分阈值 |
| `proactive_cooldown`        | int   | 300    | 主动回复冷却时间（秒） |
| `proactive_max_per_hour`    | int   | 3      | 每小时最大主动回复次数 |

### 提示词模板配置

| 配置项                      | 类型 | 默认值                       | 说明                                                 |
| --------------------------- | ---- | ---------------------------- | ---------------------------------------------------- |
| `system_prompt`             | str  | 你是小鞠，一个友好的 AI 助手 | 系统提示词                                           |
| `memory_injection_template` | str  | 参考以下信息回复：...        | 记忆注入模板，`{{CONTEXT}}` 会被替换为检索到的上下文 |

## 工作原理

### 消息处理流程

1. **消息接收**: 接收群组消息
2. **BERT 评分**: 调用 BERT 服务对消息进行重要性评分 (0.0-1.0)
3. **分类处理**:
   - **低价值消息** (score < 0.3): 仅存储到 Redis，不计入 token
   - **普通消息** (0.3 ≤ score < 0.8): 存储到 Redis 并计入 token
   - **中断信号** (score ≥ 0.8): 触发主动回复（如果启用）
4. **对话总结**: 当 token 数或时间达到阈值时，自动总结对话

### 记忆检索

- 基于向量相似度检索历史对话
- 检索结果注入到提示词中作为上下文
- 结合常识库提供更丰富的回复

## 使用说明

### 基本使用

插件启用后会自动运行，无需手动命令。

**重要**：插件需要配置 `group_whitelist` 才能工作：
- 如果白名单为空，插件不会处理任何消息（安全模式）
- 只有白名单内的群组才会启用插件功能

- 插件会自动记录群组对话
- 当对话积累到一定量时自动总结
- 如果启用了主动回复，会在高价值消息时自动回复

### 调试

查看日志了解插件运行状态：

```bash
# 启动 NoneBot
nb run

# 查看插件日志
[KomariMemory] 正在初始化组件...
[KomariMemory] PostgreSQL 连接池已建立
[KomariMemory] Redis 连接已建立
[KomariMemory] 向量嵌入模型加载完成
[KomariMemory] 组件初始化完成
```

## 常见问题

### 1. PostgreSQL 连接失败

**错误**: `Connection refused` 或 `password authentication failed`

**解决**: 检查配置文件中的数据库连接信息是否正确

### 2. pgvector 扩展未安装

**错误**: `type "vector" does not exist`

**解决**: 在数据库中安装 pgvector 扩展

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

### 3. BERT 评分服务连接失败

**现象**: 所有消息评分都是 0.5（默认值）

**解决**: 确保 BERT 评分服务正在运行，或暂时使用默认评分

### 4. 向量模型下载失败

**现象**: 启动时卡在"向量嵌入模型加载"

**解决**: 首次运行需要下载模型，请确保网络畅通。模型会缓存到本地。

## 技术架构

### 混合数据库方案

- **ORM (nonebot-plugin-orm)**: 用于实体表 (Entity) 的结构化数据
- **asyncpg**: 用于向量检索的高性能 SQL 操作

### 异步任务

- 使用 `nonebot-plugin-apscheduler` 实现后台定时任务
- 每 5 分钟检查一次是否需要触发对话总结

### 重试机制

- BERT 评分失败时重试 3 次，指数退避
- LLM 调用失败时重试 3 次，指数退避

## 许可证

MIT License

## 贡献

欢迎提交 Issue 和 Pull Request！
