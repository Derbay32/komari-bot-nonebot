# 小鞠记忆 (Komari Memory)

智能记忆与对话插件，支持向量检索和常识库集成。

## 功能特性

- **智能记忆管理**: 自动存储和检索群组对话历史
- **向量语义搜索**: 使用 pgvector 和 fastembed 实现高效的语义检索
- **动态提示词注入**: 结合记忆和常识库生成上下文相关的回复
- **主动回复**: 基于消息评分自动触发对话
- **对话总结**: 自动总结长对话并提取实体信息
- **记忆忘却**: 基于重要性评分和访问频率的智能忘却机制
- **常识库集成**: 与 komari_knowledge 插件联动
- **配置热重载**: 支持运行时配置更新，无需重启

## 依赖要求

### 系统服务

- **PostgreSQL** (>= 12) 需要安装 pgvector 扩展
- **Redis** (>= 5.0)
- **BERT 评分服务** (可选，推荐)

## 安装步骤

### 1. 安装 pgvector 扩展

连接到 PostgreSQL 数据库并执行：

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

### 2. 初始化数据库表

执行插件提供的初始化脚本：

```bash
psql -h localhost -U your_username -d komari_bot -f komari_bot/plugins/komari_memory/database/init_orm.sql
```

或者手动执行 `komari_bot/plugins/komari_memory/database/init_orm.sql` 文件中的 SQL 语句。

### 3. 配置插件

在 `config/config_manager/komari_memory_config.json` 中配置：

```json
{
  "plugin_enable": true,
  "group_whitelist": ["123456789", "987654321"],

  "pg_host": "localhost",
  "pg_port": 5432,
  "pg_database": "komari_bot",
  "pg_user": "your_username",
  "pg_password": "your_password",
  "pg_pool_min_size": 2,
  "pg_pool_max_size": 5,

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

  "summary_message_threshold": 50,
  "summary_token_threshold": 1000,
  "summary_time_threshold": 3600,
  "summary_max_messages": 200,
  "message_buffer_size": 200,
  "memory_search_limit": 3,
  "context_messages_limit": 10,

  "proactive_enabled": false,
  "proactive_score_threshold": 0.8,
  "proactive_cooldown": 300,
  "proactive_max_per_hour": 3,

  "forgetting_enabled": true,
  "forgetting_importance_threshold": 3,
  "forgetting_decay_factor": 0.95,
  "forgetting_access_boost": 1.2,

  "system_prompt": "你是小鞠，一个友好的 AI 助手"
}
```

## 配置说明

### 基础配置

| 配置项            | 类型      | 默认值 | 说明                                           |
| ----------------- | --------- | ------ | ---------------------------------------------- |
| `plugin_enable`   | bool      | false  | 是否启用插件                                   |
| `user_whitelist`  | list[str] | []     | 用户白名单（为空则允许所有用户）               |
| `group_whitelist` | list[str] | []     | 群组白名单（为空则禁用所有功能，**必须配置**） |

### 数据库配置

| 配置项             | 类型 | 默认值     | 说明                    |
| ------------------ | ---- | ---------- | ----------------------- |
| `pg_host`          | str  | localhost  | PostgreSQL 主机地址     |
| `pg_port`          | int  | 5432       | PostgreSQL 端口         |
| `pg_database`      | str  | komari_bot | 数据库名称              |
| `pg_user`          | str  | -          | 数据库用户名（必填）    |
| `pg_password`      | str  | -          | 数据库密码（必填）      |
| `pg_pool_min_size` | int  | 2          | 连接池最小连接数 (1-10) |
| `pg_pool_max_size` | int  | 5          | 连接池最大连接数 (1-50) |
| `redis_host`       | str  | localhost  | Redis 主机地址          |
| `redis_port`       | int  | 6379       | Redis 端口              |
| `redis_db`         | int  | 1          | Redis 数据库编号        |
| `redis_password`   | str  | -          | Redis 密码              |

### BERT 评分服务配置

| 配置项             | 类型  | 默认值                             | 说明               |
| ------------------ | ----- | ---------------------------------- | ------------------ |
| `bert_service_url` | str   | http://localhost:8000/api/v1/score | BERT 服务地址      |
| `bert_timeout`     | float | 2.0                                | 请求超时时间（秒） |

### LLM 配置

#### 对话模型（用于生成回复）

