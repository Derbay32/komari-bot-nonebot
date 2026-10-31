# komari-bot AI 上下文文档

> **AI 智能体在处理本项目任何任务前必须先阅读此文件。**

## 项目概述

komari-bot 是基于 [NoneBot2](https://github.com/nonebot/nonebot2) 构建的 QQ 机器人，核心角色是《败犬女主太多了》中的 **小鞠知花**。

**核心能力**：AI 聊天（LLM 驱动）、Hindsight 共享长期记忆、RAG 知识库、智能帮助、群聊总结、角色绑定、好感度系统、主动回复判定。

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
| Agent 执行 | DeepSeek Harness（DSH）+ Node.js | 聊天与总结独立预设，官方模型协议与内置重试 |
| 判定 | 官方 TypeSafe Python SDK | Jev / System One；独立于 DSH 生成任务 |
| Embedding | OpenAI 兼容 API（远程） | 默认 `BAAI/bge-small-zh-v1.5` |
| 部署 | Docker + Docker Compose | 同容器运行 Python Bot 与长期存活的 Node 进程 |
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
│       ├── 0001_fresh_marker.py         #   全新库身份 marker
│       ├── 0002_typed_schema.py          #   强类型配置表与 Prompt 表
│       ├── 0003_komari_chat_config.py    #   komari_chat 配置表（承接 memory 迁出字段）
│       ├── ...                         #   其余历史迁移
│       ├── 0022_jev_request_config.py  #   Jev 请求配置
│       ├── 0023_jev_decision_storage.py #  判定调度、去重、日志与 Prompt
│       ├── 0024_jev_recent_settlement.py # 判定近期上下文结算
│       ├── 0025_scene_storage_retirement.py # 不可逆删除旧场景存储
│       ├── 0030_hindsight_memory_retirement.py # 旧记忆配置/Prompt 退役（不可逆）
│       ├── 0031_unused_memory_search_limit.py # 删除无调用方的召回数量配置（不可逆）
│       ├── 0032_agent_run_retirement.py     # Agent Run 存储退役
│       ├── 0033_dsh_execution_retirement.py # 网关与旧提示词存储退役
│       ├── 0034_management_dotenv_config.py # 管理配置转为环境配置
│       └── 0035_business_capability_switches.py # 业务开关改名与依赖停用项删除
│
├── komari_bot/                           # ★ 核心代码
│   ├── core/                             # 核心启动与平台基础件（依赖 NoneBot 运行时）
│   │   ├── nonebot_compat.py             #   NoneBot ForwardRef 兼容补丁（显式安装点）
│   │   ├── retry.py                      #   共享异步重试装饰器
│   │   ├── project_paths.py              #   项目路径常量（DATA_DIR 等）
│   │   └── sentry_support.py             #   Sentry 初始化 + 异常过滤
│   ├── db/                               # PostgreSQL 边界共享件（经 nonebot-plugin-orm 托管连接）
│   │   ├── orm_bootstrap.py              #   迁移命令引导（upgrade head / check / revision）
│   │   ├── orm_config.py                 #   SQLALCHEMY_DATABASE_URL 解析
│   │   ├── orm_connection.py             #   共享引擎的 asyncpg 兼容 raw 连接适配
│   │   ├── pgvector_schema.py            #   向量列维度校验等辅助
│   │   ├── vector_storage_schema.py      #   pgvector DDL 离线渲染（运行时不执行 DDL）
│   │   ├── sql_like_utils.py             #   LIKE 模式转义等 SQL 工具
│   │   └── versioned_keyword_index.py    #   版本化关键词索引
│   ├── decision/                         # 判定纯契约（无 NoneBot / SDK 依赖）
│   │   ├── jev.py                        #   请求、结果与诊断数据结构
│   │   ├── engine_protocol.py            #   evaluate / claim_handoff / observe_delivered
│   │   └── dispatch.py                   #   同次入站分发的共享判定载体
│   ├── llm/                              # 内容预算与安全上下文共享件（无 NoneBot 依赖）
│   │   ├── untrusted_context.py          #   不可信上下文包装与渲染
│   │   ├── content_budget.py             #   内容预算校验（字符/字节/估算 token）
│   │   ├── token_counter.py              #   估算 token 计数
│   │   └── diagnostic_sanitizer.py       #   共享诊断脱敏（凭据与媒体载荷）
│   ├── context/                          # 近期上下文 Redis 键与中性存储操作（不依赖插件）
│   │   ├── redis_keys.py                 #   共享近期上下文/履约证据键
│   │   └── redis_store.py                #   RecentContextStore + MessageSchema
│   ├── config/                           # 强类型配置与 Prompt 存储共享件（加载器依赖 NoneBot 运行时）
│   │   ├── typed_config.py               #   强类型配置/Prompt 表基类、注册表、安全加载器
│   │   ├── prompt_storage.py             #   Prompt 强类型表与运行时加载
│   │   └── redis_config.py               #   Redis 共享配置 Schema
│   ├── management/                       # 管理 API 与审计共享件（依赖 FastAPI）
│   │   ├── management_api.py             #   Bearer Token 鉴权 + CORS
│   │   └── management_audit.py           #   管理操作审计
│   ├── onebot/                           # OneBot 消息与规则共享件（依赖 OneBot V11 适配器）
│   │   ├── onebot_messages.py            #   消息工具（plain_text_message 等）
│   │   └── onebot_rules.py               #   group_message_rule() 等
│   └── plugins/                          # NoneBot 插件模块
│       ├── <插件>/config_schema.py       #   动态配置强类型表（SQLModel）
│       ├── <插件>/prompt_schema.py       #   Prompt 强类型表（仅 Jev 判定资源）
│       └── <插件>/orm_models.py          #   业务关系表 ORM 模型（user_ban/character_binding/
│                                          #   user_data/komari_custom/komari_management/komari_decision）
│
├── docs/
│   ├── adr/                              #   架构决策记录
│
├── data/ / scripts/ / tools/ / tests/    # 数据 / 脚本 / 工具 / 测试
├── dsh/                                  # 官方依赖、接入插件与聊天/总结预设
└── logs/                                 # 普通运行时日志；历史 Agent Run 文件不自动清理
```

## 插件架构与依赖关系

### 插件分层

插件的 `require()` 声明就是硬依赖，修改前必须理解依赖链。

跨插件 import 边界（ADR-0006）：`require()` 仅作依赖加载声明，实际引用一律走普通 import；import 只允许指向依赖插件的顶层包暴露面（`__all__`），禁止指向其内部子模块（`services` / `repositories` / `handlers` 等）。唯一豁免：管理插件 import 各插件配置 Schema 以注册管理资源。`komari_context` 是近期上下文与聊天履约证据 Redis 客户端以及共享会话身份/通知策略的唯一所有者，顶层暴露 `KomariContextConfigSchema` / `RecentContextStore` / `MessageSchema` / `get_context_config` / `get_context_store`；`komari_memory` 顶层只暴露 `KomariMemoryConfigSchema` / `HindsightMemoryService` / `HindsightRecallSession` / `HindsightRetainRequest` / `HindsightUnavailableError` / `get_hindsight_memory`；旧 `MemoryService` / `get_memory_service` / `get_plugin_manager` / `get_redis_manager` 已移除；共享 `retry_async` 位于 `komari_bot.core.retry`。komari_chat 经自有 `config_interface`、chat/context 配置与上述顶层暴露面读取配置和共享资源，不再读取 memory 配置，不属于管理插件豁免。详见 `docs/adr/0006-cross-plugin-top-level-imports-decision-contracts.md`。

```
基础服务层（被依赖，不应依赖业务插件）
  config_manager ───────────── 动态配置存储（PostgreSQL 强类型表 + .env 初始化）
  komari_context ───────────── 近期上下文 Redis 客户端唯一所有者
  group_admission ──────────── 统一群准入裁决与逐效果检查
  user_ban ─────────────────── 全局 QQ 用户封禁（chat / command；ORM 表 + revision 缓存）
  embedding_provider ───────── 向量化 + Rerank 服务
  komari_agent ─────────────── DSH 进程、任务桥接与受鉴权管理入口
  komari_search ────────────── 联网搜索与网页抓取服务（Tavily / EXA 双提供者）
  user_data ────────────────── 当前好感度 PostgreSQL 服务（ORM 表）

核心功能层
  komari_memory ────────────── Hindsight 共享长期记忆调用侧
  komari_decision ──────────── Jev 回应资格、保留用途与群总结判定
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
群消息 → DecisionDispatch（冻结原始平台入口事实）
         ├─ 总结候选入口 → 检查开关、准入、权限与能力 → 一次判定
         │   ├─ 总结命中 → 停止传播 → 领取 summary 交接资格 → 执行总结
         │   ├─ 正常未命中 → 聊天入口复用同一结果
         │   └─ 技术失败 → 停止总结与聊天两条路径
         └─ 聊天入口 → 按需判定或复用结果
             ├─ 按保留用途交给既有消息保存流程
             ├─ 检查 chat 封禁与既有回复履约身份
             └─ 领取 chat 交接资格 → 读取记忆 → 生成回复 → 回复履约
                 ├─ DSH 聊天预设执行查询、好感度提案与最终回复提交
                 ├─ Python 校验候选结果
                 └─ 确认送达后执行原有送达后承诺

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
         │         跳过决策引擎、Redis push、好感度 adjust、Hindsight 写入、冷却/频控，
         │         读取真实群上下文与附图/引用消息；完整诊断报告仅私聊 SUPERUSER，
         │         群内默认只发 request ID/状态，`--public` 仅追加二次脱敏摘要
         └─ summary — 调用 group_history_summary.execute_group_summary 共享服务，
                      总结图片与完整诊断报告按顺序私聊 SUPERUSER；群内遵循相同回执/脱敏规则
         └─ notify — on|off 切换 komari_context.error_notify_enabled（回复失败 SUPERUSER 通知）
```

## 核心机制详解

### 1. 配置管理 (`config_manager`)

修改业务开关名称或作用范围前，读取 [ADR-0018](<docs/adr/0018-business-capability-switches.md>)。
业务开关按业务能力命名。
必需依赖不提供独立停用开关。
迁移 `0035` 保留改名项的已有值。
网页能力总开关合并到搜索与抓取开关。
新安装的搜索与抓取均默认关闭。

- **存储源**：业务插件动态配置统一存储在 PostgreSQL 强类型单行表 `komari_<插件>_config`（迁移 0002 建表、0004 为 komari_chat 补充、0026 新增 komari_context、0027 完成 chat/context 字段所有权迁移）；每张表固定主键 `id=1`、CAS 修订号 `revision`（写操作原子自增）、写入时间 `updated_at`（存储层显式赋值）；可扩展字段（列表/字典/嵌套配置）保留 JSONB 列
- **聊天配置**：`komari_chat_config` 保存主动回复频控与回复履约策略。
- 回复履约策略包括租约、批次、重试、新鲜度和墓碑保留。
- `komari_chat_config` 保存回复 Agent 预算（`agent_max_rounds` / `agent_max_tool_calls_per_round` / `agent_max_total_tool_calls`）。
- `komari_chat_config` 保存 8 项 `vision_image_download_*` 安全预算。
- 聊天配置继续拥有知识库开关与上下文预算。
- 迁移 `0027` 将表情反应、聊天检索开关与聊天启用开关迁入聊天配置。
- `0033` 删除聊天与总结的模型连接字段。
- 模型连接、绑定与系统提示词统一归 DSH。
- 知识与帮助的 `query_rewrite_rules` 字符串替换规则继续保留。
- **上下文配置**：`komari_context_config` 保存 `redis_db`、`bot_nickname` 与 `error_notify_enabled`。
- TSK-195 的回复履约不再读取全局互动开关。
- TSK-197 已删除 `global_interaction_enabled` 与 `global_interaction_trigger_size`。
- 聊天配置通过 `komari_chat/services/config_interface.py` 读取。
- 上下文配置通过 `komari_context.get_context_config()` 读取。
- 配置迁移不保留旧别名、双读或故障回退。
- 聊天插件禁止导入 `komari_memory.services.config_interface`。
- **结构真源**：各插件 `config_schema.py` 中的 SQLModel 元数据（`TypedConfigModel` 基类，见 `config/typed_config.py`）；Alembic 迁移环境只按源文件加载 schema（`load_all_typed_config_models()`），不执行插件包 `__init__`、不访问数据库
- **旧 JSONB 表**：迁移 `0013` 已显式删除 `komari_plugin_configs`。运行时只使用强类型配置表。删除表必须使用手写迁移。
- **Prompt 配置**：Jev 使用独立强类型表，聊天与总结使用 DSH（见 1.1 节）。
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

### 1.1 提示词所有权与初始化

修改提示词所有权或切换流程前，读取 ADR-0016。
修改 Jev 初始化前，同时读取 ADR-0011 的保留范围。

DSH 是聊天与群总结系统提示词的唯一生效来源。
管理 API 通过 `/api/v2/komari-agent/prompts/{kind}` 读写字面文本。
Python 不拼接系统提示词，也不保存回退文案。
模型绑定更新与启动初始化必须保留非空管理员文案。

Jev 提示词仍使用 PostgreSQL 资源 `komari_decision`。
通用 `PromptStorage` 保留修订校验与缓存失效语义。
`seed_bootstrap` 只初始化可选的 Jev 资源。
默认资产是 `initial_data/decision_prompt.yaml`。
初始化只插入缺失行或补齐空字段。

缺失、畸形或数据库失败只输出固定安全诊断。
初始化未确认时不能报告成功。
取消继续传播，Alembic 失败仍中止 prestart。
运行时不读取初始资产作为提示词回退。

`0033` 删除旧聊天与总结提示词表。
生产升级前先从旧部署私有导出全部字段与修订。
操作者审阅后，通过新管理 API 显式写入每个预设。
放行业务前必须逐字读回验证。
生产备份、迁移与部署均需单独授权。

### 1.2 Komari Management v2

修改管理启动配置前，读取 ADR-0017。
管理插件自身的全部配置只读取 NoneBot dotenv／进程环境变量。
环境变量统一使用 `KOMARI_MANAGEMENT_` 前缀。
凭据与 Origin 使用 JSON 数组。
全部自身配置重启生效，已加载凭据的定时撤销仍按请求检查。
管理启用字段为 `komari_management_api_enabled`。
对应环境变量为 `KOMARI_MANAGEMENT_API_ENABLED`。
旧环境变量名不再生效。
管理插件不注册自身动态配置资源。
迁移 `0034` 不可逆删除 `komari_management_config`。
升级前必须准备私有环境配置，代码不自动导出凭据或回退数据库。
其他插件动态配置与公告业务存储不受影响。
示例见 `.env.example`。

- 管理路由统一使用 `/api/v2/<plugin>` 前缀，旧版路由、弃用别名和退役 stub 均不注册。
- 鉴权只接受 `api_credentials` 具名凭据。
- 搜索提供者描述端点使用 `search:read`。
- Agent Run 的路由、权限与管理资源已退役。
- Swagger 与 OpenAPI 入口分别为 `/api/docs` 和 `/api/openapi.json`。
- `GET /api/v2/komari-search/provider-descriptors` 从 `komari_search` 配置 Schema 动态生成通用字段与 Tavily/EXA 专用字段描述。

### 2. DSH 执行边界 (`komari_agent`)

修改任务、模型配置、会话或重试边界前，读取 ADR-0016。
Python 与 Node 在同一容器内运行。
`AgentHost` 管理长期存活的 DSH 进程。
每次聊天或总结使用独立的内存会话。
任务终态清理会话，进程崩溃不恢复原任务。
未就绪时拒绝新生成任务，但空配置仍支持管理。

DSH 拥有模型连接、凭据、模型绑定和系统提示词。
首次配置也通过受鉴权的管理 API 完成。
配置与凭据保存在持久化 profile，不增加 dotenv 项。
连接写入在有任务时返回冲突。
提示词与绑定修改只影响后续任务。
管理写入要求强 ETag，未确认生效时明确失败。

Python 提供带身份、引用与来源的结构化上下文。
DSH 将初始数据渲染为一条分区 user 消息。
历史 Bot 发言保留为数据，不重建 assistant 回合。
不可信正文不能提升为系统指令。

聊天完成需要有效的最终回复与好感度提案。
两项提交顺序不限，提前正文只暂存。
总结完成需要有效的最终总结提交。
普通模型文本不能替代业务结果。
任务起点冻结轮次、单轮工具数与总工具数预算。
无效或失败的工具调用仍消费预算。

主请求、辅助请求和业务工具继续逐效果检查群准入。
模型请求仅使用 DSH 内置的暂时性故障重试。
单次请求最多尝试 3 次，外围不叠加重试。
平台发送与送达后承诺继续归 Python 履约流程。

### 2.1 执行日志退役

Agent Run 采集、索引、查询与后台清理已退役。
DSH 不提供替代执行日志或会话持久化。
保留普通错误日志、管理审计与安全通知。
debug 只返回安全结果，不保留逐轮明细或用量聚合。
历史 JSONL 的处置需要单独授权。

### 2.2 Embedding / Rerank 远程协议

- HTTP 客户端必须同时配置连接、读取和总超时；只对网络中断、超时、限流及 5xx 做有限重试。
- Embedding 响应必须与输入数量一致，`index` 唯一且保持输入顺序，向量维度等于配置值，所有元素均为有限数。
- Rerank 响应的索引必须唯一且在候选范围内，分数必须为有限数，返回条数不得超过 `top_n`。
- 日志只允许模型、数量、字符数、内容哈希、尝试次数、错误类型和状态码；禁止记录输入正文、query、payload、响应正文或 API Key。

### 3. 统一群准入 (`group_admission`)

修改群行为限制前，读取 [ADR-0012](<docs/adr/0012-unified-group-admission.md>)。
编排层在业务效果发生前重新检查群准入。
适配器不解释群准入策略。
各业务插件独立检查自身业务开关。
业务开关不替代群准入或用户封禁。

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
- **必需依赖**：用户数据不提供停用开关。
- **失败边界**：好感度读取失败时，聊天停止回复。
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

- **启动与配置**：`QQ_BOTS` 非空时注册官 Bot 适配器；可信官 Bot 数字 QQ 按 `QQ_OFFICIAL_BOT_QQ_BY_APP` 启动配置。迁移 0019/0020/0021 建立游戏、收据/履约和强类型配置；`roulette_enabled` 默认 false，动态生效。按共享 ORM → 合法配置 → binding/admission → storage/recovery 顺序启动，首次成功恢复前不放行业务。
- **领域与事务**：2～6 人、6 发弹仓、15 分钟绝对行动期限；稳定 `join_seq` 不重排或复用。开始时冻结名字、编号顺序与道具权重。命令、收据、游戏状态和胜场通过 PG 事务/约束/锁/CAS 一致提交，不新增轮盘专用 Redis 依赖。
- **QQ 交互**：仅群 @ `/轮盘 …`，目标用稳定玩家编号，不依赖第二个 mention。按钮只填入命令，用户手动发送才执行。终局为完整普通单段，无引用/粗体/按钮；提及使用胜者已知 OpenID，不从昵称猜测。
- **一次履约**：领域提交后冻结文案与投影；配置变化不重渲染旧收据。发送开始后异常、取消或缺失合法平台 ID 保持 UNKNOWN / `PENDING_CONFIRMATION`，不得回滚领域事实、重抽、重发或猜测确认。后台无合法入站消息 ID 不创建群消息 outbox。
- **恢复与清理**：关闭开关不暂停绝对期限；受限群不推进，恢复后只处理旧当前玩家一次，下一人取得完整 15 分钟。每 60 秒小批扫描，每日调度器时区 04:00 清理，PG UTC 比较保留边界；收据/履约 7 天，cancelled/expired/failed 30 天，completed/结果玩家/胜场长期保留，waiting/active 不清理。关闭先撤权和调度，再有界收束自有工作，不 dispose 共享 ORM。
- **控制面**：`/api/v2/komari-roulette/status` 与 `/leaderboards/{inspect,rebuild}`；read/manage 分权。仅按 completed 真源校验/重建，不提供任意加分、重置、恢复发送或强制终局。`latest_scan/latest_cleanup` 是结果计数，不是时间戳。
- **交付边界**：Huly `komari-bot` 团队空间的[升级运维手册](https://huly.derbaynas.top:2096/workbench/derbaysworkspace/document/6aaa95dd649daed23e114cfa)记录升级、旧名主动迁移、运维与最终 QQ 样本；自动化及旧原型不代替当前部署版本的真实 QQ 展示/通知验收。

### 4. Hindsight 共享长期记忆 (`komari_memory`)

TSK-193 起运行时只接入 Hindsight 共享长期记忆。
TSK-197 已物理删除旧四层运行代码、专用配置列与记忆总结 Prompt。
正式来源数据表仍保留；运行时不读取它们。

| 旧层 | 存储 | 表 | 当前状态 |
|------|------|-----|----------|
| 1. 对话摘要 | PG | `komari_memory_conversations` | 正式来源存储，运行时不读取 |
| 2. 用户画像 | PG | `komari_memory_user_profile` | 正式来源存储，运行时不读取 |
| 3. 互动历史 | PG | `komari_memory_interaction_history` | 正式来源存储，运行时不读取 |
| 4. 实体知识 | PG | 历史 EntityRepository 数据 | 运行时不读取 |

来源数据的导出与处置是运维边界。
代码退役不表示生产数据已迁移或删除。
导出完成前不得删除正式来源存储。

- 顶层暴露面只有 `KomariMemoryConfigSchema` / `HindsightMemoryService` / `HindsightRecallSession` / `HindsightRetainRequest` / `HindsightUnavailableError` / `get_hindsight_memory`。
- 配置只保留 5 个 `hindsight_*` 字段。
- 共享记忆不提供停用开关。
- `startup_required=False` 保留。
- 启动创建一个稳定 caller，并尝试一次有界探测；配置读取失败不中止其他插件。
- 每个回复任务打开一个会话；会话冻结一份设置快照，并复用一个官方客户端。
- 就绪要求读取目标 bank 配置成功，并完成一次真实 `arecall` 探测。
- 失败按配置的恢复间隔做单飞重试；设置指纹变化允许立即重新探测。
- 新任务仍会重新读取 bank；旧会话的迟到失败不能覆盖新指纹就绪状态。
- TSK-195 通过 `hindsight_memory` 承诺保存已确认送达的角色回复。
- 回复准备阶段冻结 Hindsight 请求。
- 冻结内容包括正文、群、Bot、适配器、触发消息、引用目标与生成时间。
- 请求的 `source_id` 为 `reply:<fulfillment_id>`。
- 请求时间戳使用回复生成时间，不代表平台送达时间。
- 只有 `DELIVERED` 父履约的执行器可以提交请求。
- 执行器按冻结的目标指纹打开会话。
- 同一目标允许凭据轮换。
- 地址或 bank 变化时拒绝改投。
- 最终提交准入检查拒绝时，执行器保留义务。
- 准入拒绝不消费重试预算。
- `document_conflict` 记为永久幂等冲突。
- 其他错误只记录调用侧归一化的安全码。
- 记忆失败不撤销已确认送达事实。
- 好感度与角色回复历史保持独立执行。
- 角色回复历史继续向判定引擎报告已送达事实。
- 记忆承诺恢复不重发平台内容。
- 迁移 `0029` 拒绝存在未完成旧父履约的数据库。
- 切换前必须完成旧已送达义务。
- 未知送达必须通过既有授权运维流程对账。
- 关闭只关闭自有客户端与在途会话；不关闭 Redis 或共享 ORM。
- 聊天只执行一次自动召回；模型工具 `recall_memory` 复用同一会话。
- 召回失败停止本次回复；空召回是成功。
- 召回内容作为结构化 memory 数据交给 DSH 不可信数据渲染器。
- 当前群准入在打开会话前和自动召回前各检查一次。
- 近期上下文写入前重新检查当前群准入。
- 聊天知识检索通过 `embedding_guard` 在实际向量化前检查当前群准入。
- 不按记忆来源群过滤；工具参数不能选择 bank、地址或凭据。
- TSK-197 已删除旧总结、画像、互动与忘却 worker，插件启动不加载它们。
- 旧四层表、向量表与 HNSW 索引仍由历史 Alembic 迁移链保留。
- `0030` 只删除旧配置列、上下文互动列、聊天死 Prompt 列与记忆总结 Prompt 表。
- `0030` 不删除正式来源记忆表、向量、来源索引或旧任务表。
- `0031` 只删除无调用方的 `komari_chat_config.memory_search_limit`。
- `0031` 拒绝降级。
- 配置故障在新版本上向前修复。
- 运行时仍不执行任何 DDL。

### 4.1 知识与帮助关键词索引

- `komari_knowledge` 与 `komari_help` 使用不可变内存快照，重建完成后一次性替换，查询不得观察到半成品索引。
- PostgreSQL 语句级触发器在业务写入事务内递增 `komari_search_index_versions`；其他 worker 最多 1 秒轮询到变化并重建。
- 初始化与索引重建必须走单飞锁；重建失败继续保留旧快照，关闭时等待在途重建结束后清空。

### 5. 判定引擎 (`komari_decision`)

依据：TSK-180、TSK-181～183、ADR-0007、ADR-0011。
修改入口交接或退役边界前，读取 ADR-0007。
修改判定配置或 Prompt 初始化前，读取 ADR-0011。

#### 请求与输入

判定直接使用官方 TypeSafe SDK，不经过 `llm_provider`。
`services/jev_request.py` 管理单次请求的客户端与总截止。
默认模型为 `jev-1.14.0`。
连接参数读取统一的 `komari_decision` 强类型配置。
`jev_base_url` 与 `jev_api_key` 默认为空，配置后才允许调用。

SDK 追加 `/v1/systemone`。
基础地址只保留此前的网关前缀。

每次判定最多发送 1 次远程请求。
SDK 与业务层均关闭自动重试。
失败时不切换模型或旧判定。
输入使用当前目标、目标前 10 条合格近期上下文与引用消息链。
OneBot 入口正文以 `event.original_message` 为真源。
判定不读取长期记忆、用户画像或好感度。

引用补取只沿明确的平台引用关系读取。
显式引用允许读取被普通长度规则过滤的原文。
引用节点不因此重新成为判定目标或普通近期上下文。
不可读节点、循环与超时保留不完整标记。

输入使用稳定身份代号。
群消息标记为不可信数据。
媒体与凭据脱敏复用 `komari_bot/llm/diagnostic_sanitizer.py`。

#### 同次判定与单次交接

- `get_decision_engine()` 是唯一公共引擎 getter。
- `get_decision_dispatch()` 提供同次入站分发的共享载体。
- 同次分发只执行一次 `evaluate(DecisionRequest)`。
- 总结与聊天消费同一份冻结结果。
- 业务交接前必须成功领取 `claim_handoff` 资格。
- 平台重投不能重复判定、抽签或领取交接资格。
- `observe_delivered_reply()` 只接收已送达回复事实。
- 跨插件共享的请求与结果类型位于 `komari_bot/decision/jev.py`。

| 入口事实 | Jev 问题 |
| --- | --- |
| 普通非总结候选 | 回应资格 Choice + 保留用途 Choice |
| 非平台召唤的总结候选 | 两个 Choice + 总结 Noul |
| 平台召唤的总结候选 | 仅总结 Noul |
| 非总结候选的平台召唤 | 无模型调用，仍检查持久去重与交接资格 |

真实平台 @ 或引用角色消息构成确定性召唤。
文本称呼只提供输入事实与长度例外。

`required` 进入既有强制回复路径。
`optional` 每个事件最多抽签一次。
抽中后继续执行原有主动频控。
`silent` 不生成普通回复。
模型置信度只用于诊断，不作为抽签概率。

原始 Choice 与代码组合后的保留资格分别记录。
`required` 与 `optional` 均保留记忆候选资格。
`silent + candidate` 同样保留记忆候选资格。
`silent + noise` 排除记忆候选资格。
技术失败不能伪装成 `silent` 或 `noise`。
记忆仍由既有调用方保存，不由判定引擎直接写入。

总结 Noul 仅在 `p > 0.7` 时命中。
带数量的总结也必须经过 Noul。
数量提取和范围校验仍由总结执行服务负责。
总结命中后先停止传播，再领取 `summary` 交接资格。
交接拒绝或异常时仍保持停止传播。
总结未命中后，聊天复用同一份结果。

#### 执行与失败边界

执行占位与交接资格由 PostgreSQL 持久保存。
判定使用独立 Redis 近期上下文，不替换回复历史来源。

默认同群执行数为 1。
默认部署共享执行数为 4。
默认同群等待上限为 5 条，不含正在执行的任务。
排队、整链引用补取与 Jev 请求各有独立的 3 秒默认截止。
Jev 请求截止覆盖连接、发送、接收与响应校验。

引用补取预算从取得执行名额时开始计算。
取得名额后的 Prompt 读取耗时也计入该预算。
引用补取超时后，使用已取得内容与缺失标记继续判定。
迟到的 Jev 结果不能用于业务交接。
重启不恢复旧队列或补做旧请求。
判定日志清理不删除持久事件去重身份。

System One 不提供人为停用开关。
运行状态区分 `ready` 与 `failed`。
缺少有效连接或 Prompt 时报告 `failed`。
判定初始化失败不阻止独立功能启动。
保留群准入撤权与进程关闭取消。
历史停用原因仍可查询，不作为新的运行状态。

技术失败通知直接使用 `GroupTaskFailureNotifier.notify`。
通知前先领取持久化的 `failure_notification` 资格。
总结候选判定失败会停止总结与聊天两条路径。
总结失败沿用固定群提示，不依赖生成中表情。
普通判定失败不逐条发送群错误提示。
通知继续遵守共享开关、冷却及逐效果准入。

聊天 `evaluate` 或 `chat` 交接抛出普通异常时，就地停止处理。
该异常分支只记录固定安全日志，不触发回复生成失败通知。
调用方取消继续传播 `CancelledError`。

#### 管理与退役

判定 Prompt 使用独立资源 `komari_decision`。
角色描述、回应资格、保留用途与总结识别共同组成一个修订。
配置、模型与 Prompt 在任务内保持冻结。
共享调度额度仍按运行时配置控制新领取。

管理接口统一位于 `/api/v2/komari-decision`。
`/health` 只读取被动快照，不调用模型。
健康与判定元数据要求 `decision:read` 权限。
判定正文要求 `decision_logs:read` 权限。
正文访问执行管理审计。

不存在或已清理的日志返回 404。
日志存储不可用返回 503。
管理接口细节以 `komari_decision/management_api.py` 为准。

旧场景运行代码、管理路由与后台任务已物理删除。
`/api/v2/komari-decision-scenes` 不再注册。
迁移 `0025` 永久删除旧场景四表与 38 个专用配置字段。
`0025` 的 `downgrade` 拒绝回退。
禁止恢复旧签名适配、双跑或故障回退。
公共向量生成与重排继续服务知识库与帮助检索。

判定存储与迁移细节见插件 `orm_models.py` 和迁移 `0022` 至 `0025`。
新组合效果验收使用固定模型与事前冻结的样例期望。
回放分别记录误参与、漏回应与技术失败。
回放不发送业务回复，也不写入业务记忆。
代码合并不代表生产部署或生产数据删除已获授权。

### 6. 聊天处理器拆解 (`komari_chat`)

`message_handler.py` 分离上下文读取与回复生成。
`reply_fulfillment_workflow.py` 管理平台发送与送达后承诺。

- `_read_buffers()` 读取同群近期消息缓冲。
- 同群缓冲包含已确认送达的角色回复。
- `store_current` 控制当前消息入缓冲。
- `_generate_reply_core()` 执行读取与生成，不执行记忆写入。
- 生成包装层从实际 Hindsight 会话标注目标指纹。
- 生成核心只向 DSH 提交结构化数据。
- `fulfill()` 在发送前冻结父身份与适用承诺。
- 发送前登记 `mark_send_started()`。
- 平台发送结果落为 `DELIVERED`、`PENDING_CONFIRMATION` 或 `NOT_DELIVERED`。
- 仅 `DELIVERED` 父履约允许执行送达后承诺。
- 好感度调整、角色回复历史与 Hindsight 记忆始终适用。
- 存在主动回复预占时，追加主动回复确认承诺。
- 每项承诺维护独立的尝试、退避与完成状态。
- 全部适用承诺完成后，父履约才能完成。
- 发送开始后结果未知时，父履约保持待确认。
- 承诺重试与运维续跑不重发平台内容。
- `_attempt_reply()` 不执行送达后承诺。

表情反应与失败通知契约：
- 表情反应在 `_attempt_reply()` 内、调用 `_generate_reply_core()` 之前以 `asyncio.create_task` fire-and-forget 派发（任务引用挂 `self._reaction_tasks` 防 GC），提示“正在生成”；`commit_delivered_reply()` 不再触发表情，送达后不撤下。
- `_attempt_reply()` 返回 `(PendingReply | None, stored, ReplyFailureInfo | None)`；生成失败（异常、空回复、delta 缺失、预占租约丢失）转为 `ReplyFailureInfo` 而非上抛（CancelledError 除外）；频控冷却/超限/重复等正常控制流返回 `(None, False, None)` 不通知。
- 失败分流边界是“是否贴过表情”：`report_reply_failure()` 在 `reaction_sent=True` 时以 reply 段引用原消息补发群内固定错误文本，并向 SUPERUSERS 私聊极简诊断卡（trace/群号/触发原因/阶段/异常类型+一行摘要）；Redis key `komari_chat:error_notify:{group_id}:{error_type}` SET NX EX 300 去重，Redis 异常降级照常通知；`komari_context.error_notify_enabled=false` 仅静默通知，不影响群内错误文本；实现位于 `services/error_notify.py`，善后方法自身绝不抛出。

主动回复频控契约：
- 非强制回复在生成前调用 Redis Lua 原子预占，同时检查冷却、最近一小时已确认名额与生成中名额；预占 ID 使用平台消息 ID，重复投递不会重复生成。
- 生成失败、空回复或发送失败调用 `release_proactive_reply()` 幂等释放；发送成功后先调用 `confirm_proactive_reply()`，再提交其他聊天副作用。
- 预占带 `proactive_reservation_ttl_seconds`；进程崩溃后的孤儿预占按 TTL 淘汰。已确认名额进入一小时滑动窗口，释放接口不得撤销已确认名额。

`process_message(..., reply_allowed=False)` 用于 chat 封禁：保留原消息的判定和缓冲写入，但在 `_attempt_reply()` 前返回，并记录 `blocked_by_user_ban`。

公开 debug 入口：
- `komari_chat.generate_debug_reply()` 使用发起者身份与当前群上下文。
- debug 只执行读取与生成，跳过判定与业务副作用。
- debug 使用 `debug-reply-*` 任务标识。
- debug 返回 `DebugReplyResult` 安全生成结果。
- 底层依赖未初始化时抛出 `RuntimeError`（可展示的错误信息）

#### 原生图片输入

修改图片传输或安全诊断前，读取 ADR-0016。
`NativeImageInput` 复用现有安全下载器。
引用图片在前，当前图片在后，失败不重排索引。
任务起点冻结 8 项下载预算。

地址解析在受控 resolver 内校验，每跳重定向重复校验。
真实格式由 Pillow 解码确定，只接受 JPEG、PNG、GIF、WebP。
下载继续限制字节、像素、并发和累计活动时限。

可信图片描述符与有界分块送入任务私有附件缓存。
DSH 官方图片助手执行规范化与模型输入投影。
文本模型使用官方占位内容，不探测能力或回退视觉服务。
全部下载失败在提交 DSH 任务前终止。
部分失败成功任务保留安全摘要。
图片请求失败与通用任务故障保持不同分类。

异常链与递归 traceback locals 不保留 URL、载荷或上游正文。
任务确认停止后释放附件。
子进程实际退出后才删除宿主附件根目录。
通知仍复用共享权限、冷却与固定群道歉规则。
debug 干跑不发送失败通知。

### 7. 群聊总结执行服务 (`group_history_summary`)

`execute_group_summary()` 冻结配置并持有 Redis 群租约。
能力检查在创建 DSH 任务前完成。
正常入口继续消费同次 Jev 总结判定与单次交接资格。
debug 复用执行服务，保留能力检查与群锁。

DSH 总结预设与聊天预设隔离提示词、工具和会话。
模型只获得近期、用户、主题历史查询与最终总结提交工具。
Python 历史工具保留数量限制、过滤规则与部分覆盖校验。
历史分页仅供可信桥接，不向模型开放任意页面读取。
空历史仍需最终提交，结果保持既有空历史提示。

`summary_planning_round_limit` 继续作为任务预算输入。
历史数量、锁设置与渲染布局保持原有配置所有权。
最终结果经过校验后才进入纯渲染与既有输出流程。
租约丢失时先取消并等待任务释放，再归还群锁。
准入拒绝保持静默，不伪装为生成技术失败。

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

# 生成新 revision（当前命令默认 autogenerate；审阅生成的迁移）
poetry run python -m komari_bot.db.orm_bootstrap revision -m "描述"
```

规则：

- 新增/修改表结构一律走迁移：改 `config_schema.py` / `prompt_schema.py` / `orm_models.py` 等 SQLModel 元数据后 autogenerate，`check` 必须零 diff 通过；CI（`.github/workflows/pr-acceptance.yml`）执行 `upgrade head` + `check` 守门。
- 使用 `op.execute` 手写特殊 DDL 的 revision。
- 特殊 DDL 包括向量、触发器、`UNLOGGED` 与 advisory lock。
- 特殊 DDL 不依赖 autogenerate。
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
9. **debug 无副作用**：`.debug reply` 走 `generate_debug_reply()`，完全不触发 Redis push、好感度 adjust、Hindsight 写入或冷却/频控
10. **诊断结构与投递**：完整报告绝不包含完整 prompt、reasoning content、base64、历史或画像正文，且只私聊已鉴权 SUPERUSER；群内默认仅返回 request ID/状态，`--public` 仍必须隐藏输入、输出、用户标识、异常正文与工具参数
11. **用户封禁边界**：`chat` 只压制 `komari_chat` 的实际回复；其他用户 matcher 统一属于 `command`，封禁时必须静默且保留原 matcher 的传播阻断语义
12. **内容预算**：用户/管理入口可写文本必须复用 `komari_bot.llm.content_budget`；同时检查字符、UTF-8 字节、估算 token 与关键词组合，不得在各插件复制限额或静默截断
13. **fetch_page 脱敏**：保持查询结果的不可信数据边界。
    抓取失败日志只记录 URL 数量与 URL 集合 SHA-256 指纹。

14. **QQ 按钮协议**：显式配置权限，现有绑定/轮盘公开群命令按钮使用 `permission.type=2`；仅指定用户入口才使用 `type=0` + 真实 QQ 群成员 OpenID 非空名单，禁止缺失权限、空名单或身份缺失时放宽权限。按钮可点击性不替代服务端授权。按官方字段表完整输出 `label` / `visited_label` / `style` / `action.type` / `action.permission.type` / `data` / `unsupport_tips`，群命令保持 `enter=false` / `reply=false`；在真实 SDK 最终序列化层验收，不依赖客户端或 SDK 的宽松默认。设计、历史审计与修复说明见 `docs/adr/0013-qq-button-permission-policy.md`（TSK-296～299）；生产物化层已补齐字段，真实客户端展示仍须按实际运行版本记录样本。

---

*本文件由 AI 生成于 2026-04-26，最后更新于配置业务开关整合（TSK-210）。发现不一致请以实际代码为准并更新本文档。*
