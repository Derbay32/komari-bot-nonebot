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
| 适配器 | OneBot V11 + QQ 官方适配器 | 既有 OneBot 功能；可选官 Bot 群 @ 绑定与轮盘 |
| Web | FastAPI（内嵌于 NoneBot2） | 管理 API、知识库 WebUI |
| 数据库 | PostgreSQL + **pgvector**（HNSW 索引） | 连接/会话由 **nonebot-plugin-orm**（SQLAlchemy 2.x）托管，Schema 由 **Alembic 版本链**（`migrations/`）唯一管理；特殊 DDL 表保留 raw SQL |
| 缓存 | Redis >=7.1.0 | `redis.asyncio`（**禁止**使用 `aioredis`） |
| LLM | OpenAI 兼容接口 | DeepSeek / Gemini 双后端 |
| Embedding | OpenAI 兼容 API（远程） | 默认 `BAAI/bge-small-zh-v1.5` |
| 部署 | Docker + Docker Compose | Gunicorn + Uvicorn；prestart 自动 `upgrade head` |
| CI/CD | GitHub Actions → GitHub Container Registry | 所有 PR 执行静态、无服务、PostgreSQL/Redis 集成与迁移验收；发布 tag 自动构建镜像 |
| Lint | Ruff (py313) + Pyright `standard` | 零容忍类型错误 |

## 目录结构

```
komari-bot/
├── AGENTS.md                             # ← 本文件
├── pyproject.toml                        # 项目元数据、依赖、ruff/pyright 配置
├── Dockerfile / docker-compose.yml       # 容器化部署
├── .env / .env.dev / .env.prod           # 环境变量（SUPERUSERS, SENTRY_DSN 等）
│
├── migrations/                           # ★ Schema 唯一权威（Alembic 版本链）
│   ├── env.py                            #   合并 SQLModel.metadata + include_object 守卫
│   └── versions/
│       ├── 0001_baseline_full_schema.py  #   全量基线（26 张既有表，含保留的 2 张 legacy 配置表）
│       ├── 0002_typed_plugin_config_tables.py  # 14 张强类型配置表（0004 补充 komari_chat）
│       ├── 0003_typed_prompt_tables.py   #   3 张强类型 Prompt 表
│       └── 0004_komari_chat_config.py    #   komari_chat 配置表（承接 memory 迁出字段）
│
├── komari_bot/                           # ★ 核心代码
│   ├── core/                             # 核心启动与平台基础件（依赖 NoneBot 运行时）
│   │   ├── nonebot_compat.py             #   NoneBot ForwardRef 兼容补丁（显式安装点）
│   │   ├── project_paths.py              #   项目路径常量（DATA_DIR 等）
│   │   └── sentry_support.py             #   Sentry 初始化 + 异常过滤
│   ├── db/                               # PostgreSQL 边界共享件（经 nonebot-plugin-orm 托管连接）
│   │   ├── orm_bootstrap.py              #   迁移命令引导（upgrade head / check / revision）
│   │   ├── orm_config.py                 #   SQLALCHEMY_DATABASE_URL 解析
│   │   ├── orm_connection.py             #   共享引擎的 asyncpg 兼容 raw 连接适配
│   │   ├── pgvector_schema.py            #   向量列维度校验等辅助
│   │   ├── vector_storage_schema.py      #   pgvector DDL 离线渲染（运行时不执行 DDL）
│   │   ├── sql_like_utils.py             #   LIKE 模式转义等 SQL 工具
│   │   ├── memory_agent_locks.py         #   记忆 Agent 并发锁
│   │   └── versioned_keyword_index.py    #   版本化关键词索引
│   ├── llm/                              # LLM 协议与安全上下文共享件（无 NoneBot 依赖）
│   │   ├── llm_protocol.py               #   RequestApi 等协议类型（避免业务插件 import 网关）
│   │   ├── untrusted_context.py          #   不可信上下文包装与渲染
│   │   ├── content_budget.py             #   内容预算校验（字符/字节/估算 token）
│   │   ├── token_counter.py              #   估算 token 计数
│   │   └── dsv4_instruct.py              #   DSV4 指令注入
│   ├── config/                           # 强类型配置与 Prompt 存储共享件（加载器依赖 NoneBot 运行时）
│   │   ├── typed_config.py               #   强类型配置/Prompt 表基类、注册表、安全加载器
│   │   ├── prompt_storage.py             #   Prompt 强类型表与运行时加载
│   │   └── redis_config.py               #   Redis 共享配置 Schema
│   ├── management/                       # 管理 API 与审计共享件（依赖 FastAPI）
│   │   ├── management_api.py             #   Bearer Token 鉴权 + CORS
│   │   └── management_audit.py           #   管理操作审计
│   ├── memory/                           # 记忆画像共享件（无 NoneBot 依赖）
│   │   ├── profile_compaction.py         #   用户画像 LLM 压缩
│   │   └── profile_operations.py         #   画像 trait 操作
│   ├── onebot/                           # OneBot 消息与规则共享件（依赖 OneBot V11 适配器）
│   │   ├── onebot_messages.py            #   消息工具（plain_text_message 等）
│   │   └── onebot_rules.py               #   group_message_rule() 等
│   └── plugins/                          # NoneBot 插件模块
│       ├── <插件>/config_schema.py       #   动态配置强类型表（SQLModel，15 个资源）
│       ├── <插件>/prompt_schema.py       #   Prompt 强类型表（SQLModel，3 个资源）
│       └── <插件>/orm_models.py          #   业务关系表 ORM 模型（user_ban/character_binding/
│                                          #   user_data/komari_custom/komari_management/komari_decision）
│
├── docs/
│   ├── adr/                              #   架构决策记录（0001–0012）
│   ├── config/                           #   旧版配置归档（迁移输入源）
│   ├── local/                            #   本地工具脚本
│   ├── reviews/                          #   代码审查记录
│   ├── handoff/                          #   任务交接记录
│   └── *.md                              #   组件文档
│
├── data/ / scripts/ / tools/ / tests/    # 数据 / 脚本 / 工具 / 测试
└── logs/                                 # 运行时日志（含 Agent Run JSONL）
```

## 插件架构与依赖关系

### 插件分层

插件的 `require()` 声明就是硬依赖，修改前必须理解依赖链。

