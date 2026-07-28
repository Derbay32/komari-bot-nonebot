# Komari Knowledge

小鞠常识库插件，提供关键词精确匹配 + pgvector 语义检索的混合知识检索能力。

> 2026-04 管理接口迁移说明：
> 知识库、记忆库、reply 日志的管理 API 与 Swagger/OpenAPI 文档已统一迁移到 `komari_management` 插件。
> 旧的 `api_enabled`、`api_token`、`api_allowed_origins`、`webui_*` 配置说明仅供历史参考，不再作为当前实现的生效来源。

## 当前状态

- 插件入口：`komari_bot/plugins/komari_knowledge/__init__.py`
- 核心引擎：`komari_bot/plugins/komari_knowledge/engine.py`
- REST API：`komari_bot/plugins/komari_knowledge/api.py`
- 路由挂载：`komari_bot/plugins/komari_knowledge/api_runtime.py`
- 数据模型：`komari_bot/plugins/komari_knowledge/models.py`
- 手工初始化 SQL：`komari_bot/plugins/komari_knowledge/init_db.sql`

运行时会自动补齐基础表结构并校验向量维度。手工 SQL 只在预建表、运维或排障时需要。

## 依赖

- PostgreSQL 12+，并安装 `pgvector`
- `embedding_provider` 插件
- `config_manager` 插件
- `nonebot2[fastapi]`

## 快速开始

### 1. 配置数据库

共享数据库配置默认读取：

- `config/config_manager/database_config.json`

`komari_knowledge` 也支持在本地配置里用 `pg_*` 字段覆盖共享配置：

- `config/config_manager/komari_knowledge_config.json`

最小可用示例：

```json
{
  "plugin_enable": true,
  "pg_user": "your_username",
  "pg_password": "your_password",
  "api_enabled": true,
  "api_token": "replace-with-a-secret-token",
  "api_allowed_origins": [
    "http://localhost:3000"
  ]
}
```

### 2. 配置 embedding_provider

向量维度来自 `embedding_provider`，默认配置文件：

- `config/config_manager/embedding_provider_config.json`

默认本地模型配置对应 512 维向量；如果切换模型或 API，请保持知识库和记忆库使用同一 provider 配置。

### 3. 启动插件

Bot 启动后：

- 会初始化 `KnowledgeEngine`
- 会按当前 embedding 维度自动补齐 `komari_knowledge` 表结构
- 会校验 `komari_knowledge.embedding` 与当前 provider 维度是否一致
- 在 FastAPI 驱动下，如果配置了 `api_token`，会自动挂载管理 REST API

## 手工初始化与迁移

### 手工初始化 SQL

大多数场景不需要手工执行；若需要，可运行：

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

- `/api/komari-knowledge/v1`

### 启用条件

只有在以下条件都满足时才会挂载管理接口：

- `plugin_enable = true`
- `api_enabled = true`
- `api_token` 非空
- 当前 NoneBot 驱动是 FastAPI

### 鉴权

所有管理接口都要求：

- `Authorization: Bearer <api_token>`

缺失或错误 Token 返回 `401`。

### CORS

外部 WebUI 需要的跨域来源请配置到：

- `api_allowed_origins`

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
  "http://localhost:8080/api/komari-knowledge/v1/knowledge?limit=20&offset=0"
```

按关键词/内容搜索知识列表：

```bash
curl -H "Authorization: Bearer replace-with-a-secret-token" \
  "http://localhost:8080/api/komari-knowledge/v1/knowledge?q=布丁&category=character"
```

新增知识：

```bash
curl -X POST \
  -H "Authorization: Bearer replace-with-a-secret-token" \
  -H "Content-Type: application/json" \
  "http://localhost:8080/api/komari-knowledge/v1/knowledge" \
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
  "http://localhost:8080/api/komari-knowledge/v1/knowledge/1" \
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
  "http://localhost:8080/api/komari-knowledge/v1/search" \
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
| `pg_host` / `pg_port` / `pg_database` / `pg_user` / `pg_password` | `None` | 可选：覆盖共享数据库配置 |
| `similarity_threshold` | `0.65` | 向量检索最低相似度阈值 |
| `query_rewrite_rules` | `{"你": "小鞠", "您的": "小鞠的"}` | 查询重写规则 |
| `layer1_limit` | `3` | Layer 1 关键词匹配返回上限 |
| `layer2_limit` | `2` | Layer 2 向量检索返回上限 |
| `total_limit` | `5` | 最终总返回上限 |
| `api_enabled` | `true` | 是否启用 REST 管理接口 |
| `api_token` | `""` | REST 管理接口 Bearer Token |
| `api_allowed_origins` | `[]` | 允许跨域访问接口的前端 Origin 白名单 |
| `webui_enabled` | `false` | 已废弃，仅为兼容旧配置保留 |
| `webui_port` | `8502` | 已废弃，仅为兼容旧配置保留 |

## 检索原理

1. Layer 1：关键词倒排索引精确匹配
2. Layer 2：pgvector 向量检索补充召回
3. 合并结果后按相似度/来源返回

`KnowledgeEngine` 在启动时会预热关键词索引，并在增删改知识后同步更新内存索引。

## 排障

### 管理 API 未挂载

检查：

- `plugin_enable` 是否为 `true`
- `api_enabled` 是否为 `true`
- `api_token` 是否为空
- `.env` / `env.example` 中的驱动是否仍为 `DRIVER=~fastapi`

### 数据库密码未配置

现象：启动日志提示数据库用户名或密码未配置，插件跳过初始化。

处理：检查：

- `config/config_manager/database_config.json`
- `config/config_manager/komari_knowledge_config.json`

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

- 前端页面 `Origin` 是否加入 `api_allowed_origins`
- 请求是否携带了 `Authorization: Bearer <api_token>`
- 反向代理是否拦截了 `OPTIONS` 预检请求