| 配置项                 | 类型  | 默认值                 | 范围      | 说明          |
| ---------------------- | ----- | ---------------------- | --------- | ------------- |
| `llm_provider`         | str   | gemini                 | -         | LLM 提供商    |
| `llm_model_chat`       | str   | gemini-3-flash-preview | -         | 对话模型名称  |
| `llm_temperature_chat` | float | 1.0                    | 0.0 - 2.0 | 温度参数      |
| `llm_max_tokens_chat`  | int   | 500                    | 20 - 8192 | 最大 token 数 |

#### 总结模型（用于总结对话）

| 配置项                    | 类型  | 默认值                | 范围      | 说明          |
| ------------------------- | ----- | --------------------- | --------- | ------------- |
| `llm_model_summary`       | str   | gemini-2.5-flash-lite | -         | 总结模型名称  |
| `llm_temperature_summary` | float | 0.3                   | 0.0 - 2.0 | 温度参数      |
| `llm_max_tokens_summary`  | int   | 2048                  | 20 - 8192 | 最大 token 数 |

### 记忆管理配置

| 配置项                      | 类型 | 默认值                 | 范围        | 说明                              |
| --------------------------- | ---- | ---------------------- | ----------- | --------------------------------- |
| `embedding_model`           | str  | BAAI/bge-small-zh-v1.5 | -           | 向量嵌入模型                      |
| `summary_message_threshold` | int  | 50                     | 10 - 500    | 触发总结的消息数量阈值（优先）    |
| `summary_token_threshold`   | int  | 1000                   | 100 - 10000 | 触发总结的 Token 阈值（备用条件） |
| `summary_time_threshold`    | int  | 3600                   | 300 - 86400 | 触发总结的时间阈值（秒）          |
| `summary_max_messages`      | int  | 200                    | 50 - 500    | 总结时从缓冲区获取的最大消息数    |
| `message_buffer_size`       | int  | 200                    | 50 - 1000   | Redis 消息缓存大小                |
| `memory_search_limit`       | int  | 3                      | 1 - 10      | 检索相关记忆的最大数量            |
| `context_messages_limit`    | int  | 10                     | 5 - 50      | 获取最近消息上下文的最大数量      |

### 常识库集成配置

| 配置项              | 类型 | 默认值 | 范围   | 说明               |
| ------------------- | ---- | ------ | ------ | ------------------ |
| `knowledge_enabled` | bool | true   | -      | 是否启用常识库集成 |
| `knowledge_limit`   | int  | 3      | 1 - 10 | 常识库检索数量限制 |

### 主动回复配置

| 配置项                      | 类型  | 默认值 | 范围      | 说明                   |
| --------------------------- | ----- | ------ | --------- | ---------------------- |
| `proactive_enabled`         | bool  | false  | -         | 是否启用主动回复       |
| `proactive_score_threshold` | float | 0.8    | 0.0 - 1.0 | 触发主动回复的评分阈值 |
| `proactive_cooldown`        | int   | 300    | 60 - 3600 | 主动回复冷却时间（秒） |
| `proactive_max_per_hour`    | int   | 3      | 1 - 10    | 每小时最大主动回复次数 |

### 记忆忘却配置

| 配置项                            | 类型  | 默认值 | 范围       | 说明                   |
| --------------------------------- | ----- | ------ | ---------- | ---------------------- |
| `forgetting_enabled`              | bool  | true   | -          | 是否启用记忆忘却       |
| `forgetting_importance_threshold` | int   | 3      | 1 - 5      | 删除低重要性记忆的阈值 |
| `forgetting_decay_factor`         | float | 0.95   | 0.9 - 0.99 | 重要性衰减系数         |
| `forgetting_access_boost`         | float | 1.2    | 1.0 - 2.0  | 访问时重要性提升系数   |
| `forgetting_min_age_days`         | int   | 7      | 1 - 30     | 记忆最小保留天数       |

### 提示词模板配置

| 配置项          | 类型 | 默认值 | 说明       |
| --------------- | ---- | ------ | ---------- |
| `system_prompt` | str  | 略     | 系统提示词 |

## 工作原理

### 消息处理流程