跨插件 import 边界（ADR-0006）：`require()` 仅作依赖加载声明，实际引用一律走普通 import；import 只允许指向依赖插件的顶层包暴露面（`__all__`），禁止指向其内部子模块（`services` / `repositories` / `handlers` 等）。唯一豁免：管理插件 import 各插件配置 Schema 以注册管理资源。komari_memory 遵循同一规则并采用顶层暴露面方案：其顶层 `__all__` 暴露配置 Schema 与共享工具符号（`KomariMemoryConfigSchema` / `retry_async` / `MemoryService` / `MessageSchema` / `RedisManager`），komari_chat 经自有 `config_interface` 与上述顶层暴露面读取 memory 配置与共享工具，不属于管理插件豁免。详见 `docs/adr/0006-cross-plugin-top-level-imports-decision-contracts.md`。

```
基础服务层（被依赖，不应依赖业务插件）
  config_manager ───────────── 动态配置存储（PostgreSQL 强类型表 + .env 初始化）
  permission_manager ───────── 权限检查（白名单、插件开关、SUPERUSER）
  user_ban ─────────────────── 全局 QQ 用户封禁（chat / command；ORM 表 + revision 缓存）
  embedding_provider ───────── 向量化 + Rerank 服务
  llm_provider ─────────────── LLM 网关（DeepSeek/OpenAI 兼容）
  agent_run_logger ─────────── 单任务 Agent Run JSONL + PostgreSQL 轻索引（共享引擎 raw 连接）
  komari_search ────────────── 联网搜索与网页抓取服务（Tavily / EXA 双提供者）
  user_data ────────────────── 当前好感度 PostgreSQL 服务（ORM 表）

核心功能层
  komari_memory ────────────── 四层记忆系统
  komari_decision ──────────── 回复/记忆判定引擎
  komari_chat ──────────────── AI 聊天处理器（编排者）
  komari_knowledge ─────────── RAG 知识库
  komari_help ──────────────── 智能帮助系统
  group_history_summary ────── 群聊历史总结

辅助功能层
  character_binding ────────── 应用/群作用域角色名与双协议身份关联；QQ `/bind` 用户向导、OneBot 静默取证；受权 REST 预览/清除修复
  sr ───────────────────────── 神人榜抽签
  komari_custom ────────────── .custom 知识库提案与投票采纳
  komari_sentry ────────────── Sentry 集成
  komari_management ────────── 管理 REST API
  komari_debug ─────────────── SUPERUSER 调试命令（好感度/已确认群角色名/回复干跑/总结诊断/失败通知开关）
  komari_roulette ──────────── QQ 群俄罗斯轮盘（纯领域规则 + PG 事务/收据/一次履约 + 恢复维护；默认关闭）
```

### 数据流路径

```
群消息 → komari_chat（MessageHandler）
         ├─ 调用 user_ban 判断是否允许实际聊天回复
         ├─ 调用 komari_memory 获取记忆上下文
         ├─ 调用 komari_decision 判定回复策略
         ├─ 可选调用 komari_search 工具查询实时信息（search_web 搜索 / fetch_page 抓取网页正文）
         ├─ 调用 llm_provider 生成回复
         ├─ 由 agent_run_logger 汇总一条完整 Agent Run 日志
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
         └─ notify — on|off 切换 komari_memory.error_notify_enabled（回复失败 SUPERUSER 通知）
```

## 核心机制详解

### 1. 配置管理 (`config_manager`)

- **存储源**：业务插件动态配置统一存储在 PostgreSQL 强类型单行表 `komari_<插件>_config`（15 张，迁移 0002 建表、0004 为 komari_chat 补充）；每张表固定主键 `id=1`、CAS 修订号 `revision`（写操作原子自增）、写入时间 `updated_at`（存储层显式赋值）；可扩展字段（列表/字典/嵌套配置）保留 JSONB 列
- **komari_chat 专属配置**：主动回复频控与回复送达副作用 outbox 的 10 个字段位于 `komari_chat_config`（迁移 0004 建表，从 `komari_memory_config` 单行迁入，死字段 `proactive_score_threshold` 随批删除）；TSK-192/TSK-193 起回复 Agent 预算（`agent_max_rounds` / `agent_max_tool_calls_per_round` / `agent_max_total_tool_calls`）与工具调用约束模式（`agent_tool_call_mode`）也落在此表；TSK-194/ADR-0010 起图片理解模式（`image_understanding_mode`）与 8 项下载预算（`vision_image_download_*`，迁移 0015）同样归 `komari_chat`，从 `komari_memory_config` 迁出后不保留 alias / 双读 / fallback；运行时经 `komari_chat/services/config_interface.py` 读取，该接口同时提供 `get_memory_config()` 承接聊天流程仍依赖的 memory 侧字段，komari_chat 不得 import `komari_memory.services.config_interface`
- **结构真源**：各插件 `config_schema.py` 中的 SQLModel 元数据（`TypedConfigModel` 基类，见 `config/typed_config.py`）；Alembic 迁移环境只按源文件加载 schema（`load_all_typed_config_models()`），不执行插件包 `__init__`、不访问数据库
- **旧 JSONB 表**：`komari_plugin_configs` 在 v2.0.0 保留（仅承载存量数据，运行时不读写），`DROP` 由后续版本的 autogenerate revision 执行
- **Prompt 配置**：字符串 prompt 不存入配置表，统一使用独立强类型表（见 1.1 节）
- **初始化**：PG 中缺失配置时，从 `.env` 环境变量 / Pydantic 默认值生成并写入 PG
- **持久化**：`update_field()` → Pydantic 校验 → 写回 PostgreSQL（revision CAS，冲突时重载重试）
- **跨进程刷新**：应用事件循环上亚秒级轮询各表 `revision` 检测变更；已移除 asyncpg LISTEN/NOTIFY
- **线程安全**：唯一工厂注册表仅锁定实例创建；各配置资源使用独立同步/异步锁，互不串行
- **资源清理**：同步兼容桥的专用事件循环线程在应用关闭时依次关闭一次性短命引擎连接、停止循环、join 线程并关闭 loop；共享引擎归 nonebot-plugin-orm 托管，插件关闭不得 dispose
- **管理元数据**：受 `komari_management` 管理的 Schema 必须在 `model_config.json_schema_extra.default_apply_mode` 声明默认 `immediate | rebuild | restart`；例外字段通过 `Field(json_schema_extra={"apply_mode": ...})` 覆盖
- **秘密字段**：API Token、API Key、密码、凭据和 DSN 必须用 `Field(json_schema_extra={"secret": True})` 显式标记；管理响应中的配置值与可确认生效值均只返回掩码（如 `komari_search.search_api_key`）
- **生效状态**：管理配置详情通过 `field_states` 返回配置来源、生效来源和 `restart_required`；无法观测的启动/服务快照以 `effective_value=null` 表示，禁止宣称已即时生效
- **使用模式**：
  ```python
  from komari_bot.plugins.config_manager import get_config_manager
  config = get_config_manager("plugin_name", MyConfigSchema)
  value = config.get().some_field       # 运行时获取
  config.update_field("some_field", x)  # 更新并持久化
  ```
