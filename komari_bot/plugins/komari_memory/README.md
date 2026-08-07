# Komari Memory

小鞠记忆与对话插件，负责群聊消息缓冲、对话总结、记忆检索、忘却策略，以及 scene 持久化依赖的底层存储。

## 当前状态

- 插件入口：`komari_bot/plugins/komari_memory/__init__.py`
- 数据访问：`repositories/`
- 核心服务：`services/`
- 后台任务：`handlers/summary_worker.py`、`handlers/forgetting_worker.py`
- Schema 唯一权威：`migrations/` Alembic 版本链（`0001_baseline_full_schema.py`）；`komari_bot/db/vector_storage_schema.py` 仅用于离线渲染对照 SQL

运行时特性：

- 连接统一经 `komari_bot/db/orm_connection.py` 共享引擎 raw 适配层（nonebot-plugin-orm 托管，保留 asyncpg 风格 `$n` SQL）
- 表结构与 HNSW 索引由 Alembic 迁移统一管理，运行时无任何 DDL
- 启动阶段校验向量列维度；维度不匹配时通过迁移脚本升级

历史 `database/init_orm.sql` 已废弃并会直接退出，避免旧表结构被误用。手工预建、
运维或排障时，通过 `scripts/render_memory_schema.py` 从迁移结构生成一次性 SQL。

## 依赖

- PostgreSQL 12+，并安装 `pgvector`；连接由 nonebot-plugin-orm（`SQLALCHEMY_DATABASE_URL`）统一托管
- Redis 5+
- `embedding_provider`
- `llm_provider`
- `config_manager`
- `nonebot_plugin_apscheduler`

## 快速开始

### 1. 配置数据库与 Redis

数据库连接唯一权威是 `SQLALCHEMY_DATABASE_URL`（`.env` / `.env.dev` / `.env.prod` 或进程环境变量，旧 `PG_*` 配置已下线）；Redis 引导配置为 `REDIS_HOST` / `REDIS_PORT` / `REDIS_PASSWORD`（见 `komari_bot/config/redis_config.py`）。

插件动态配置存储在强类型单行表 `komari_memory_config`，首次缺失时由 schema 默认值或 dotenv 初始化。

最小示例：

```json
{
  "plugin_enable": true,
  "group_whitelist": ["123456789"],
  "redis_host": "localhost",
  "redis_port": 6379
}
```

### 2. 启动插件

Bot 启动后：

- 会初始化共享引擎连接租约（nonebot-plugin-orm）
- 表结构由 Alembic 迁移统一管理，无需运行时补齐
- 会校验独立 embedding 表与 provider 维度一致
- 会初始化 Redis 缓冲区管理
- 会注册总结任务和忘却任务

## 手工初始化与迁移

### 从运行时 DDL 生成运维 SQL

大多数场景不需要手工执行。确需预建或排障时，先按当前
`embedding_provider` 维度生成并审阅一次性 SQL：

```bash
poetry run python scripts/render_memory_schema.py \
  --embedding-dimension 512 \
  --output /tmp/komari-memory-schema.sql
```

确认 SQL 与备份后再执行；`ON_ERROR_STOP` 禁止部分成功后继续：

```bash
psql -v ON_ERROR_STOP=1 \
  -h localhost -U your_username -d komari_bot \
  -f /tmp/komari-memory-schema.sql
```

不要执行 `database/init_orm.sql`；它只保留 fail-closed 的迁移提示。

### 对话向量迁移

切换 embedding 模型后，先做 dry-run：

```bash
poetry run python scripts/migrate_embeddings.py --target memory
```

执行真实迁移：

```bash
poetry run python scripts/migrate_embeddings.py --apply --target memory
```

### 单表实体拆表

记忆四层与独立 embedding 表的最终结构已由 Alembic 基线 `0001` 一次性建立；
v1.x 时代的手工拆表脚本（`split_komari_memory_entity_tables.py` 等）已随
v2.0.0 删除，不再需要单独执行。

## 核心能力

### 对话总结

- 群聊消息先进入 Redis 缓冲
- 达到消息数 / token / 时间阈值后，触发总结任务
- 总结结果写入 `komari_memory_conversations`
- 用户画像写入 `komari_memory_user_profile`
- 跨群互动事件写入 `komari_memory_interaction_history`，向量写入独立 embedding 表

### 记忆检索

- 用 `embedding_provider` 生成查询向量
- 在 `komari_memory_conversations` 上做 pgvector 检索
- 如启用 rerank，会先取更大的候选集，再只对最终命中结果更新访问时间与重要性

### 忘却策略

忘却任务每天凌晨 4 点运行，核心规则：

- `importance_current` 使用整数模型，每天执行 `-1`，最低降到 `0`
- 记忆被命中后，`importance_current` 直接恢复为 `importance_initial`
- `importance_initial <= forgetting_importance_threshold` 的低价值记忆，归零后会直接删除
- `importance_initial > forgetting_importance_threshold` 的高价值记忆，第一次归零会交给 LLM 模糊化并重置重要性，下一次归零再删除
- `forgetting_fuzzify_concurrency`：首次归零模糊化时的并发上限
- `forgetting_min_age_days`：未达到最小保留天数的记忆不会被删除或模糊化

### scene 持久化依赖

