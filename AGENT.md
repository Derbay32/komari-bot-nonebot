# komari-bot AI 上下文文档

> **AI 智能体在处理本项目任何任务前必须先阅读此文件。**

## 项目概述

komari-bot 是基于 [NoneBot2](https://github.com/nonebot/nonebot2) 构建的 QQ 机器人，核心角色是《败犬女主太多了》中的 **小鞠知花**。项目代号 Project NEON-TAVERN。

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
| Embedding | fastembed（本地） | 默认 `BAAI/bge-small-zh-v1.5` |
| 部署 | Docker + Docker Compose | Gunicorn + Uvicorn |
| CI/CD | GitHub Actions → Docker Hub | 发布 tag 自动构建 |
| Lint | Ruff (py313) + Pyright `standard` | 零容忍类型错误 |

## 目录结构

```
komari-bot/
├── AGENT.md                              # ← 本文件
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
│   │   ├── profile_compaction.py         #   用户画像 LLM 压缩
│   │   ├── onebot_rules.py               #   group_message_rule() 等
│   │   └── sentry_support.py             #   Sentry 初始化 + 异常过滤
│   └── plugins/                          # NoneBot 插件（19个）
│
├── config/                               # 运行时配置
│   ├── config_manager/                   #   各插件的 JSON 配置 + .example
│   └── prompts/                          #   YAML 提示词模板
│
├── docs/                                 # 文档
│   ├── ai-context/                       #   AI 上下文（含 handoff.md 交接记录）
│   ├── local/                            #   本地开发记录
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
  config_manager ───────────── 配置热加载（JSON + .env）
  permission_manager ───────── 权限检查（白名单、插件开关、SUPERUSER）
  embedding_provider ───────── 向量化 + Rerank 服务
  llm_provider ─────────────── LLM 网关（DeepSeek/OpenAI 兼容）
  user_data ────────────────── 用户好感度数据库（SQLite）

核心功能层
  komari_memory ────────────── 四层记忆系统
  komari_decision ──────────── 回复/记忆判定引擎
  komari_chat ──────────────── AI 聊天处理器（编排者）
  komari_knowledge ─────────── RAG 知识库
  komari_help ──────────────── 智能帮助系统
  group_history_summary ────── 群聊历史总结

辅助功能层
  character_binding ────────── .nn 昵称指令
  sr ───────────────────────── 神人榜抽签
  jrhg ─────────────────────── .jrhg 今日好感
  komari_healthcheck ───────── 健康检查 / Bark 推送
  komari_sentry ────────────── Sentry 集成
  komari_status ────────────── Uptime Kuma 状态查询
  komari_management ────────── 管理 REST API
```

### 数据流路径

```
群消息 → komari_chat（MessageHandler）
         ├─ 调用 komari_memory 获取记忆上下文
         ├─ 调用 komari_decision 判定回复策略
         ├─ 调用 llm_provider 生成回复
         └─ 调用 komari_memory 写入新记忆
```

## 核心机制详解

### 1. 配置管理 (`config_manager`)

- **三层优先级**：JSON 文件 > `.env` 环境变量 > Pydantic 默认值
- **热加载**：`ConfigManager.get()` 自动检测文件 mtime 变化
- **持久化**：`update_field()` → 内存更新 → 写回 JSON（0644）
- **线程安全**：单例 + `RLock`，按 `plugin_name:schema_name` 区分实例
- **使用模式**：
  ```python
  from komari_bot.plugins.config_manager import get_config_manager
  config = get_config_manager("plugin_name", MyConfigSchema)
  value = config.get().some_field       # 运行时获取（自动热加载）
  config.update_field("some_field", x)  # 更新并持久化
  ```

### 2. LLM 网关 (`llm_provider`)

导出的核心函数（位于 `__init__.py`）：
- `generate_text(prompt, model, ...)` → `str`
- `generate_completion(...)` → `LLMCompletionResultSchema`（含 thinking 内容）
- `generate_text_with_messages(messages, model, ...)` → `str`
- `test_connection()` → `bool`

关键规则：
- **防注入指令** 在 `_build_safe_system_instruction()` 中注入到 system 角色
- `max_tokens` 必须为 **`int`**（不能是 `float`），默认 8192
- 知识库注入：`enable_knowledge=True` 时自动检索并注入到 system prompt
- 调用日志：所有请求记录到 `logs/llm_provider/`

### 3. 权限管理 (`permission_manager`)

```python
from komari_bot.plugins.permission_manager import check_runtime_permission
ok, reason = await check_runtime_permission(bot, event, config)
```

- **禁止** 在 matcher 创建时用 `rule=` 做静态权限检查（会捕获模块加载时的旧配置）
- **必须** 在处理器内调用 `check_runtime_permission()` 动态检查
- `SUPERUSERS` 通过 `.env` 配置，白名单通过 JSON 配置

### 4. 四层记忆系统 (`komari_memory`)

| 层 | 存储 | 表 | 说明 |
|----|------|-----|------|
| 1. 对话摘要 | PG | `komari_memory_conversations` | 向量搜索 + 遗忘模糊化 |
| 2. 用户画像 | PG | `komari_memory_user_profile` | JSONB traits，LLM 压缩 |
| 3. 互动历史 | PG | `komari_memory_interaction_history` | JSONB records，增量更新 |
| 4. 实体知识 | PG | 通过 EntityRepository | 关键词 + 向量检索 |

关键类：`PluginManager` → `MemoryService` → `ConversationRepository` / `EntityRepository` + `ForgettingService`
注意：EntityRepository 是跟 komari_knowledge 共享知识表还是独立管理需确认。

### 5. 判定引擎 (`komari_decision`)

核心服务：
- `SceneRuntimeService` — 场景生命周期管理
- `SceneAdminService` — 场景运维（CRUD）
- `UnifiedCandidateRerankService` — 候选回复重排序
- `SocialTimingService` — 社交时机判定（主动回复冷却、频控）
- `MessageFilter` — 消息过滤

### 6. 编码规范（必须遵守）

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

### 7. 数据库操作模式

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
# 安装依赖
poetry install --with dev

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
7. **Sentry 过滤**：NoneBot 控制流异常（StopPropagation 等）已在 `sentry_support.py` 中过滤

## 相关文档

| 文档 | 位置 | 用途 |
|------|------|------|
| 任务交接记录 | `docs/ai-context/handoff.md` | 历史任务详情、决策记录、注意事项 |
| 项目结构模板 | `docs/ai-context/project-structure.md` | 项目组织说明 |
| 集成架构 | `docs/ai-context/system-integration.md` | 跨组件通信模式 |
| 部署文档 | `docs/ai-context/deployment-infrastructure.md` | 容器化、CI/CD |
| 组件文档 | `docs/*.md` | 各插件的详细设计文档 |

---

*本文件由 AI 生成于 2026-04-26，基于完整的项目探索。发现不一致请以实际代码为准并更新本文档。*