- 列表、字典等读改写字段必须使用 `mutate_field_async()`，变换函数会在 CAS 冲突后基于数据库最新值重跑；禁止先 `get_async()` 计算整份新值再 `update_field_async()` 覆盖

### 1.1 Prompt 配置 (`komari_prompt_*`)

- **存储源**：`komari_chat`、`komari_memory_summary`、`group_history_summary` 三组字符串 prompt 各对应一张强类型单行表（`komari_prompt_komari_chat` / `komari_prompt_memory_summary` / `komari_prompt_group_history_summary`，迁移 0003 建表），运行时经 `config/prompt_storage.py` 读取；表结构真源是各插件 `prompt_schema.py` 的 `TypedPromptModel` 元数据
- **默认值回退**：PG 无记录或读取失败时使用代码内 defaults；读取失败优先回退当前进程缓存，避免聊天主流程中断
- **跨进程刷新**：无 LISTEN/NOTIFY；`PromptTemplateLoader` 缓存自带 1 秒陈限上限，本进程写入经 `register_invalidator` 回调立即失效本地缓存，传播延迟 ≤1 秒级
- **管理 API**：`komari_management` Prompt API 的 GET / PUT / PATCH 均读写强类型表（`If-Match` revision 强 ETag），响应中 `config_source` 形如 `postgresql:komari_prompt_komari_chat:komari_chat`，`file_path` 仅保留为 `null` 兼容字段
- **旧表与旧 YAML**：`komari_prompt_configs` JSONB 表与 `config/prompts/*.yaml` 均不再作为运行时来源；如需保留旧值，显式执行 `scripts/migrate_prompt_config_to_pg.py` 导入旧 JSONB 表（旧表数据由 `migrate_legacy_config_to_typed_tables.py` 统一搬运）。`komari_decision` 的 `komari_memory_scenes.yaml` 已迁移到 PostgreSQL `komari_decision_scenes` 表，运行时默认使用 `PostgresSceneTemplateLoader`；YAML loader 仅供迁移脚本和测试使用。

### 1.2 Komari Management v2

- 管理路由统一使用 `/api/v2/<plugin>` 前缀，旧版路由、弃用别名和退役 stub 均不注册。
- 鉴权只接受 `api_credentials` 具名凭据；启动时对遗留 `komari_plugin_configs` 中仍使用旧单 Token / `llm_logs:read` 权限名的凭据记录警告（不物理删除）。
- Agent Run 日志权限为 `agent_run_logs:read`；搜索提供者描述端点使用 `search:read`。
- Swagger 与 OpenAPI 入口分别为 `/api/docs` 和 `/api/openapi.json`。
- `GET /api/v2/komari-search/provider-descriptors` 从 `komari_search` 配置 Schema 动态生成通用字段与 Tavily/EXA 专用字段描述。

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
- `continuation: LLMProviderContinuationSchema | None` — Responses 协议的不透明续接项（见下）

`UnifiedUsageSchema` 字段（全部 `int | None`，`None` 表示后端未报告）：
- `input_tokens`、`cached_input_tokens`、`cache_miss_input_tokens`
- `output_tokens`、`reasoning_output_tokens`、`total_tokens`

用量提取（`openai_compatible_api.py`）：
- 支持对象属性、普通字典、Pydantic `model_extra` 三种形式
- DeepSeek `prompt_cache_hit_tokens` 优先于 OpenAI `prompt_tokens_details.cached_tokens`
- `completion_tokens_details.reasoning_tokens` 映射到 `reasoning_output_tokens`
- 缺失或异常字段不阻断解析，对应位置保留 `None`

双请求 API 与流式（issue #23）：
- 四个 `generate_*` 公共入口与底层抽象接口均接受显式关键字参数 `request_api: RequestApi | None` 与 `stream_enabled: bool | None`；`RequestApi = Literal["chat_completions", "responses"]` 定义在 `komari_bot/llm/llm_protocol.py`（避免业务插件 import 网关触发 require 副作用）
- 调用方传 `None` 时按网关默认槽位配置解析；业务插件必须按自己的槽位显式透传（见下「槽位配置」），禁止依赖默认解析
- 流式只在网关内部聚合，业务侧永远拿到完整 `LLMCompletionResultSchema`；Chat 流式强制 `stream_options.include_usage=true` 并按工具 index 聚合增量 tool_calls；断流/取消丢弃部分结果并明确失败
- Responses 协议翻译：安全边界合并入 `instructions`；tools 扁平化（strict 默认 false）；`store=false` 无状态；终态归一化（completed/incomplete/failed/refusal）；无映射参数（frequency_penalty、不兼容 extra_params 键）静默忽略
- 明确失败、禁止静默降级或能力自动探测；连接指纹保持 token/base_url/timeout 三元组不变

续接项（continuation）传递机制：
- `LLMProviderContinuationSchema`（`api="responses"` + 序列化 `output_items`，含加密推理项）只在当前业务任务的多轮工具循环内存活：不写入业务数据库、不跨 QQ 消息复用
- assistant 消息统一经 `build_assistant_message(completion)` 构造；continuation 以内部元数据键 `_llm_continuation`（`CONTINUATION_METADATA_KEY`）附着在消息上
- Chat 请求构建器剥离全部 `_` 前缀内部键（永不发往 Chat 端点）；Responses 构建器校验 `api=="responses"` 后原样展开 output items
- 无工具但需重试的 assistant 输出同样经统一构造函数附着 continuation

槽位配置（issue #23 新增，全部默认 `chat_completions` + 非流式，零行为变化）：
- `llm_provider`：默认槽位 `request_api` / `stream_enabled`、`vision_request_api` / `vision_stream_enabled`
- `komari_memory`：`llm_request_api_chat` / `llm_stream_enabled_chat`、`llm_request_api_summary` / `llm_stream_enabled_summary`
- `group_history_summary`：`summary_planning_request_api` / `summary_planning_stream_enabled`、`summary_request_api` / `summary_stream_enabled`
- 任务内配置冻结：同一业务任务（如 komari_chat 工具循环）在任务开始时读取一次协议/流式配置，任务执行期间的配置变更不影响进行中的任务；运维按槽位逐位灰度