```
群组消息
  ↓
消息预过滤（长度、重复检测）
  ↓
BERT 重要性评分 (0.0-1.0)
  ↓
分类处理:
  ├─ 低价值 (score < 0.3) → 丢弃
  ├─ 普通 (0.3 ≤ score < 阈值) → 存入 Redis
  └─ 高价值 (score ≥ 阈值) → 触发主动回复（如果启用）
  ↓
后台总结任务（每 5 分钟检查）
  ├─ 消息数达到阈值
  ├─ 时间达到阈值
  └─ Token 数达到阈值
  ↓
生成总结并存储到 PostgreSQL
  ↓
记忆忘却任务（每天凌晨 2 点）
  └─ 删除低重要性、旧的记忆
```

### 记忆检索

- 基于向量相似度检索历史对话
- 检索结果注入到提示词中作为上下文
- 结合常识库提供更丰富的回复
- AI 生成回复时包含最近消息上下文（可通过 `context_messages_limit` 配置）

### 配置热重载

插件支持配置热重载，修改配置文件后无需重启：

1. 修改 `config/config_manager/komari_memory_config.json`
2. `config_manager` 插件自动检测文件变化
3. 下次获取配置时自动应用新值

**注意**：部分配置（如数据库连接池）需要重启插件才能生效。

## 使用说明

### 基本使用

插件启用后会自动运行，无需手动命令。

**重要**：插件必须配置 `group_whitelist` 才能工作：

- 如果白名单为空，插件不会处理任何消息（安全模式）
- 只有白名单内的群组才会启用插件功能

### 功能说明

- **自动记录**: 插件会自动记录群组对话
- **智能总结**: 当对话积累到一定量时自动总结
- **主动回复**: 如果启用，会在高价值消息时自动回复
- **记忆忘却**: 自动清理低价值的旧记忆，保持数据库精简

### 调试

查看日志了解插件运行状态：

```bash
# 启动 NoneBot
nb run

# 查看插件日志
[KomariMemory] 正在初始化组件...
[KomariMemory] PostgreSQL 连接池已建立
[KomariMemory] Redis 连接已建立
[KomariMemory] 组件初始化完成
```

### 性能调优

根据实际情况调整配置：

**低负载场景**（小群组、消息量少）：

```json
{
  "pg_pool_min_size": 1,
  "pg_pool_max_size": 3,
  "summary_message_threshold": 30,
  "memory_search_limit": 3
}
```

**高负载场景**（大群组、消息量大）：

```json
{
  "pg_pool_min_size": 5,
  "pg_pool_max_size": 20,
  "summary_message_threshold": 100,
  "memory_search_limit": 5,
  "context_messages_limit": 20
}
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

### 5. 配置修改后不生效

**原因**: 部分配置需要重启插件

**解决**: 重启 NoneBot 以应用新配置

## 技术架构

### 分层架构

```
┌─────────────────────────────────────┐
│         插件入口 (__init__)          │
└─────────────────────────────────────┘
              ↓
┌─────────────────────────────────────┐
│          处理器层 (handlers)         │
│  - message_handler.py   消息处理    │
│  - summary_worker.py   对话总结     │
│  - forgetting_worker.py 忘却任务    │
└─────────────────────────────────────┘
              ↓
┌─────────────────────────────────────┐
│          服务层 (services)           │
│  - memory_service.py  记忆管理      │
│  - llm_service.py      LLM 调用     │
│  - bert_client.py     BERT 评分     │
│  - redis_manager.py   Redis 操作    │
│  - config_interface.py 配置接口     │
└─────────────────────────────────────┘
              ↓
┌─────────────────────────────────────┐
│       数据访问层 (repositories)      │
│  - conversation_repository.py        │
│  - entity_repository.py              │
└─────────────────────────────────────┘
              ↓
┌─────────────────────────────────────┐
│          数据库层                    │
│  - PostgreSQL (pgvector)             │
│  - Redis                             │
└─────────────────────────────────────┘
```

### 设计模式

- **Repository 模式**: 分离数据访问逻辑
- **依赖注入**: 通过构造函数传递配置和依赖
- **中间层模式**: config_interface 封装配置访问
- **装饰器模式**: retry_async 统一重试逻辑

### 核心组件

| 组件               | 职责             |
| ------------------ | ---------------- |
| `PluginManager`    | 插件生命周期管理 |
| `MessageHandler`   | 消息处理主逻辑   |
| `MemoryService`    | 记忆存储与检索   |
| `RedisManager`     | Redis 缓冲区管理 |
| `config_interface` | 配置访问中间层   |
