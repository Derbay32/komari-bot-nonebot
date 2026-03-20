# Komari Memory

小鞠记忆与对话插件，负责群聊消息缓冲、对话总结、记忆检索、忘却策略，以及 scene 持久化依赖的底层存储。

## 当前状态

- 插件入口：`komari_bot/plugins/komari_memory/__init__.py`
- 数据访问：`repositories/`
- 核心服务：`services/`
- 后台任务：`handlers/summary_worker.py`、`handlers/forgetting_worker.py`
- 手工初始化 SQL：`database/init_orm.sql`

运行时已经支持：

- 按当前 `embedding_provider` 维度自动补齐基础表结构
- 启动阶段校验向量列维度
- 维度不匹配时通过迁移脚本升级

手工 SQL 现在主要用于预建表、手工运维或排障，不再是首装必经步骤。

## 依赖

- PostgreSQL 12+，并安装 `pgvector`
- Redis 5+
- `embedding_provider`
- `llm_provider`
- `config_manager`
- `nonebot_plugin_apscheduler`

## 快速开始

### 1. 配置数据库与 Redis

共享数据库配置默认读取：

- `config/config_manager/database_config.json`

插件本地配置：

- `config/config_manager/komari_memory_config.json`

其中 `pg_*` 字段会覆盖共享数据库配置。

最小示例：

```json
{
  "plugin_enable": true,
  "group_whitelist": ["123456789"],
  "pg_user": "your_username",
  "pg_password": "your_password",
  "redis_host": "localhost",
  "redis_port": 6379
}
```

### 2. 启动插件

Bot 启动后：

- 会建立 PostgreSQL 连接池
- 会按当前 embedding 维度自动补齐 `komari_memory_conversations` / `komari_memory_entity`
- 会校验 `komari_memory_conversations.embedding` 与 provider 维度一致
- 会初始化 Redis 缓冲区管理
- 会注册总结任务和忘却任务

## 手工初始化与迁移

### 手工初始化 SQL

大多数场景不需要手工执行；若需要，可运行：

```bash
psql -h localhost -U your_username -d komari_bot \
  -v embedding_dimension=512 \
  -f komari_bot/plugins/komari_memory/database/init_orm.sql
```

如果不显式传入 `embedding_dimension`，脚本默认使用当前 provider 默认值 `512`。

### 对话向量迁移

切换 embedding 模型后，先做 dry-run：

```bash
poetry run python scripts/migrate_embeddings.py --target memory
```

执行真实迁移：

```bash
poetry run python scripts/migrate_embeddings.py --apply --target memory
```

### `komari_memory_entity` 旧结构迁移

如果你的 `komari_memory_entity` 还是旧版多行 key/value 结构，先做 dry-run：

```bash
poetry run python scripts/migrate_komari_memory_entity_to_json.py
```

确认后执行真实迁移：

```bash
poetry run python scripts/migrate_komari_memory_entity_to_json.py --apply
```

如果只想手工应用约束：

```bash
psql -h localhost -U your_username -d komari_bot \
  -f komari_bot/plugins/komari_memory/database/entity_unification_constraints.sql
```

## 核心能力

### 对话总结

- 群聊消息先进入 Redis 缓冲
- 达到消息数 / token / 时间阈值后，触发总结任务
- 总结结果写入 `komari_memory_conversations`
- 用户画像和互动历史写入 `komari_memory_entity`

### 记忆检索

- 用 `embedding_provider` 生成查询向量
- 在 `komari_memory_conversations` 上做 pgvector 检索
- 如启用 rerank，会先取更大的候选集，再只对最终命中结果更新访问时间与重要性

### 忘却策略

忘却任务每天凌晨 4 点运行，核心规则：

- `forgetting_decay_factor`：每日按比例衰减 `importance_current`
- `forgetting_access_boost`：记忆被命中时提升 `importance_current`
- `forgetting_min_age_days`：未达到最小保留天数的记忆不会被删除或模糊化
- 低价值记忆删除，高价值记忆先模糊化，再进入下一轮删除

### scene 持久化依赖

`scene` 运行时已迁到 `komari_decision`，但 PostgreSQL 连接池和共享配置仍由 `komari_memory` 提供依赖。