关键规则：
- `llm_provider` 最底层通过 `apply_llm_security_boundary()` 强制追加不可覆盖的安全 system 边界；知识、网页、群历史、引用、画像、视觉描述和工具结果必须使用 `UntrustedContext` 或统一不可信标签传入，禁止拼进 system prompt
- 不可信上下文必须保留来源类型、来源 ID、信任级别并限制正文长度；工具调用必须使用白名单、对象参数 schema、轮数与总调用预算
- `max_tokens` 必须为 **`int`**（不能是 `float`），默认 8192
- 知识库注入：`enable_knowledge=True` 时自动检索并注入到 system prompt
- `request_trace_id` / `request_phase` 仅用于限流和普通运行日志；provider 不持久化 Agent Run，也不依赖日志插件

### 2.1 Agent Run 日志 (`agent_run_logger`)

- 配置资源只有 `log_enabled`（默认 `true`）与 `retention_days`（默认 `1`，范围 `1..90`）；不读取或继承 `llm_provider.llm_log_*` 旧字段。
- `AgentRunCollector` 由 `komari_chat`、`komari_memory`、`group_history_summary` 和 `komari_debug` 显式下传；一个业务任务无论包含多少 LLM 轮次和工具调用，都只结束和落盘一次。
- 日志类别固定为 `chat_reply`、`scheduled_summary`、`group_history_summary`；debug 复用对应类别并标记 `origin=debug`。日志关闭时普通任务不采集，debug 仍保留不落盘的内存诊断。
- JSONL v3 写入 `logs/agent_run_logger/YYYY-MM-DD.jsonl`，目录固定 `0700`、文件固定 `0600`；保存完整输入、输出、prompt/messages、reasoning、工具参数/结果及异常，只移除显式凭据并把图片 URL、base64 与二进制替换为摘要。
- 每次 LLM 调用（`LLMCallTrace`）额外记录最终生效的 `request_api` / `stream_enabled`（取自请求 kwargs，缺失保持 `null` 不伪造）；响应载荷完整保留 `continuation`（含加密推理项），与正文走同一脱敏管线。
- PostgreSQL `UNLOGGED` 表 `komari_agent_run_log_index` 只保存 run/trace、分类、时间、文件字节定位、模型/方法集合、计数与 usage；禁止加入任何正文、预览、prompt、reasoning、工具正文或错误正文；协议标记只存在于 JSONL，不为它扩展索引结构。
- 写入顺序是跨进程文件锁内追加 JSONL，再 upsert PG。PG 故障不撤销 JSONL；启动及每 5 分钟使用 advisory lock 对账，管理查询在 PG 不可用时降级扫描 JSONL。
- 每日本地时间 04:00 清理，保留当前日志日及此前 `retention_days - 1` 日；日志关闭不停止清理。启动时删除废弃 SQLite 索引文件；旧 `llm_provider.llm_log_*` 配置字段不兼容、不迁移（agent_run_logger 不再执行 legacy 配置键清理）。
- 管理接口为 `/api/v2/agent-run-logs/runs` 与 `/runs/{run_id}`，使用 `agent_run_logs:read`。
- debug 报告必须继续走独立脱敏投影，绝不能直接复用完整 JSONL 正文。

### 2.2 Embedding / Rerank 远程协议

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

- **持久化**：PostgreSQL 表 `komari_user_bans`（SQLModel ORM，见 `orm_models.py`），以 `(user_id, ban_scope)` 为主键；记录可选理由与到期时间，空到期时间表示永久；三张表（含 `komari_user_ban_cache_state`、通知 outbox）均由 Alembic 基线 0001 建表，运行时无 DDL
- **运行时缓存**：启动加载有效记录快照；之后每 5 秒仅检查单行 revision，变化时才以 `REPEATABLE READ` 重载全表；每次命中仍即时判断到期时间，存储不可用时故障关闭
- **初始化并发**：Repository 单飞确认 ORM 存储可连接（表结构已由迁移管理），Service 首份快照加载必须单飞，禁止并发首次调用重复加载
- **自然解封**：APScheduler 每 30 秒原子删除到期记录并按用户合并发送一次普通文本私信；发送失败不回滚且不重试
- **command 拦截**：全局 `run_preprocessor` 检查除 `komari_chat` 外的用户 matcher，封禁时静默清空 `remain_handlers`，保留 matcher 原有 `block`
- **chat 拦截**：聊天消息仍参与判定和记忆；只有实际准备回复时通过 `reply_allowed=False` 压制生成及全部回复副作用
- **管理入口**：SUPERUSER 命令支持永久或 `m/h/d/w` 临时封禁和理由；统一管理 API 通过 `/api/v2/komari-user-bans` 提供查询、封禁与解封
- **SUPERUSER**：管理命令仅限 SUPERUSER，且 SUPERUSER 运行时始终绕过封禁；管理操作和生命周期变化会尝试发送一次私信

### 3.2 用户数据 (`user_data`)

- **生命周期**：仅通过 NoneBot Driver 的 `on_startup` / `on_shutdown` 钩子初始化与关闭，不使用框架不会识别的模块魔术变量
- **动态禁用**：每次数据库入口（包括已有缓存）都实时检查 `plugin_enable`；禁用时抛出 `UserDataDisabledError`，禁止通过懒加载绕过开关
- **原子初始化**：表结构由 Alembic 基线 0001 统一管理（启动期与懒路径均无 DDL）；连接与引擎生命周期归 nonebot-plugin-orm 托管（`get_session`），Repository 单飞确认存储可连接，失败保持未初始化状态
- **并发清理**：关闭流程与懒初始化共用初始化锁，先清空全局引用再关闭，避免继续分发正在关闭的实例

### 3.3 角色名绑定 (`character_binding`)