`scene` 运行时位于 `komari_decision`，其四张表（`komari_decision_scenes` /
`komari_memory_scene_set` / `komari_memory_scene_item` /
`komari_memory_scene_runtime`）为 SQLModel ORM 模型，经
nonebot-plugin-orm `get_session` 访问；embedding 生成仍依赖
`komari_memory` 提供的服务。

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
| `profile_trait_limit` | `20` | 每个用户画像允许保留的长期稳定 traits 最大数量 |
| `message_buffer_size` | `200` | Redis 缓冲大小 |
| `memory_search_limit` | `3` | 记忆检索数量 |
| `context_messages_limit` | `10` | 最近上下文消息数 |
| `knowledge_enabled` | `true` | 是否启用常识库联动 |
| `knowledge_limit` | `3` | 常识库检索数量 |

其中 `summary_token_threshold` 用于决定“什么时候触发总结”，`profile_trait_limit` 用于限制“每个用户画像最终最多保留多少条长期稳定 traits”。画像 Agent 会在显式提交前校验该上限，超限时要求先压缩再提交。

### 忘却策略

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `forgetting_enabled` | `true` | 是否启用忘却 |
| `forgetting_importance_threshold` | `3` | 低价值记忆直接删除阈值，高于该值的记忆首次归零会先模糊化 |
| `forgetting_decay_factor` | `0.95` | 兼容旧配置，当前整数忘却模型未使用 |
| `forgetting_access_boost` | `1.2` | 兼容旧配置，当前整数忘却模型未使用 |
| `forgetting_min_age_days` | `3` | 最小保留天数 |
| `forgetting_fuzzify_concurrency` | `3` | 首次归零模糊化时的 LLM 最大并发数 |

### 主动回复与判定

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `proactive_enabled` | `false` | 是否启用主动回复 |
| `proactive_score_threshold` | `0.8` | 主动回复阈值 |
| `proactive_cooldown` | `300` | 主动回复确认送达后的冷却时间（秒） |
| `proactive_max_per_hour` | `400` | 最近一小时最大主动回复次数，包含生成中的预占 |
| `proactive_reservation_ttl_seconds` | `360` | 生成与发送阶段的 Redis 预占有效期（秒） |
| `reply_threshold` | `0.72` | 回复阈值 |
| `noise_conf_threshold` | `0.76` | NOISE 置信度阈值 |
| `noise_margin_threshold` | `0.1` | NOISE 领先阈值 |
| `call_margin_threshold` | `0.08` | call intent 领先阈值 |

非强制主动回复会在调用 LLM 前通过 Redis Lua 原子检查冷却与最近一小时名额，并用平台消息 ID 建立有 TTL 的预占。生成失败、回复为空或 QQ 发送失败会幂等释放预占；发送成功后将同一预占原子确认为一小时滑动窗口记录，并开始正式冷却。进程中断遗留的生成中预占会在 TTL 到期后由下一次操作清理。

### 视觉图片下载

当前消息和引用消息中的图片共用一组下载预算，引用图片优先进入预算。动态配置更新后立即生效。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `vision_image_download_max_count` | `4` | 单条消息最多下载的图片总数 |
| `vision_image_download_max_bytes` | `8388608` | 单张图片响应体上限（字节） |
| `vision_image_download_total_max_bytes` | `20971520` | 单条消息全部图片响应体累计上限（字节） |
| `vision_image_download_max_pixels` | `40000000` | 单张静态图或动画全部帧的累计像素上限 |
| `vision_image_download_concurrency` | `2` | 下载并发上限 |
| `vision_image_download_connect_timeout_seconds` | `5` | 单次连接超时（秒） |
| `vision_image_download_read_timeout_seconds` | `30` | 单次响应读取停顿超时（秒） |
| `vision_image_download_total_timeout_seconds` | `45` | 整批图片下载总时限（秒） |

下载器只允许 HTTP/HTTPS 的 80/443 端口；DNS 地址在实际建连阶段校验，任何内网、回环、链路本地或保留地址都会拒绝。每一跳重定向都会重新经过相同边界。响应头和文件后缀不作为图片类型依据，下载完成后会按真实文件格式、完整解码结果及像素规模验图，只接受 JPEG、PNG、GIF 和 WebP。

## 对外使用方式

`komari_memory` 没有稳定的用户命令接口，主要作为内部服务插件使用。

其他插件通常通过：

- `komari_bot.plugins.komari_memory.get_plugin_manager()`

拿到 `PluginManager`，再访问：

- `manager.memory`
- `manager.pg_pool`（`SharedEngineConnectionPool`：nonebot-plugin-orm 共享引擎的 asyncpg 兼容连接租约，`acquire()` 借出底层连接）
- `manager.redis`

`komari_decision` 的 scene 子系统通过 `get_session()` 访问自己的 ORM 表，不共用记忆插件的连接池。

## 排障

### 插件启动后立即跳过

检查：

- `plugin_enable` 是否为 `true`
- `group_whitelist` 是否配置了目标群
- `SQLALCHEMY_DATABASE_URL` 是否已正确配置（旧 `pg_user` / `pg_password` 已随 v2.0.0 下线）

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
2. 确认 PostgreSQL `komari_decision_scenes` 表中是否有数据（首次需运行 `poetry run python scripts/migrate_komari_decision_scenes_to_pg.py` 或通过管理 API 初始化）
3. `komari_memory_scene_set` / `komari_memory_scene_item` 是否存在 FAILED 记录

### 忘却策略看起来没生效

检查：

- `forgetting_enabled`
- `forgetting_decay_factor`
- `forgetting_access_boost`
- `forgetting_min_age_days`

注意：未达到最小保留天数的记忆不会被删除或模糊化；rerank 模式下也只会刷新最终命中的结果。
