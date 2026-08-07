# Komari Knowledge

小鞠常识库插件，提供关键词精确匹配 + pgvector 语义检索的混合知识检索能力。

> 管理接口由 `komari_management` 统一注册并提供具名凭据鉴权、CORS 与 Swagger/OpenAPI 文档。

## 当前状态

- 插件入口：`komari_bot/plugins/komari_knowledge/__init__.py`
- 核心引擎：`komari_bot/plugins/komari_knowledge/engine.py`
- REST API：`komari_bot/plugins/komari_knowledge/api.py`
- 路由挂载：`komari_bot/plugins/komari_management/api_runtime.py`
- 数据模型：`komari_bot/plugins/komari_knowledge/models.py`
- 手工参考 SQL：`komari_bot/plugins/komari_knowledge/init_db.sql`（仅运维参考，不作为运行时 DDL 来源）

表结构与向量列 DDL 由 Alembic 迁移链统一管理（`migrations/versions/0001_baseline_full_schema.py`），运行时不再执行任何建表语句；启动时只校验向量维度。

## 依赖

- PostgreSQL 12+，并安装 `pgvector`；连接由 nonebot-plugin-orm（`SQLALCHEMY_DATABASE_URL`）统一托管
- `embedding_provider` 插件
- `config_manager` 插件
- `nonebot2[fastapi]`

## 快速开始

### 1. 配置数据库

数据库连接唯一权威是 `SQLALCHEMY_DATABASE_URL`（`.env` / `.env.dev` / `.env.prod` 或进程环境变量，旧 `PG_*` 配置已下线）。

`komari_knowledge` 的动态配置存储在强类型单行表 `komari_knowledge_config`（由 `config_manager` 管理）。

最小可用示例：

```json
{
  "plugin_enable": true
}
```

### 2. 配置 embedding_provider

向量维度来自 `embedding_provider` 的动态配置（强类型表 `komari_embedding_provider_config`）。

默认配置对应 512 维向量；如果切换模型或 API 端点，请保持知识库和记忆库使用同一 provider 配置。

### 3. 启动插件

Bot 启动后：

- 会初始化 `KnowledgeEngine`
- 表结构由 Alembic 迁移统一管理，无需运行时补齐
- 会校验 `komari_knowledge.embedding` 与当前 provider 维度是否一致
- `komari_management` 启用且具名凭据有效时，会统一挂载管理 REST API

## 手工初始化与迁移

### 手工初始化 SQL

Schema 唯一权威是 `migrations/` Alembic 版本链，正常运行不需要手工建表；`init_db.sql` 仅保留为运维参考。确需预建或排障时，可运行：

```bash
psql -h localhost -U your_username -d komari_bot \
  -v embedding_dimension=512 \
  -f komari_bot/plugins/komari_knowledge/init_db.sql
```

如果不显式传入 `embedding_dimension`，脚本默认使用当前 provider 默认值 `512`。

### 旧库切换 embedding 维度

切换 embedding 模型后，如果库里已有历史向量，请先执行迁移脚本：

```bash
poetry run python scripts/migrate_embeddings.py
```

上面命令是 dry-run，只打印目标维度、表状态和预计改动。

执行真实迁移：

```bash
poetry run python scripts/migrate_embeddings.py --apply --target knowledge
```

如果记忆库也要一起迁移：

```bash
poetry run python scripts/migrate_embeddings.py --apply --target knowledge --target memory
```

## REST API

默认前缀：

- `/api/v2/komari-knowledge`

### 启用条件

只有在以下条件都满足时才会挂载管理接口：

- `komari_management.plugin_enable = true`
- `komari_management.api_credentials` 至少包含一条有效具名凭据
- 当前 NoneBot 驱动是 FastAPI

### 鉴权

所有管理接口都要求：

- `Authorization: Bearer <management-token>`

缺失或错误凭据返回 `401`；凭据缺少 `knowledge:read` 或 `knowledge:write` 时返回 `403`。

### CORS

外部 WebUI 需要的跨域来源请配置到：

- `komari_management.api_allowed_origins`

例如：

```json
{
  "api_allowed_origins": [
    "http://localhost:3000",
    "https://knowledge.example.com"
  ]
}
```

如果不配置该字段，接口仍可被同源或反向代理调用，但不会额外放开浏览器跨域。

### 接口列表

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/knowledge` | 分页获取知识列表，支持 `q`、`category`、`limit`、`offset` |
| `GET` | `/knowledge/{id}` | 获取单条知识 |
| `POST` | `/knowledge` | 新增知识 |
| `PATCH` | `/knowledge/{id}` | 更新知识；`notes: null` 表示清空备注 |
| `DELETE` | `/knowledge/{id}` | 删除知识 |
| `POST` | `/search` | 测试混合检索 |

### 请求示例

获取知识列表：

```bash
curl -H "Authorization: Bearer replace-with-a-secret-token" \
  "http://localhost:8080/api/v2/komari-knowledge/knowledge?limit=20&offset=0"