- **正式真源**：迁移 0018 的 `komari_character_binding_groups` / `komari_character_binding_members`，按 `(app_id, group_openid)` 隔离群关联、成员身份与本群角色名；名字使用 NFKC/casefold 唯一键，双向身份约束由 PG 事务保证。
- **消费边界**：QQ 使用应用/群/成员 OpenID 上下文；OneBot 只通过 `(group_id, user_id)` 做最小群作用域桥接，禁止裸 `user_id` 查询当前角色名。缺失或歧义不猜测身份。
- **快照与就绪**：同步名称读取使用进程内快照；写库成功后发布新快照。修复提交后先在锁内失效受影响缓存，再尽力刷新；刷新失败不能把已提交清除改成失败或保留旧身份。部署保持单 worker。
- **用户入口**：普通用户仅使用官 Bot 群 @ `/bind` 向导；OneBot 原 `bind/bind_set/bind_del/bind_list` matcher 已退役。首次未知群只在有效策略及可信取证配置齐备时获得一次原生引用挑战；OneBot listener 经真实引用和 `get_msg` 取证，保持静默。两平台消息 ID 不混用，10 分钟绝对会话期限不续期，重启失效。
- **确认边界**：草稿不能当正式绑定；取证后、提交前和发送前重新核查群准入及身份。不可用或受限时失败关闭，不将数据库失败解释为无绑定。
- **改名/解绑**：`/bind rename`、`/bind unbind` 经公开键盘会话码和二次确认；普通解绑只清本群角色名，保留已确认身份关系。改名、解绑和取消草稿不改变已有对局的入席资格或冻结名；无当前角色名不能新开局或重入。
- **旧值**：`komari_character_bindings` 仅保留旧全局候选；`get_legacy_character_name()` 只用于本人主动选择迁移，不自动 fallback 或批量创建新关系。旧 JSON 导入脚本不等于正式群绑定迁移。
- **运营修复**：`/api/v2/character-bindings/repair/{diagnose,preview,confirm}` 提供受权诊断、预览和清除；修复需 read/manage 权限、理由、请求 ID 和 10 分钟单次令牌。waiting/active 拒绝修复，清后未绑定是正确终态，用户之后独立 `/bind`；不自动改指身份，不改历史胜场。既有 `.debug bind set|del|list` 仅管理已确认群内角色名，不是身份修复入口。

### 3.4 俄罗斯轮盘 (`komari_roulette`)

- **启动与配置**：`QQ_BOTS` 非空时注册官 Bot 适配器；可信官 Bot 数字 QQ 按 `QQ_OFFICIAL_BOT_QQ_BY_APP` 启动配置。迁移 0019/0020/0021 建立游戏、收据/履约和强类型配置；`plugin_enable` 默认 false，动态生效。按共享 ORM → 合法配置 → binding/admission → storage/recovery 顺序启动，首次成功恢复前不放行业务。
- **领域与事务**：2～6 人、6 发弹仓、15 分钟绝对行动期限；稳定 `join_seq` 不重排或复用。开始时冻结名字、编号顺序与道具权重。命令、收据、游戏状态和胜场通过 PG 事务/约束/锁/CAS 一致提交，不新增轮盘专用 Redis 依赖。
- **QQ 交互**：仅群 @ `/轮盘 …`，目标用稳定玩家编号，不依赖第二个 mention。按钮只填入命令，用户手动发送才执行。终局为完整普通单段，无引用/粗体/按钮；提及使用胜者已知 OpenID，不从昵称猜测。
- **一次履约**：领域提交后冻结文案与投影；配置变化不重渲染旧收据。发送开始后异常、取消或缺失合法平台 ID 保持 UNKNOWN / `PENDING_CONFIRMATION`，不得回滚领域事实、重抽、重发或猜测确认。后台无合法入站消息 ID 不创建群消息 outbox。
- **恢复与清理**：关闭开关不暂停绝对期限；受限群不推进，恢复后只处理旧当前玩家一次，下一人取得完整 15 分钟。每 60 秒小批扫描，每日调度器时区 04:00 清理，PG UTC 比较保留边界；收据/履约 7 天，cancelled/expired/failed 30 天，completed/结果玩家/胜场长期保留，waiting/active 不清理。关闭先撤权和调度，再有界收束自有工作，不 dispose 共享 ORM。
- **控制面**：`/api/v2/komari-roulette/status` 与 `/leaderboards/{inspect,rebuild}`；read/manage 分权。仅按 completed 真源校验/重建，不提供任意加分、重置、恢复发送或强制终局。`latest_scan/latest_cleanup` 是结果计数，不是时间戳。
- **交付边界**：根目录 `ROULETTE-ROLLOUT.md` 记录升级、旧名主动迁移、运维与最终 QQ 样本；自动化及旧原型不代替当前部署版本的真实 QQ 展示/通知验收。

### 4. 四层记忆系统 (`komari_memory`)

| 层 | 存储 | 表 | 说明 |
|----|------|-----|------|
| 1. 对话摘要 | PG | `komari_memory_conversations` | 向量搜索 + 遗忘模糊化 |
| 2. 用户画像 | PG | `komari_memory_user_profile` | JSONB traits，LLM 压缩 |
| 3. 互动历史 | PG | `komari_memory_interaction_history` | JSONB records，增量更新 |
| 4. 实体知识 | PG | 通过 EntityRepository | 关键词 + 向量检索 |

关键类：`PluginManager` → `MemoryService` → `ConversationRepository` / `EntityRepository` + `ForgettingService`
注意：四层表（含独立 embedding 表、记忆 job 表）与 HNSW 索引的 DDL 由 Alembic 基线 0001 统一管理，运行时不建表；`EntityRepository` 操作的是记忆层自身的画像/互动表，与 `komari_knowledge` 的知识表相互独立。连接统一经 `komari_bot/db/orm_connection.py` 共享引擎 raw 适配层（asyncpg 兼容，`$n` 占位符 SQL 原样保留）。

### 4.1 知识与帮助关键词索引

- `komari_knowledge` 与 `komari_help` 使用不可变内存快照，重建完成后一次性替换，查询不得观察到半成品索引。
- PostgreSQL 语句级触发器在业务写入事务内递增 `komari_search_index_versions`；其他 worker 最多 1 秒轮询到变化并重建。
- 初始化与索引重建必须走单飞锁；重建失败继续保留旧快照，关闭时等待在途重建结束后清空。

### 5. 判定引擎 (`komari_decision`)

核心服务：
- `SceneRuntimeService` — 场景生命周期管理
- `SceneAdminService` — 场景运维（CRUD）
- `SocialTimingService` — 社交时机判定（主动回复冷却、频控）
- `MessageFilter` — 消息过滤

