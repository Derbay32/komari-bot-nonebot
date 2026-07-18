# komari-bot AI 上下文文档

> **AI 智能体在处理本项目任何任务前必须先阅读此文件。**

## 项目概述

komari-bot 是基于 [NoneBot2](https://github.com/nonebot/nonebot2) 构建的 QQ 机器人，核心角色是《败犬女主太多了》中的 **小鞠知花**。

**核心能力**：AI 聊天（LLM 驱动）、四层记忆系统、RAG 知识库、智能帮助、群聊总结、角色绑定、好感度系统、主动回复判定。

## 技术栈速览

| 层次 | 技术 | 说明 |
|------|------|------|
| 语言 | Python **3.13+**（禁止兼容旧版） | 强制使用 `X \| Y`、`list[T]`、`match-case` |
| 包管理 | Poetry | `pyproject.toml` + `poetry.lock` |
| Bot 框架 | NoneBot2 >=2.4.4 | 插件通过 `require()` 声明依赖 |
| 适配器 | OneBot V11 | QQ 协议适配 |
| Web | FastAPI（内嵌于 NoneBot2） | 管理 API、知识库 WebUI |
| 数据库 | PostgreSQL + **pgvector** | raw SQL（无 ORM），HNSW 向量索引 |
| 缓存 | Redis >=7.1.0 | `redis.asyncio`（**禁止**使用 `aioredis`） |
| LLM | OpenAI 兼容接口 | DeepSeek / Gemini 双后端 |
| Embedding | OpenAI 兼容 API（远程） | 默认 `BAAI/bge-small-zh-v1.5` |
| 部署 | Docker + Docker Compose | Gunicorn + Uvicorn |
| CI/CD | Forgejo CI → Codeberg 容器注册表 | 发布 tag 自动构建 |
| Lint | Ruff (py313) + Pyright `standard` | 零容忍类型错误 |

| 层次 | 技术 | 说明 |
|------|------|------|
| 语言 | Python **3.13+**（禁止兼容旧版） | 强制使用 `X \| Y`、`list[T]`、`match-case` |
| 包管理 | Poetry | `pyproject.toml` + `poetry.lock` |
| Bot 框架 | NoneBot2 >=2.4.4 | 插件通过 `require()` 声明依赖 |
| 适配器 | OneBot V11 | QQ 协议适配 |
| Web | FastAPI（内嵌于 NoneBot2） | 管理 API、知识库 WebUI |
| 数据库 | PostgreSQL + **pgvector** | raw SQL（无 ORM），HNSW 向量索引 |
| 缓存 | Redis >=7.1.0 | `redis.asyncio`（**禁止**使用 `aioredis`） |
| LLM | OpenAI 兼容接口 | DeepSeek / Gemini 双后端 |
| Embedding | OpenAI 兼容 API（远程） | 默认 `BAAI/bge-small-zh-v1.5` |
| 部署 | Docker + Docker Compose | Gunicorn + Uvicorn |
| CI/CD | Forgejo CI → Codeberg 容器注册表 | 发布 tag 自动构建 |
| Lint | Ruff (py313) + Pyright `standard` | 零容忍类型错误 |

## 目录结构

```
komari-bot/
├── AGENTS.md                             # ← 本文件
├── pyproject.toml                        # 项目元数据、依赖、ruff/pyright 配置
├── Dockerfile / docker-compose.yml       # 容器化部署
├── .env / .env.dev / .env.prod           # 环境变量（SUPERUSERS, SENTRY_DSN 等）
│
├── komari_bot/                           # ★ 核心代码
│   ├── common/                           # 共享工具层（无 NoneBot 依赖）
│   │   ├── database_config.py            #   Postgres/Redis 配置 Schema
│   │   ├── postgres.py                   #   asyncpg 连接池创建
│   │   ├── vector_storage_schema.py      #   pgvector DDL 构建（HNSW）
│   │   ├── management_api.py             #   Bearer Token 鉴权 + CORS
│   │   ├── prompt_storage.py             #   Prompt 专用 PG 表与运行时加载
│   │   ├── profile_compaction.py         #   用户画像 LLM 压缩
│   │   ├── onebot_rules.py               #   group_message_rule() 等
│   │   └── sentry_support.py             #   Sentry 初始化 + 异常过滤
│   └── plugins/                          # NoneBot 插件模块
│
├── docs/
│   ├── config/                           #   旧版配置归档（迁移输入源）
│   ├── local/                            #   本地工具脚本
│   ├── reviews/                          #   代码审查记录
│   ├── handoff.md                        #   任务交接记录
│   └── *.md                              #   组件文档
│
├── data/ / scripts/ / tools/ / tests/    # 数据 / 脚本 / 工具 / 测试
└── logs/                                 # 运行时日志（含 LLM 请求 trace）
```

## 插件架构与依赖关系

### 插件分层

插件的 `require()` 声明就是硬依赖，修改前必须理解依赖链。

```
基础服务层（被依赖，不应依赖业务插件）
  config_manager ───────────── 动态配置存储（PostgreSQL + .env 初始化）
  permission_manager ───────── 权限检查（白名单、插件开关、SUPERUSER）
  user_ban ─────────────────── 全局 QQ 用户封禁（chat / command）
  embedding_provider ───────── 向量化 + Rerank 服务
  llm_provider ─────────────── LLM 网关（DeepSeek/OpenAI 兼容）
  komari_search ────────────── Tavily 联网搜索服务
  user_data ────────────────── 当前好感度 PostgreSQL 服务

核心功能层
  komari_memory ────────────── 四层记忆系统
  komari_decision ──────────── 回复/记忆判定引擎
  komari_chat ──────────────── AI 聊天处理器（编排者）
  komari_knowledge ─────────── RAG 知识库
  komari_help ──────────────── 智能帮助系统
  group_history_summary ────── 群聊历史总结

辅助功能层
  character_binding ────────── .nn 昵称指令（普通用户 self-only；跨用户管理入口已移至 `.debug bind ...`）
  sr ───────────────────────── 神人榜抽签
  komari_custom ────────────── .custom 知识库提案与投票采纳
  komari_sentry ────────────── Sentry 集成
  komari_management ────────── 管理 REST API
  komari_debug ─────────────── SUPERUSER 调试命令（好感度/绑定/回复干跑/总结诊断）
```

### 数据流路径

```
群消息 → komari_chat（MessageHandler）
         ├─ 调用 user_ban 判断是否允许实际聊天回复
         ├─ 调用 komari_memory 获取记忆上下文
         ├─ 调用 komari_decision 判定回复策略
         ├─ 可选调用 komari_search 工具查询实时信息
         ├─ 调用 llm_provider 生成回复
         └─ 调用 komari_memory 写入新记忆

用户事件 → user_ban（run_preprocessor）
         ├─ SUPERUSER 或无可靠 QQ 号 → 直接放行
         ├─ komari_chat → 由聊天流程单独检查 chat 封禁
         └─ 其他 matcher → 检查 command 封禁，静默清空处理链并保留 block

.custom 提案流程
群消息 → komari_custom
         ├─ Redis 编辑会话（多步标题/正文编辑）
         ├─ PostgreSQL 提案表（publishing/failed/voting/approving/approved 状态机）
         ├─ 稳定编辑会话幂等键 + 发布租约（失败重试复用同一 proposal）
         ├─ 平台消息 ID 先暂存 Redis，数据库回填失败时无重复发送地恢复
         ├─ 表情反应监听 + fetch_emoji_like 补偿拉取
         └─ 投票达标 → komari_knowledge.add_knowledge() 写入知识库

.debug 调试命令流程
SUPERUSER 消息 → komari_debug（命令处理器）
         ├─ 运行时 await SUPERUSER(bot, event) 校验（不在 matcher 层）
         ├─ favor — 调用 user_data.get/set_user_favorability
         ├─ bind — 通过 character_binding.get_binding_manager() 操作；list 明细默认私聊
         ├─ reply — 调用 komari_chat.generate_debug_reply，走纯读取/生成核心，
         │         跳过决策引擎、Redis push、好感度 adjust、互动写入、冷却/频控，
         │         读取真实群上下文与附图/引用消息；完整诊断报告仅私聊 SUPERUSER，
         │         群内默认只发 request ID/状态，`--public` 仅追加二次脱敏摘要
         └─ summary — 调用 group_history_summary.execute_group_summary 共享服务，
                      总结图片与完整诊断报告按顺序私聊 SUPERUSER；群内遵循相同回执/脱敏规则
```

## 核心机制详解

### 1. 配置管理 (`config_manager`)

- **存储源**：业务插件动态配置统一存储在 PostgreSQL `komari_plugin_configs`
- **Prompt 配置**：字符串 prompt 不存入 `komari_plugin_configs`，统一使用独立 PostgreSQL 表 `komari_prompt_configs`
- **初始化**：PG 中缺失配置时，从 `.env` 环境变量 / Pydantic 默认值生成并写入 PG
- **持久化**：`update_field()` → Pydantic 校验 → 写回 PostgreSQL
- **线程安全**：唯一工厂注册表仅锁定实例创建；各配置资源使用独立同步/异步锁，互不串行
- **资源清理**：同步兼容桥的专用事件循环线程在应用关闭时依次关闭连接池、停止循环、join 线程并关闭 loop
- **管理元数据**：受 `komari_management` 管理的 Schema 必须在 `model_config.json_schema_extra.default_apply_mode` 声明默认 `immediate | rebuild | restart`；例外字段通过 `Field(json_schema_extra={"apply_mode": ...})` 覆盖
- **秘密字段**：API Token、API Key、密码、凭据和 DSN 必须用 `Field(json_schema_extra={"secret": True})` 显式标记；管理响应中的配置值与可确认生效值均只返回掩码
- **生效状态**：管理配置详情通过 `field_states` 返回配置来源、生效来源和 `restart_required`；无法观测的启动/服务快照以 `effective_value=null` 表示，禁止宣称已即时生效
- **使用模式**：
  ```python
  from komari_bot.plugins.config_manager import get_config_manager
  config = get_config_manager("plugin_name", MyConfigSchema)
  value = config.get().some_field       # 运行时获取
  config.update_field("some_field", x)  # 更新并持久化
  ```
- 列表、字典等读改写字段必须使用 `mutate_field_async()`，变换函数会在 CAS 冲突后基于数据库最新值重跑；禁止先 `get_async()` 计算整份新值再 `update_field_async()` 覆盖

### 1.1 Prompt 配置 (`komari_prompt_configs`)

- **存储源**：`komari_chat`、`komari_memory_summary`、`group_history_summary` 三组字符串 prompt 运行时从 PostgreSQL `komari_prompt_configs` 读取。
- **默认值回退**：PG 无记录或读取失败时使用代码内 defaults；读取失败优先回退当前进程缓存，避免聊天主流程中断。
- **管理 API**：`komari_management` Prompt API 的 GET / PUT / PATCH 均读写 `komari_prompt_configs`，响应中 `config_source` 形如 `postgresql:komari_prompt_configs:<resource_id>`，`file_path` 仅保留为 `null` 兼容字段。
- **旧 YAML**：`config/prompts/*.yaml` 不再作为运行时来源；如需保留旧值，显式执行 `scripts/migrate_prompt_config_to_pg.py` 导入。`komari_decision` 的 `komari_memory_scenes.yaml` 已迁移到 PostgreSQL `komari_decision_scenes` 表，运行时默认使用 `PostgresSceneTemplateLoader`；YAML loader 仅供迁移脚本和测试使用。

### 2. LLM 网关 (`llm_provider`)

导出的核心函数（位于 `__init__.py`）：
- `generate_text(prompt, model, ...)` → `str`
- `generate_completion(...)` → `LLMCompletionResultSchema`（含 thinking 内容）
- `generate_text_with_messages(messages, model, ...)` → `str`
- `generate_messages_completion(messages, model, ...)` → `LLMCompletionResultSchema`
- `test_connection()` → `bool`

`LLMCompletionResultSchema` 新增字段：
- `usage: UnifiedUsageSchema | None` — 后端实际返回的 token 用量
- `duration_ms: float | None` — 网关测得的调用耗时（毫秒）

`UnifiedUsageSchema` 字段（全部 `int | None`，`None` 表示后端未报告）：
- `input_tokens`、`cached_input_tokens`、`cache_miss_input_tokens`
- `output_tokens`、`reasoning_output_tokens`、`total_tokens`

用量提取（`openai_compatible_api.py`）：
- 支持对象属性、普通字典、Pydantic `model_extra` 三种形式
- DeepSeek `prompt_cache_hit_tokens` 优先于 OpenAI `prompt_tokens_details.cached_tokens`
- `completion_tokens_details.reasoning_tokens` 映射到 `reasoning_output_tokens`
- 缺失或异常字段不阻断解析，对应位置保留 `None`

诊断模型（`diagnostic.py`）：
- `LLMCallTrace`：call ID、父 call ID、阶段、轮次、模型、finish reason、耗时、usage
- `ToolExecutionTrace`：所属 call ID、工具名、解析参数、状态、错误/结果摘要（不含敏感正文）
- `LLMDiagnosticCollector`：按请求保存调用与工具记录，按阶段及全链路聚合 token，逐字段标注完整性
- collector 由 debug 命令创建并显式向下传递；正常业务调用传 `None`

关键规则：
- `llm_provider` 最底层通过 `apply_llm_security_boundary()` 强制追加不可覆盖的安全 system 边界；知识、网页、群历史、引用、画像、视觉描述和工具结果必须使用 `UntrustedContext` 或统一不可信标签传入，禁止拼进 system prompt
- 不可信上下文必须保留来源类型、来源 ID、信任级别并限制正文长度；工具调用必须使用白名单、对象参数 schema、轮数与总调用预算
- `max_tokens` 必须为 **`int`**（不能是 `float`），默认 8192
- 知识库注入：`enable_knowledge=True` 时自动检索并注入到 system prompt
- 调用日志：所有请求记录到 `logs/llm_provider/`，JSONL 只写入已报告字段

### 2.1 Embedding / Rerank 远程协议

- HTTP 客户端必须同时配置连接、读取和总超时；只对网络中断、超时、限流及 5xx 做有限重试。
- Embedding 响应必须与输入数量一致，`index` 唯一且保持输入顺序，向量维度等于配置值，所有元素均为有限数。
- Rerank 响应的索引必须唯一且在候选范围内，分数必须为有限数，返回条数不得超过 `top_n`。
- 日志只允许模型、数量、字符数、内容哈希、尝试次数、错误类型和状态码；禁止记录输入正文、query、payload、响应正文或 API Key。

### 3. 权限管理 (`permission_manager`)

```python
from komari_bot.plugins.permission_manager import check_runtime_permission
ok, reason = await check_runtime_permission(bot, event, config)
```

- **禁止** 在 matcher 创建时用 `rule=` 做静态权限检查（会捕获模块加载时的旧配置）
- **必须** 在处理器内调用 `check_runtime_permission()` 动态检查
- `SUPERUSERS` 通过 `.env` 配置，白名单等动态配置通过 PostgreSQL 管理

### 3.1 用户封禁 (`user_ban`)

- **持久化**：PostgreSQL 表 `komari_user_bans`，以 `(user_id, ban_scope)` 为主键；记录可选理由与到期时间，空到期时间表示永久
- **运行时缓存**：启动加载有效记录快照；之后每 5 秒仅检查单行 revision，变化时才以 `REPEATABLE READ` 重载全表；每次命中仍即时判断到期时间，存储不可用时故障关闭
- **初始化并发**：Repository 建池/建表和 Service 首份快照都必须单飞，禁止并发首次调用创建多份 pool 或重复全表加载
- **自然解封**：APScheduler 每 30 秒原子删除到期记录并按用户合并发送一次普通文本私信；发送失败不回滚且不重试
- **command 拦截**：全局 `run_preprocessor` 检查除 `komari_chat` 外的用户 matcher，封禁时静默清空 `remain_handlers`，保留 matcher 原有 `block`
- **chat 拦截**：聊天消息仍参与判定和记忆；只有实际准备回复时通过 `reply_allowed=False` 压制生成及全部回复副作用
- **管理入口**：SUPERUSER 命令支持永久或 `m/h/d/w` 临时封禁和理由；统一管理 API 通过 `/api/komari-user-bans/v1` 提供查询、封禁与解封
- **SUPERUSER**：管理命令仅限 SUPERUSER，且 SUPERUSER 运行时始终绕过封禁；管理操作和生命周期变化会尝试发送一次私信

### 3.2 用户数据 (`user_data`)

- **生命周期**：仅通过 NoneBot Driver 的 `on_startup` / `on_shutdown` 钩子初始化与关闭，不使用框架不会识别的模块魔术变量
- **动态禁用**：每次数据库入口（包括已有缓存）都实时检查 `plugin_enable`；禁用时抛出 `UserDataDisabledError`，禁止通过懒加载绕过开关
- **原子初始化**：PostgreSQL 连接池仅在表结构创建成功后发布；建表失败必须立即关闭局部连接池并保持实例未初始化
- **并发清理**：关闭流程与懒初始化共用初始化锁，先清空全局引用再关闭连接池，避免继续分发正在关闭的实例

### 4. 四层记忆系统 (`komari_memory`)

| 层 | 存储 | 表 | 说明 |
|----|------|-----|------|
| 1. 对话摘要 | PG | `komari_memory_conversations` | 向量搜索 + 遗忘模糊化 |
| 2. 用户画像 | PG | `komari_memory_user_profile` | JSONB traits，LLM 压缩 |
| 3. 互动历史 | PG | `komari_memory_interaction_history` | JSONB records，增量更新 |
| 4. 实体知识 | PG | 通过 EntityRepository | 关键词 + 向量检索 |

关键类：`PluginManager` → `MemoryService` → `ConversationRepository` / `EntityRepository` + `ForgettingService`
注意：EntityRepository 是跟 komari_knowledge 共享知识表还是独立管理需确认。

### 4.1 知识与帮助关键词索引

- `komari_knowledge` 与 `komari_help` 使用不可变内存快照，重建完成后一次性替换，查询不得观察到半成品索引。
- PostgreSQL 语句级触发器在业务写入事务内递增 `komari_search_index_versions`；其他 worker 最多 1 秒轮询到变化并重建。
- 初始化与索引重建必须走单飞锁；重建失败继续保留旧快照，关闭时等待在途重建结束后清空。

### 5. 判定引擎 (`komari_decision`)

核心服务：
- `SceneRuntimeService` — 场景生命周期管理
- `SceneAdminService` — 场景运维（CRUD）
- `UnifiedCandidateRerankService` — 候选回复重排序
- `SocialTimingService` — 社交时机判定（主动回复冷却、频控）
- `MessageFilter` — 消息过滤

运行时契约：
- `get_runtime_state()` 明确返回 `ready` / `disabled` / `failed`，禁止再用 `None` 混合表达状态。
- `disabled` 或 `failed` 时，显式 @、文本 @ 别名和回复机器人仍由 `komari_chat` 直通；普通非 @ 消息仅保留必要缓冲，不执行 embedding/rerank，也不主动回复。
- `plugin_enable=true` 但初始化异常或 scene snapshot 缺失必须报告 `failed`；只有 snapshot 可用时才记录 `ready`。

### 6. 聊天处理器拆解 (`komari_chat`)

`message_handler.py` 的 `_attempt_reply()` 已拆分为三个边界：

1. **`_read_buffers()`** — 读取 Redis 现有的 recent/global interaction buffer，可选 `store_current`
2. **`_generate_reply_core()`** — 纯读取/生成核心：查询重写、记忆/画像/好感度读取、prompt 构建、LLM 回复生成；不执行任何副作用；接受可选 `LLMDiagnosticCollector`
3. **`_commit_side_effects()`** — 提交好感度 adjust、AI 消息存储与互动历史写入；主动回复冷却/频控不在这里重复记账

主动回复频控契约：
- 非强制回复在生成前调用 Redis Lua 原子预占，同时检查冷却、最近一小时已确认名额与生成中名额；预占 ID 使用平台消息 ID，重复投递不会重复生成。
- 生成失败、空回复或发送失败调用 `release_proactive_reply()` 幂等释放；发送成功后先调用 `confirm_proactive_reply()`，再提交其他聊天副作用。
- 预占带 `proactive_reservation_ttl_seconds`；进程崩溃后的孤儿预占按 TTL 淘汰。已确认名额进入一小时滑动窗口，释放接口不得撤销已确认名额。

`process_message(..., reply_allowed=False)` 用于 chat 封禁：保留原消息的判定和缓冲写入，但在 `_attempt_reply()` 前返回，并记录 `blocked_by_user_ban`。

公开 debug 入口：
- `komari_chat.generate_debug_reply()` — 以命令发起者身份、当前群上下文执行纯读取/生成，完全跳过决策引擎、表情反应、Redis push、好感度 adjust、互动历史、冷却/频控；使用 `debug-reply-*` trace ID；返回 `DebugReplyResult`（含 collector）
- 底层依赖未初始化时抛出 `RuntimeError`（可展示的错误信息）

视觉服务（`vision_service.py`）：
- 已移除绕过网关的独立 `AsyncOpenAI` 调用，改用 `llm_provider.generate_messages_completion()`
- 保留原 prompt、模型参数、并发信号量、空结果和错误文本语义
- 视觉调用作为 `read_image` 工具的子调用，通过 collector 记录同一 trace
- 图片下载器对当前消息和引用消息应用同一批次预算：默认最多 4 张、单图 8 MiB、总计 20 MiB、并发 2、总时限 45 秒；配置更新即时生效
- 域名必须由 aiohttp 建连阶段的受控 resolver 解析并校验，禁止恢复“预解析后再由客户端重新解析”的 DNS 重绑定窗口；每一跳重定向都执行同样校验
- 图片 MIME 必须来自 Pillow 对真实文件的识别与解码结果，禁止信任响应 `Content-Type` 或 URL 后缀；仅接受 JPEG、PNG、GIF、WebP，并执行累计像素限制

### 7. 群聊总结执行服务 (`group_history_summary`)

`execution_service.py` 提供共享执行服务 `execute_group_summary()`：
- 输入 bot、group ID、bot self ID、自然语言总结要求、动态配置、可选 collector
- 复用 `_running_groups` 集合（非 TOCTOU）+ 共享 `_group_locks` 双重保障
- 正常 handler 必须按“动态开关 → 运行时权限 → 平台能力 → 场景识别”顺序检查；matcher 默认 `block=False`，只有确认接管总结请求后才对当前实例调用 `stop_propagation()`
- debug 入口直接调用共享服务，跳过场景识别与业务权限，但仍执行能力检查与群锁
- 返回结构化 `SummaryExecutionResult`：正文、筛选数、规划结果、图片 base64、过滤标签、时间范围

规划与总结阶段采集诊断：
- 每轮传入 `request_trace_id` 和 `request_phase`
- 规划工具结果摘要仅包含 source、matched count、filters，不含消息正文

### 8. 编码规范（必须遵守）

```python
# ✅ 现代类型注解
def func(x: str | None) -> int | float: ...

# ✅ 内置泛型
items: list[str] = []
mapping: dict[str, int] = {}

# ✅ match-case
match status:
    case "ok": ...
    case "error": ...
    case _: ...

# ✅ ClassVar 标注可变类属性
class Foo:
    _instances: ClassVar[dict[str, "Foo"]] = {}
    _lock: ClassVar[RLock] = RLock()

# ✅ PluginState 模式封装全局状态
class PluginState:
    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None
state = PluginState()

# ❌ 禁止旧写法
# from typing import Union, Optional, List, Dict  # 不要用
# args: str = CommandArg()  # CommandArg 返回 Message，不是 str
```

### 9. 数据库操作模式

```python
# PostgreSQL（通过 asyncpg，无 ORM）
from komari_bot.common.postgres import create_postgres_pool
pg_pool = await create_postgres_pool(config)
async with pg_pool.acquire() as conn:
    rows = await conn.fetch("SELECT * FROM ... WHERE ...", param)

# pgvector
from komari_bot.common.vector_storage_schema import apply_schema_statements
await apply_schema_statements(pg_pool, statements)

# Redis（使用 redis.asyncio，禁止用 aioredis）
import redis.asyncio as aioredis
redis_client = aioredis.Redis(host=..., port=..., db=..., password=...)
```

## 开发流程

```bash
# 严格按 lock 同步开发依赖
poetry sync --with dev

# 类型检查
poetry run pyright

# Lint 检查
poetry run ruff check .

# 测试
poetry run pytest tests/ -v
```

## 关键注意事项

1. **NoneBot2 依赖注入**：`CommandArg()` 返回 `Message` 类型，类型注解错误会导致处理器静默跳过
2. **`FinishedException`**：`nonebot.finish()` 通过抛出该异常终止，不要被 `except Exception` 捕获
3. **权限必须运行时检查**：不要在 matcher 创建时用 `rule=` 做静态权限检查
4. **资源清理**：`close()` 方法必须清理所有资源引用（连接池、模型、文件句柄）
5. **提前返回**：条件检查不通过时添加 `return`，避免继续执行
6. **Python 3.13 特有**：本项目不兼容 Python 3.12 及以下
7. **Sentry 过滤**：NoneBot 控制流异常（StopPropagation 等）已在 `sentry_support.py` 中过滤；breadcrumb、Sentry Logs 和错误事件发送前必须隐藏正文、插值参数、请求数据与堆栈局部变量，用户上下文仅在显式启用 `send_default_pii` 时保留
8. **debug 插件权限**：`komari_debug` 所有子命令在处理器第一行调用 `await SUPERUSER(bot, event)`，绝对不放进 matcher 的 `permission=` 或 `rule=`
9. **debug 无副作用**：`.debug reply` 走 `generate_debug_reply()`，完全不触发 Redis push、好感度 adjust、互动历史写入或冷却/频控
10. **诊断结构与投递**：完整报告绝不包含完整 prompt、reasoning content、base64、历史或画像正文，且只私聊已鉴权 SUPERUSER；群内默认仅返回 request ID/状态，`--public` 仍必须隐藏输入、输出、用户标识、异常正文与工具参数
11. **用户封禁边界**：`chat` 只压制 `komari_chat` 的实际回复；其他用户 matcher 统一属于 `command`，封禁时必须静默且保留原 matcher 的传播阻断语义
12. **内容预算**：用户/管理入口可写文本必须复用 `komari_bot.common.content_budget`；同时检查字符、UTF-8 字节、估算 token 与关键词组合，不得在各插件复制限额或静默截断

## 相关文档

| 文档 | 位置 | 用途 |
|------|------|------|
| 任务交接记录 | `docs/handoff.md` | 历史任务详情、决策记录、注意事项 |
| 组件文档 | `docs/*.md` | 各插件的详细设计文档 |

---

*本文件由 AI 生成于 2026-04-26，最后更新于 2026-07-13。发现不一致请以实际代码为准并更新本文档。*