相关配置：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `scene_persist_enabled` | `false` | 是否启用 scene 持久化 |
| `scene_sync_poll_seconds` | `30` | scene 同步/刷新轮询间隔 |
| `scene_keep_versions` | `3` | 保留的 READY 版本数量 |

## 主要配置

完整定义见：

- `komari_bot/plugins/komari_memory/config_schema.py`

常用项如下：

### 基础与连接

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `plugin_enable` | `false` | 插件总开关 |
| `user_whitelist` | `[]` | 用户白名单 |
| `group_whitelist` | `[]` | 群白名单 |
| `pg_host` / `pg_port` / `pg_database` / `pg_user` / `pg_password` | `None` | 可选：覆盖共享数据库配置 |
| `redis_host` | `localhost` | Redis 主机 |
| `redis_port` | `6379` | Redis 端口 |
| `redis_db` | `1` | Redis DB |

### 总结与检索

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `summary_message_threshold` | `50` | 触发总结的消息数阈值 |
| `summary_token_threshold` | `1000` | 触发总结的 token 阈值 |
| `summary_time_threshold` | `3600` | 触发总结的时间阈值（秒） |
| `summary_max_messages` | `200` | 总结时读取的最大消息数 |
| `summary_chunk_token_limit` | `3000` | 总结前原文分段的估算 token 上限 |
| `message_buffer_size` | `200` | Redis 缓冲大小 |
| `memory_search_limit` | `3` | 记忆检索数量 |
| `context_messages_limit` | `10` | 最近上下文消息数 |
| `knowledge_enabled` | `true` | 是否启用常识库联动 |
| `knowledge_limit` | `3` | 常识库检索数量 |

其中 `summary_token_threshold` 用于决定“什么时候触发总结”，`summary_chunk_token_limit` 用于限制“单次发送给总结模型的原文分段大小”，两者职责不同。

### 忘却策略

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `forgetting_enabled` | `true` | 是否启用忘却 |
| `forgetting_importance_threshold` | `3` | 低价值记忆阈值 |
| `forgetting_decay_factor` | `0.95` | 每日衰减系数 |
| `forgetting_access_boost` | `1.2` | 命中时的访问回升系数 |
| `forgetting_min_age_days` | `3` | 最小保留天数 |

### 主动回复与判定

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `proactive_enabled` | `false` | 是否启用主动回复 |
| `proactive_score_threshold` | `0.8` | 主动回复阈值 |
| `proactive_cooldown` | `300` | 主动回复冷却时间 |
| `proactive_max_per_hour` | `400` | 每小时最大主动回复次数 |
| `reply_threshold` | `0.72` | 回复阈值 |
| `noise_conf_threshold` | `0.76` | NOISE 置信度阈值 |
| `noise_margin_threshold` | `0.1` | NOISE 领先阈值 |
| `call_margin_threshold` | `0.08` | call intent 领先阈值 |

## 对外使用方式

`komari_memory` 没有稳定的用户命令接口，主要作为内部服务插件使用。

其他插件通常通过：

- `komari_bot.plugins.komari_memory.get_plugin_manager()`

拿到 `PluginManager`，再访问：

- `manager.memory`
- `manager.pg_pool`
- `manager.redis`

`komari_decision` 的 scene 子系统就是这样复用 `komari_memory` 的 PostgreSQL 连接池。

## 排障

### 插件启动后立即跳过

检查：

- `plugin_enable` 是否为 `true`
- `group_whitelist` 是否配置了目标群
- 共享数据库配置或本地 `pg_user` / `pg_password` 是否完整

### 向量维度不匹配

先执行 dry-run：

```bash
poetry run python scripts/migrate_embeddings.py --target memory
```

确认后执行：

```bash
poetry run python scripts/migrate_embeddings.py --apply --target memory
```

### scene 开启后没有 active set

检查：

1. `scene_persist_enabled=true`
2. `config/prompts/komari_memory_scenes.yaml` 格式是否正确
3. `komari_memory_scene_set` / `komari_memory_scene_item` 是否存在 FAILED 记录

### 忘却策略看起来没生效

检查：

- `forgetting_enabled`
- `forgetting_decay_factor`
- `forgetting_access_boost`
- `forgetting_min_age_days`

注意：未达到最小保留天数的记忆不会被删除或模糊化；rerank 模式下也只会刷新最终命中的结果。