聊天候选重排与群总结场景归类收敛到深场景归类 module（`services/scene_classification.py`）内部：聊天专用 operation `rank_chat_message` 及其候选/结果/异常类型不对外导出，不加入插件顶层、services `__all__` 与共享包 `komari_bot.decision`（KOMARIBOT-27 已物理删除旧宽重排服务与跨插件宽契约）。

存储：场景四表（`komari_decision_scenes` / `komari_memory_scene_set` / `komari_memory_scene_item` / `komari_memory_scene_runtime`）为 SQLModel ORM 模型（`orm_models.py`），经 nonebot-plugin-orm `get_session` 访问，DDL 由 Alembic 基线 0001 管理；embedding 生成经 `embedding_provider` 远程接口。

运行时契约：
- 运行时状态模型 `DecisionRuntimeState`（共享包 `komari_bot.decision`，经顶层 `get_decision_engine()` 发放的引擎内部消费）明确返回 `ready` / `disabled` / `failed`，禁止再用 `None` 混合表达状态。
- `disabled` 或 `failed` 时，显式 @、文本 @ 别名和回复机器人仍由 `komari_chat` 直通；普通非 @ 消息仅保留必要缓冲，不执行 embedding/rerank，也不主动回复。
- `plugin_enable=true` 但初始化异常或 scene snapshot 缺失必须报告 `failed`；只有 snapshot 可用时才记录 `ready`。

### 6. 聊天处理器拆解 (`komari_chat`)

`message_handler.py` 的 `_attempt_reply()` 已拆分为三个边界：

1. **`_read_buffers()`** — 读取 Redis 现有的 recent/global interaction buffer，可选 `store_current`
2. **`_generate_reply_core()`** — 纯读取/生成核心：查询重写、记忆/画像/好感度读取、prompt 构建、LLM 回复生成；不执行任何副作用；接受可选 `AgentRunCollector`
3. **送达后副作用唯一路径 = outbox** — `commit_delivered_reply()` 只做 `mark_delivered()` 登记 → `claim_operation()` 领取（失败/None 由后台 worker 轮询）→ 四步幂等提交（proactive confirm / 好感度 adjust / AI 历史存储 / 互动历史写入）→ `complete()`；`_attempt_reply()` 自身不提交任何聊天副作用

表情反应与失败通知契约：
- 表情反应在 `_attempt_reply()` 内、调用 `_generate_reply_core()` 之前以 `asyncio.create_task` fire-and-forget 派发（任务引用挂 `self._reaction_tasks` 防 GC），提示“正在生成”；`commit_delivered_reply()` 不再触发表情，送达后不撤下。
- `_attempt_reply()` 返回 `(PendingReply | None, stored, ReplyFailureInfo | None)`；生成失败（异常、空回复、delta 缺失、预占租约丢失）转为 `ReplyFailureInfo` 而非上抛（CancelledError 除外）；频控冷却/超限/重复等正常控制流返回 `(None, False, None)` 不通知。
- 失败分流边界是“是否贴过表情”：`report_reply_failure()` 在 `reaction_sent=True` 时以 reply 段引用原消息补发群内固定错误文本，并向 SUPERUSERS 私聊极简诊断卡（trace/群号/触发原因/阶段/异常类型+一行摘要）；Redis key `komari_chat:error_notify:{group_id}:{error_type}` SET NX EX 300 去重，Redis 异常降级照常通知；`error_notify_enabled=false` 仅静默通知，不影响群内错误文本；实现位于 `services/error_notify.py`，善后方法自身绝不抛出。

主动回复频控契约：
- 非强制回复在生成前调用 Redis Lua 原子预占，同时检查冷却、最近一小时已确认名额与生成中名额；预占 ID 使用平台消息 ID，重复投递不会重复生成。
- 生成失败、空回复或发送失败调用 `release_proactive_reply()` 幂等释放；发送成功后先调用 `confirm_proactive_reply()`，再提交其他聊天副作用。
- 预占带 `proactive_reservation_ttl_seconds`；进程崩溃后的孤儿预占按 TTL 淘汰。已确认名额进入一小时滑动窗口，释放接口不得撤销已确认名额。

`process_message(..., reply_allowed=False)` 用于 chat 封禁：保留原消息的判定和缓冲写入，但在 `_attempt_reply()` 前返回，并记录 `blocked_by_user_ban`。

公开 debug 入口：
- `komari_chat.generate_debug_reply()` — 以命令发起者身份、当前群上下文执行纯读取/生成，完全跳过决策引擎、表情反应、Redis push、好感度 adjust、互动历史、冷却/频控；使用 `debug-reply-*` trace ID；返回 `DebugReplyResult`（含 collector）
- 底层依赖未初始化时抛出 `RuntimeError`（可展示的错误信息）

