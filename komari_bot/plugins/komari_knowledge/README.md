# Komari Knowledge

小鞠常识库插件，提供关键词精确匹配 + pgvector 语义检索的混合知识检索能力，并可选启动 Streamlit WebUI 管理界面。

## 当前状态

- 插件入口：`komari_bot/plugins/komari_knowledge/__init__.py`
- 核心引擎：`komari_bot/plugins/komari_knowledge/engine.py`
- WebUI：`komari_bot/plugins/komari_knowledge/webui.py`
- 手工初始化 SQL：`komari_bot/plugins/komari_knowledge/init_db.sql`

运行时已经支持自动补齐基础表结构并校验向量维度。
手工执行 SQL 只在需要预建表、手动运维或排障时使用。

## 依赖

- PostgreSQL 12+，并安装 `pgvector`
- `embedding_provider` 插件
- `config_manager` 插件
- 可选：`streamlit`（启用 WebUI 时）

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
  "webui_enabled": true,
  "webui_port": 8502
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
- 如果启用了 WebUI，会自动启动 Streamlit 管理界面

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

## WebUI

默认地址：

- `http://localhost:8502`

手动独立启动：

```bash
streamlit run komari_bot/plugins/komari_knowledge/webui.py
```

WebUI 读取的配置路径：

- `config/config_manager/komari_knowledge_config.json`
- `config/config_manager/database_config.json`

主要功能：

- 新增知识
- 编辑知识
- 删除知识
- 测试检索
- 查看全部知识和关键词

## 对外接口

插件对外暴露的核心接口在 `__init__.py`：

- `search_knowledge(query, limit=None, query_embedding=None)`
- `search_by_keyword(keyword)`
- `add_knowledge(content, keywords, category="general", notes=None)`
- `get_all_knowledge()`
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
| `layer1_limit` | `3` | 关键词匹配返回上限 |
| `layer2_limit` | `2` | 向量检索返回上限 |
| `total_limit` | `5` | 最终总返回上限 |
| `webui_enabled` | `false` | 是否自动启动 WebUI |
| `webui_port` | `8502` | WebUI 端口 |

## 检索原理

1. Layer 1：关键词倒排索引精确匹配
2. Layer 2：pgvector 向量检索补充召回
3. 合并结果后按相似度/来源返回

`KnowledgeEngine` 在启动时会预热关键词索引，并在增删改知识后同步更新内存索引。

## 排障

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

### WebUI 无法连接

检查：

- 插件是否启用 `webui_enabled`
- `streamlit` 是否已安装
- 端口是否被占用
- Bot 启动日志中是否有 `WebUI 已启动` 或相关错误