```

按关键词/内容搜索知识列表：

```bash
curl -H "Authorization: Bearer replace-with-a-secret-token" \
  "http://localhost:8080/api/v2/komari-knowledge/knowledge?q=布丁&category=character"
```

新增知识：

```bash
curl -X POST \
  -H "Authorization: Bearer replace-with-a-secret-token" \
  -H "Content-Type: application/json" \
  "http://localhost:8080/api/v2/komari-knowledge/knowledge" \
  -d '{
    "content": "小鞠喜欢布丁",
    "keywords": ["小鞠", "布丁"],
    "category": "character",
    "notes": "外部后台录入"
  }'
```

更新知识并清空备注：

```bash
curl -X PATCH \
  -H "Authorization: Bearer replace-with-a-secret-token" \
  -H "Content-Type: application/json" \
  "http://localhost:8080/api/v2/komari-knowledge/knowledge/1" \
  -d '{
    "content": "小鞠超喜欢布丁",
    "notes": null
  }'
```

测试混合检索：

```bash
curl -X POST \
  -H "Authorization: Bearer replace-with-a-secret-token" \
  -H "Content-Type: application/json" \
  "http://localhost:8080/api/v2/komari-knowledge/search" \
  -d '{
    "query": "小鞠喜欢吃什么？",
    "limit": 5
  }'
```

## 对外接口

插件对外暴露的核心接口在 `__init__.py`：

- `search_knowledge(query, limit=None, query_embedding=None)`
- `search_by_keyword(keyword)`
- `add_knowledge(content, keywords, category="general", notes=None)`
- `get_knowledge(kid)`
- `list_knowledge(limit, offset, query=None, category=None)`
- `get_all_knowledge()`
- `update_knowledge(kid, ...)`
- `delete_knowledge(kid)`

示例：

```python
from nonebot.plugin import require

knowledge = require("komari_knowledge")

results = await knowledge.search_knowledge("小鞠喜欢什么？")
for item in results:
    print(item.category, item.content, item.source)
```

## 配置项

核心配置定义见：

- `komari_bot/plugins/komari_knowledge/config_schema.py`

常用项如下：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `plugin_enable` | `false` | 插件总开关 |
| `similarity_threshold` | `0.65` | 向量检索最低相似度阈值 |
| `query_rewrite_rules` | `{"你": "小鞠", "您的": "小鞠的"}` | 查询重写规则 |
| `layer1_limit` | `3` | Layer 1 关键词匹配返回上限 |
| `layer2_limit` | `2` | Layer 2 向量检索返回上限 |
| `total_limit` | `5` | 最终总返回上限 |

## 检索原理

1. Layer 1：关键词倒排索引精确匹配
2. Layer 2：pgvector 向量检索补充召回
3. 合并结果后按相似度/来源返回

`KnowledgeEngine` 在启动时会预热不可变关键词索引快照。`komari_knowledge`
表的语句级触发器会在同一事务中递增 `komari_search_index_versions` 版本；
本 worker 写入后立即检查并重建，其他 worker 最多 1 秒发现版本变化并原子替换快照。
并发重建由单飞锁合并，加载失败时继续保留上一份完整快照。

## 排障

### 管理 API 未挂载

检查：

- `komari_management.plugin_enable` 是否为 `true`
- `komari_management.api_credentials` 是否至少包含一条有效凭据
- `.env` / `env.example` 中的驱动是否仍为 `DRIVER=~fastapi`

### 数据库连接未配置

现象：启动日志提示未配置 `SQLALCHEMY_DATABASE_URL`，插件跳过初始化。

处理：检查：

- `.env` / `.env.prod` 是否设置了 `SQLALCHEMY_DATABASE_URL`（旧 `PG_USER`、`PG_PASSWORD` 等字段已随 v2.0.0 下线）
- `komari_knowledge_config` 表中是否已有可用的 `komari_knowledge` 配置

### 向量维度不匹配

现象：启动时报知识库向量列维度与当前 embedding provider 不一致。

处理：先执行 dry-run：

```bash
poetry run python scripts/migrate_embeddings.py --target knowledge
```

确认无误后执行：

```bash
poetry run python scripts/migrate_embeddings.py --apply --target knowledge
```

### 外部 WebUI 跨域失败

检查：

- 前端页面 `Origin` 是否加入 `komari_management.api_allowed_origins`
- 请求是否携带了 `Authorization: Bearer <management-token>`
- 反向代理是否拦截了 `OPTIONS` 预检请求