视觉服务与图片理解（`vision_service.py` / `image_understanding.py`，TSK-194/ADR-0010）：
- 已移除绕过网关的独立 `AsyncOpenAI` 调用，改用 `llm_provider.generate_messages_completion()`；视觉调用作为 `read_image` 工具的子调用，通过 collector 记录同一 trace，最终请求携带任务起点快照冻结的 `thinking_mode` / `reasoning_effort`，任务内不重读
- 图片理解模式（`image_understanding_mode`：`native` / `delegated`）与 8 项下载预算（`vision_image_download_*`）归 `komari_chat` 配置（迁移 0015 从 `komari_memory` 一次性迁入，不保留 alias / 双读 / 运行时 fallback）；模式与预算在任务起点从 chat 配置读取一次并冻结，任务执行期间配置变更只影响下一个任务
- `native`：图片经安全下载与校验后作为多模态输入直接交给聊天主模型（chat 槽位），不声明 / 不调用 `read_image` 工具；聊天模型对带图请求报错或拒图时本任务明确失败，不自动降级、不切 delegated、不调用视觉服务
- `delegated`：只向主回复 Agent 暴露稳定图片索引与 `read_image` 工具，主工具循环恒使用聊天模型与 chat 槽位；视觉子调用（`vision_service`）才使用独立视觉模型与 vision 槽位（含 `vision_thinking_mode` / `vision_reasoning_effort`）
- TSK-195：delegated 任务起点零预下载，`ImageReadingSession`（任务级图片会话）持有稳定引用（引用消息在前、当前消息在后）、按索引成功/失败缓存与并发单飞（取消任意 waiter 不取消共享下载/视觉任务，任务完成结果仍缓存）、字节账本/并发信号量/累计总时限（跨多轮不重建；总时限为下载活动区间的并集耗时，并发重叠只计一次、空闲不消耗不重置，`download()` 与 `download_many()` 共用同一账本）、全失败安全摘要（`all_images_unavailable` + 归一化错误类型，TSK-196 消费）；只有首次 `read_image(image_index)` 才经安全下载器懒下载；原始 URL 只存在于会话内部私有映射→安全下载器边界（公开引用只含稳定 index/origin/source_label），不进主模型消息、工具结果、普通日志与 Agent Run/debug 投影；任务结束 `close()` 阻止新读取、取消并等待在途读取后再释放下载连接；视觉描述经既有 `UntrustedContext`（`source_type=vision`）回流；主 prompt 只含稳定索引/来源/范围（无 URL/base64）
- 域名必须由 aiohttp 建连阶段的受控 resolver 解析并校验，禁止恢复“预解析后再由客户端重新解析”的 DNS 重绑定窗口；每一跳重定向都执行同样校验
- 图片 MIME 必须来自 Pillow 对真实文件的识别与解码结果，禁止信任响应 `Content-Type` 或 URL 后缀；仅接受 JPEG、PNG、GIF、WebP，并执行累计像素限制
- TSK-196：图片理解失败统一汇总与安全终止。delegated 部分失败（非全部可用索引均已尝试且失败）保留结构化 `read_image` 失败工具结果并允许 `final_response`，成功任务把聚合摘要附加到 `ReplyResult.image_failure_summary`；全部可用索引均已尝试且失败（含同轮 `read_image`+`final_response`、以及最后允许轮次仅 `read_image` 无 `final_response`）在每次 `read_image` 的 ToolExecutionTrace 写入后立即以 `ImageUnderstandingFailureError`（只携带 `ImageFailureSummary`，`str()` 仅含模式）终止（绝不落入 MaxRounds RuntimeError），最后一次失败工具 Trace 保留；同轮 `final_response` 排在 `read_image` 之前时 `final_response` 成功且后续 `read_image` 不执行（不预判失败）。native 全部下载失败在主 LLM 前终止；主 provider 多模态调用失败在 llm_service 的 `_call_llm_completion` except seam 收敛为窄 marker `NativeMultimodalRequestError`（离开 except 后 raise，`cause/context=None`，不携带原异常 cause/正文，Agent Run LLM trace 只记录归一化异常类型），message_handler 只捕获该 marker 并包装为 `mode=native` 摘要（不切 delegated），其他异常（MaxRounds/工具预算/协议校验/内部）即便 native 有图也原样传播；provider 整体失败时 `failed_count` 恒为 `total_images`（部分下载失败 + provider 失败时 error_types/stages 同时含 download+vision）；部分下载失败成功任务同样附带摘要；无图片的普通 LLM 错误原样传播。`_generate_reply_core` 对所有预期图片失败（native 全下载失败 / native provider 失败 / delegated 全部不可用）统一汇聚到单点安全抛出：先保存安全 `ImageFailureSummary`（delegated 原异常与 native marker 都在 except 内只保存摘要、不 raise），离开 except/finally 后抛新 `ImageUnderstandingFailureError`（`cause/context=None`，异常链彻底断开，不残留原 provider 异常对象）；抛前显式清空该 frame 的图片敏感 traceback locals（函数参数 `image_urls`/`reply_context`、`reply_sources`/`current_sources`/`combined_sources`、`aligned_images`/`reply_image_urls`/`base64_image_urls`、含 image_url 部件的 `prompt_messages`、已 close 后置 None 的 `image_session`、可能持 raw input_data 的 `collector`），只重绑本地名字不触碰调用者对象，最终异常 traceback 的 komari_chat 帧递归投影不含原 URL/base64/视觉描述（不依赖 Sentry sanitizer 后处理）；`generate_debug_reply` 图片失败重抛前在 collector 安全 finalize 与普通安全日志后同样清空其帧的 `image_urls`/`reply_context`/`refetched_context`/`collector` 局部再 re-raise，普通非图片 debug 错误语义不变。成功路径 `process_message` 经共享 `GroupTaskFailureNotifier` 最多提交一次 SUPERUSER 图片汇总卡（`group_text=None` 无群消息，`image_failure_reason_code` 稳定去重键，`error_notify_enabled` 关闭只静默私聊），失败路径 `report_reply_failure` 对图片失败只发一张汇总卡（`reaction_sent` → 群内固定错误文本），debug 干跑绝不通知（只把安全结果保留在 collector/output）；`ImageFailureSummary` 只在 message_handler 通知边界经本地窄 mapper 投影为 onebot `ImageFailureDiagnostic`（构造时运行时校验并确定性去重排序，mode/stage/error type 白名单、failed_count 正整数），summary→diagnostic 映射失败 fail-closed：静默 SUPERUSER 图片卡且不降级为可能泄漏的 generic 私聊摘要，但仍经共享 notifier 投递 `group_text`（reaction_sent 分流），不吞掉既有群内固定道歉；图片卡只渲染群/trace/模式/失败阶段/失败数量/错误类型（不含任务），无 URL/base64/正文/视觉描述

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

数据库连接唯一权威是 nonebot-plugin-orm 的 `SQLALCHEMY_DATABASE_URL`（旧 `PG_*` 环境变量已删除）；Schema 唯一权威是 `migrations/` Alembic 版本链，运行时禁止任何 `CREATE TABLE` / `ALTER` DDL。

```python
# 常规 ORM 访问（nonebot-plugin-orm AsyncSession，业务关系表走这里）
from nonebot_plugin_orm import get_session
async with get_session(expire_on_commit=False) as session:
    result = await session.execute(select(Model).where(...))

# 特殊 DDL 表（向量/HNSW/UNLOGGED/advisory lock）保留 raw SQL：
# 共享引擎的 asyncpg 兼容适配层，SQL 的 $n 占位符逐字保留
from komari_bot.db.orm_connection import get_shared_orm_connection_pool
pg_pool = get_shared_orm_connection_pool()
async with pg_pool.acquire() as conn:
    rows = await conn.fetch("SELECT * FROM ... WHERE col = $1", param)
    async with conn.transaction():
        ...

# pgvector 维度校验辅助
from komari_bot.db.pgvector_schema import ensure_vector_column_dimension

# Redis（使用 redis.asyncio，禁止用 aioredis；配置见 config/redis_config.py）
import redis.asyncio as aioredis
redis_client = aioredis.Redis(host=..., port=..., db=..., password=...)
```

Alembic 迁移工作流（Schema 变更唯一入口）：

```bash
# 应用迁移 / 校验迁移链与模型零漂移
poetry run python -m komari_bot.db.orm_bootstrap upgrade head
poetry run python -m komari_bot.db.orm_bootstrap check

# 生成新 revision（改完 SQLModel 后必须 autogenerate 并审阅生成的迁移）
poetry run python -m komari_bot.db.orm_bootstrap revision --autogenerate -m "描述"
```

规则：

- 新增/修改表结构一律走迁移：改 `config_schema.py` / `prompt_schema.py` / `orm_models.py` 等 SQLModel 元数据后 autogenerate，`check` 必须零 diff 通过；CI（`.github/workflows/migration-check.yml`）执行 `upgrade head` + `check` 守门。
- 含向量、触发器、`UNLOGGED`、advisory lock 等特殊 DDL 的表（记忆四层、知识库、帮助、`komari_agent_run_log_index`、`komari_search_index_versions`）以 `op.execute` 手写 revision，不依赖 autogenerate。
- `migrations/env.py` 合并 SQLModel.metadata，`include_object` 统一禁止 autogenerate 为 metadata 之外的表生成 drop；删除表必须手写迁移，绝不依赖 autogenerate。
- 容器 `docker/prestart.sh` 在启动 Gunicorn 前自动 `upgrade head`，失败 fail fast；本地升级新代码前先跑一次 `upgrade head`。

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

# 迁移验证（改过任何 SQLModel 后必须执行）
poetry run python -m komari_bot.db.orm_bootstrap upgrade head
poetry run python -m komari_bot.db.orm_bootstrap check   # 必须零 diff

# 真实库集成测试（KOMARI_TEST_POSTGRES_URL 门控；两变量必须同库，否则守卫 skip）
# 共享门控库必须先 upgrade head 且始终保持 head；迁移链驱动验收
# （tests/db 的 0004/0010/0011 迁移测试与 legacy 配置脚本集成测试）在
# 门控库派生的一次性隔离库（库名 = 门控库名 + 文件专属后缀）内重建迁移链，
# 用例结束即 DROP，门控用户需要 CREATEDB 权限
SQLALCHEMY_DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/komari_bot_test \
KOMARI_TEST_POSTGRES_URL=postgresql+asyncpg://user:pass@host:5432/komari_bot_test \
  poetry run pytest tests/ -v
```

## 关键注意事项

1. **NoneBot2 依赖注入**：`CommandArg()` 返回 `Message` 类型，类型注解错误会导致处理器静默跳过
2. **`FinishedException`**：`nonebot.finish()` 通过抛出该异常终止，不要被 `except Exception` 捕获
3. **权限必须运行时检查**：不要在 matcher 创建时用 `rule=` 做静态权限检查
4. **资源清理**：`close()` 方法必须清理所有资源引用（连接池、模型、文件句柄）
5. **提前返回**：条件检查不通过时添加 `return`，避免继续执行
6. **Python 3.13 特有**：本项目不兼容 Python 3.12 及以下
7. **Sentry 过滤**：NoneBot 控制流异常（StopPropagation 等）已在 `sentry_support.py` 中过滤；脱敏采用黑名单式：诊断数据（异常正文、breadcrumb、Sentry Logs 正文、事务/span、堆栈局部变量、请求数据）默认全量保留，仅通过三层机制（字段名黑名单、值模式正则、已配置秘密精确替换）隐藏凭据类字段。`send_default_pii` 仅控制 user 上下文（用户标识）是否上报
8. **debug 插件权限**：`komari_debug` 所有子命令在处理器第一行调用 `await SUPERUSER(bot, event)`，绝对不放进 matcher 的 `permission=` 或 `rule=`
9. **debug 无副作用**：`.debug reply` 走 `generate_debug_reply()`，完全不触发 Redis push、好感度 adjust、互动历史写入或冷却/频控
10. **诊断结构与投递**：完整报告绝不包含完整 prompt、reasoning content、base64、历史或画像正文，且只私聊已鉴权 SUPERUSER；群内默认仅返回 request ID/状态，`--public` 仍必须隐藏输入、输出、用户标识、异常正文与工具参数
11. **用户封禁边界**：`chat` 只压制 `komari_chat` 的实际回复；其他用户 matcher 统一属于 `command`，封禁时必须静默且保留原 matcher 的传播阻断语义
12. **内容预算**：用户/管理入口可写文本必须复用 `komari_bot.llm.content_budget`；同时检查字符、UTF-8 字节、估算 token 与关键词组合，不得在各插件复制限额或静默截断
13. **fetch_page 脱敏**：`komari_debug` 诊断报告的 `_build_safe_tool_arguments` 对 `fetch_page` 只记录 `url_count`，绝不记录 URL 内容；`komari_search` 抓取失败日志只记录 URL 数量与 URL 集合 SHA-256 指纹

## Agent skills

### Issue tracker

Track issues and PRDs in the self-hosted Huly project **TSK** through the Huly MCP proxy tools, not through `gh`. Publish `/to-tickets` implementation tickets as sub-issues of their parent spec. Represent blocking edges with Huly's native issue relations and retain a textual `Blocked by` line as a quick-reading fallback. See `docs/agents/issue-tracker.md`.

### Triage labels

使用默认五个规范标签：needs-triage / needs-info / ready-for-agent / ready-for-human / wontfix。See `docs/agents/triage-labels.md`.

### Domain docs

Single-context 布局：根目录 `CONTEXT.md` + `docs/adr/`。See `docs/agents/domain.md`.

## 相关文档

| 文档 | 位置 | 用途 |
|------|------|------|
| 任务交接记录 | `docs/handoff/` | 历史任务详情、跨会话交接文档 |
| 架构决策记录 | `docs/adr/`（0001–0012） | 搜索抽象、管理 API v2、绑定迁移、ORM/强类型配置迁移等 |
| 组件文档 | `docs/*.md` | 各插件的详细设计文档 |
| 迁移说明 | `migrations/README.md` | Alembic 工作流与存量配置搬运 |
| Agent skill 配置 | `docs/agents/*.md` | issue tracker / triage 标签 / 领域文档约定 |

---

*本文件由 AI 生成于 2026-04-26，最后更新于 2026-09-11。发现不一致请以实际代码为准并更新本文档。*
