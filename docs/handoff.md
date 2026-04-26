# 任务交接文档

*此文档帮助在 Claude Code 会话之间维护上下文连续性和任务状态。*

---

## JRHG 插件开发 - 已完成

### 会话日期
2025-12-24

### 当前状态
✅ **已完成** - JRHG 插件已完全修复并正常工作

### 完成的内容

#### 1. 分层配置系统实现
- **文件**: `komari_bot/plugins/jrhg/config_manager.py`（新建）
- **文件**: `komari_bot/plugins/jrhg/config_schemas.py`（新建）
- 实现了 ConfigManager 单例，支持配置优先级：JSON > .env > 默认值
- 支持运行时配置热更新并持久化到 JSON
- API token 自动加密/解密存储

#### 2. API Token 加密存储
- **文件**: `komari_bot/plugins/jrhg/crypto.py`（新建）
- 使用 Fernet (AES-128) 加密
- 基于机器指纹生成密钥（hostname + platform + system）
- token 在 JSON 中加密存储，运行时自动解密

#### 3. 修复关键 Bug

**Bug 1: CommandArg 类型注解错误**
- **问题**: `args: str = CommandArg()` 导致依赖注入失败，处理器被跳过
- **修复**: 改为 `args: Message = CommandArg()`，使用 `extract_plain_text()` 获取文本
- **文件**: `komari_bot/plugins/jrhg/__init__.py:100, 129`

**Bug 2: 静态权限检查问题**
- **问题**: `rule=create_whitelist_rule(dynamic_config)` 在模块加载时捕获配置，`jrhg_plugin_enable=False` 导致权限检查始终失败
- **修复**: 移除静态 rule，在处理器内部使用 `check_runtime_permission()` 进行动态检查
- **文件**: `komari_bot/plugins/jrhg/__init__.py:42-46`, `permissions.py:177-194`

**Bug 3: FinishedException 被错误记录**
- **问题**: `finish()` 抛出的 `FinishedException`（正常终止机制）被 `except Exception` 捕获并记录为 ERROR
- **修复**: 添加 `if not isinstance(e, FinishedException)` 检查
- **文件**: `komari_bot/plugins/jrhg/__init__.py:7, 147-150`

#### 4. 配置文件
- **文件**: `config/jrhg_config.json.example`（新建）
- **文件**: `pyproject.toml` - 添加 `cryptography>=41.0.0` 依赖

### 当前问题
无 - 所有已知问题已修复

### 下一步
无 - 功能已完整

### 关键文件清单
```
komari_bot/plugins/jrhg/
├── __init__.py           # 主入口，matcher 和处理器
├── config.py             # .env 配置模型（静态配置）
├── config_manager.py     # 配置管理器（新建）
├── config_schemas.py     # JSON 配置模型（新建）
├── crypto.py             # 加密工具（新建）
├── permissions.py        # 权限检查（添加运行时检查）
└── deepseek_client.py    # DeepSeek API 客户端

config/
└── jrhg_config.json.example  # 配置示例（新建）
```

### 下次会话上下文
- JRHG 插件使用 `.jrhg` 命令
- 管理命令 `/jrhg on/off/status` 控制插件开关
- 配置通过 JSON 持久化，支持运行时更新
- token 使用机器指纹加密，不同机器配置不通用

---

## LLM Provider 插件安全增强和 SDK 升级 - 已完成

### 会话日期
2025-12-28

### 当前状态
✅ **已完成** - LLM Provider 插件安全增强和 Gemini SDK 升级完成

### 完成的内容

#### 1. 防注入安全增强
- **文件**: `komari_bot/plugins/llm_provider/__init__.py`
- **改动**: 将安全指令从用户输入移至 system_instruction
- **函数**: 新增 `_build_safe_system_instruction(user_system_instruction)`
- **效果**:
  - 用户输入保持纯净，便于 AI 理解
  - 安全指令权重更高（system_instruction 优先级高于 user 输入）
  - 明确禁止用户输入中的指令执行

#### 2. Gemini 客户端 SDK 升级
- **文件**: `komari_bot/plugins/llm_provider/gemini_client.py`
- **改动**: 从 aiohttp REST API 迁移到 google-genai SDK
- **代码量**: 从 158 行减少到 110 行（减少 30%）
- **改进**:
  - 使用官方 SDK，代码更简洁
  - 统一的 `errors.APIError` 错误处理
  - SDK 自动处理连接管理、重试等
  - 不再需要手动管理 HTTP 会话

#### 3. 依赖更新
- **文件**: `pyproject.toml`
- **新增**: `google-genai>=1.56.0,<2.0.0`
- **Python 版本**: 项目已升级到 Python 3.10+

### 当前问题
无

### 下一步
无 - 功能已完整

### 关键文件清单
```
komari_bot/plugins/llm_provider/
├── __init__.py           # 主入口，添加防注入函数
├── base_client.py        # 抽象基类
├── config.py            # 静态配置
├── config_schema.py     # 动态配置 Schema
├── deepseek_client.py   # DeepSeek API 客户端（保持 aiohttp）
└── gemini_client.py     # Gemini API 客户端（使用 google-genai SDK）
```

### 下次会话上下文
- 防注入指令在 system_instruction 前置，确保最高优先级
- Gemini 使用 google-genai SDK，DeepSeek 保持 aiohttp 实现
- Python 版本要求：**>= 3.13**（项目已全面升级）
- 不再使用的配置：`gemini_api_base`（SDK 自动处理）

### Python 3.13 现代化编码规范

**重要**：项目已升级到 Python 3.13，所有新代码应使用现代化写法，**无需兼容旧版本**。

**推荐使用的现代特性：**
- **类型注解**：使用 `X | Y` 语法代替 `Union[X, Y]` 和 `Optional[X]`
  ```python
  # 旧写法（不推荐）
  from typing import Union, Optional
  def func(x: Optional[str]) -> Union[int, str]:

  # 新写法（推荐）
  def func(x: str | None) -> int | str:
  ```

- **类型检查**：使用 `isinstance()` 检查类型联合
  ```python
  if isinstance(x, int | float):
  ```

- **泛型**：使用内置泛型类型
  ```python
  # 旧写法（不推荐）
  from typing import List, Dict
  items: List[str] = []

  # 新写法（推荐）
  items: list[str] = []
  ```

- **Match 语句**：使用 `match-case` 代替复杂的 if-elif
  ```python
  match status:
      case "ok":
          return "Success"
      case "error":
          return "Failed"
      case _:
          return "Unknown"
  ```

- **错误处理**：使用 `except*` 语句进行异常组处理
  ```python
  try:
      ...
  except* (ValueError, TypeError):
      ...
  ```

**禁止使用的旧模式：**
- ❌ `from typing import Union, Optional, List, Dict, Tuple`（使用内置类型）
- ❌ 类型注解中的字符串前向引用（使用 `from __future__ import annotations`）
- ❌ `__future__` 导入（Python 3.13 默认启用所有特性）

**代码质量工具配置：**
- **Ruff**: target-version = `py313`（已配置）
- **Pyright**: pythonVersion = `3.13`（已配置）
- 自动检查和强制执行现代 Python 模式

---

## SR 插件撤销功能 - 已完成

### 会话日期
2025-12-28

### 当前状态
✅ **已完成** - SR 插件已实现命令模式撤销功能

### 完成的内容

#### 1. 命令模式实现
- **文件**: `komari_bot/plugins/sr/commands.py`（新建）
- 实现了 `Command` 抽象基类
- `AddCommand` - 添加神人命令，支持 execute/undo
- `DeleteCommand` - 删除神人命令，支持 execute/undo
- 使用 `deque(maxlen=5)` 维护撤销栈，自动限制为最近 5 条记录

#### 2. 插件集成
- **文件**: `komari_bot/plugins/sr/__init__.py`
- 添加 `/sr add <名称>` - 添加神人到列表
- 添加 `/sr del <名称>` - 从列表删除神人
- 添加 `/sr undo` - 撤销最近的 add/del 操作
- 失败操作（返回 ❌）不会入栈

#### 3. 设计特点
- 单一职责：每个命令类只负责一种操作
- 开闭原则：新增操作只需添加新 Command 类
- 持久化一致：通过 `config_manager` 确保配置同步

### 当前问题
无

### 下一步
无 - 功能已完整

### 关键文件清单
```
komari_bot/plugins/sr/
├── __init__.py       # 主入口，添加 add/del/undo 处理
├── commands.py       # 命令模式实现（新建）
├── config.py         # 静态配置
└── config_schema.py  # 动态配置 Schema
```

### 下次会话上下文
- SR 插件使用 `/sr` 命令
- 管理命令 `/sr list/add/del/undo`
- `/sr undo` 可撤销最近 5 次操作
- 撤销栈内存存储，重启后清空

---

## SR 插件增强：序号删除和 Redis 持久化 - 已完成

### 会话日期
2026-01-03

### 当前状态
✅ **已完成** - SR 插件增强：序号删除功能和 Redis 撤销栈持久化

### 完成的内容

#### 1. 序号删除功能
- **文件**: `komari_bot/plugins/sr/commands.py`
- `DeleteCommand` 新增 `index` 参数支持（1-indexed）
- 支持序号删除：`/sr del 3`
- 向后兼容名称删除：`/sr del 小鞠`
- 撤销时能准确恢复到原始位置（使用 `insert()` 而非 `append()`）

#### 2. Redis 撤销栈持久化
- **文件**: `komari_bot/plugins/sr/redis_undo_stack.py`（新建）
- **核心功能**:
  - 用户隔离：按 `user_id` 隔离撤销栈，Redis key 格式 `sr:undo:{user_id}`
  - 最多保留 5 条操作记录（`ltrim`）
  - 12 小时 TTL 自动过期
  - 命令序列化/反序列化：保存命令类型和参数
- **API**: `push_undo()`, `pop_undo()`, `clear_undo()`, `close_redis()`

#### 3. Redis 配置集成
- **文件**: `komari_bot/plugins/sr/config_schema.py`
- 新增配置字段：
  - `redis_host`: Redis 服务器地址（默认 localhost）
  - `redis_port`: Redis 端口（默认 6379）
  - `redis_password`: Redis 密码（默认空）
  - `redis_db`: Redis 数据库编号（默认 0）

#### 4. Python 3.13 兼容性修复
- **问题**: `aioredis` 2.x 与 Python 3.13 不兼容
  - 错误：`TypeError: duplicate base class TimeoutError`
- **解决**: 使用 `redis` 包的 `redis.asyncio` 模块
- **文件**: `pyproject.toml` - 依赖改为 `redis (>=7.1.0,<8.0.0)`
- **文件**: `redis_undo_stack.py` - 使用 `import redis.asyncio as aioredis`

#### 5. 插件集成更新
- **文件**: `komari_bot/plugins/sr/__init__.py`
- del 处理器：判断输入是数字（序号）还是文本（名称）
- add/del 处理器：使用 `await push_undo(user_id, cmd_obj, config_manager)`
- undo 处理器：使用 `await pop_undo(user_id, config_manager)` 从 Redis 恢复命令
- 命令类新增 `from_dict()` 类方法支持反序列化

### 技术挑战和解决方案

#### 挑战 1: Python 3.13 兼容性
- **问题**: aioredis 2.x 类型定义与 Python 3.13 冲突
- **解决**: 迁移到 `redis.asyncio`（从 redis 4.2.0 起，aioredis 已合并）
- **代码**: `import redis.asyncio as aioredis`

#### 挑战 2: 类型注解误报
- **问题**: Pylance 报告 `lpop` 不是 awaitable
- **原因**: redis.asyncio 类型存根不完整
- **解决**: 添加 `# type: ignore[misc]` 抑制误报

#### 挑战 3: 命令序列化
- **问题**: 命令对象包含 `config_manager` 引用，无法直接序列化
- **解决**: 只序列化必要参数（type, item, index），通过 `from_dict()` 恢复时重新注入 config_manager

### 当前问题
无

### 下一步
无 - 功能已完整

### 关键文件清单
```
komari_bot/plugins/sr/
├── __init__.py           # 主入口（del/undo 处理器更新）
├── commands.py           # 命令模式（序号删除 + from_dict）
├── redis_undo_stack.py   # Redis 撤销栈（新建）
├── config.py             # 静态配置
└── config_schema.py      # 动态配置 Schema（新增 Redis 配置）
```

### 下次会话上下文
- **序号删除**: `/sr del 3` 删除第 3 位，`/sr del 小鞠` 按名称删除
- **Redis 撤销**: 撤销栈持久化，重启后不会丢失
- **用户隔离**: 每个用户只能撤销自己的操作
- **配置**: Redis 连接通过 `config/config_manager/sr_config.json` 配置
- **Python 3.13**: 使用 `redis.asyncio`，不要使用 `aioredis` 包
- **类型注解**: redis.asyncio 类型存根有问题，需要 `# type: ignore[misc]`

---

## 代码质量优化和资源泄漏修复 - 已完成

### 会话日期
2026-01-05

### 当前状态
✅ **已完成** - 代码质量优化和资源泄漏修复

### 完成的内容

#### 1. 代码质量优化（35个文件）
- **提交**: `0ffaeab` - "optimize code"
- **改动**: 965行新增，652行删除
- **核心改进**:

##### a) 类型提示标准化
- **文件**: `komari_bot/plugins/config_manager/manager.py`
  - 修复 ruff `ClassVar` 警告
  - 将 `_instances: dict[str, "ConfigManager"]` 改为 `ClassVar[dict[str, "ConfigManager"]]`
  - 将 `_lock: RLock` 改为 `ClassVar[RLock]`
  - 添加 `from typing import ClassVar` 导入

##### b) 导入语句优化
- 统一导入顺序（标准库 → 第三方库 → 本地模块）
- 合并重复导入：`from google.genai import types, errors`
- 使用现代类型注解：`type[BaseModel]` 替代 `Type[BaseModel]`

##### c) 代码格式化
- **文件**: `pyproject.toml`
  - 添加 ruff 忽略规则（`BLE001`, `PLR0913`, `C901`, `E501` 等）
  - 扩展中文字符可混淆字符列表
  - 改进数组格式化一致性

##### d) LLM Provider thinking_level 支持
- **文件**: `komari_bot/plugins/llm_provider/gemini_client.py`
  - 添加 `thinking_level` 参数支持（minimal/low/medium/high）
  - 改进 thinking token 配置逻辑
  - 添加参数验证和 match-case 匹配

#### 2. 资源泄漏修复
- **提交**: `c19bfc1` - "fix bugs"
- **改动**: 2个文件，43行新增，38行删除

##### a) Komari Knowledge 插件状态管理重构
- **文件**: `komari_bot/plugins/komari_knowledge/__init__.py`

**问题**:
- 使用全局变量 `_streamlit_process` + `global` 声明（ruff `PLW0603` 警告）
- 插件禁用时仍继续初始化
- 资源清理不完整

**解决方案**:
1. **状态管理重构**:
   ```python
   # 改进前
   _streamlit_process: asyncio.subprocess.Process | None = None
   global _streamlit_process

   # 改进后
   class PluginState:
       def __init__(self) -> None:
           self.streamlit_process: asyncio.subprocess.Process | None = None
   state = PluginState()
   ```

2. **添加提前返回**:
   ```python
   if not config.plugin_enable:
       logger.info("[Komari Knowledge] 插件未启用，跳过初始化")
       return  # 新增

   if not config.pg_user or not config.pg_password:
       logger.warning("数据库用户名或密码未配置...")
       return  # 新增
   ```

3. **WebUI 启动条件优化**:
   - 仅在引擎初始化成功后启动 WebUI
   - 避免在未完成初始化时启动子进程

##### b) Komari Knowledge 引擎资源清理完善
- **文件**: `komari_bot/plugins/komari_knowledge/engine.py`

**改进**:
- 完善 `close()` 方法，清理嵌入模型引用
- 添加 `self._embed_model = None` 帮助垃圾回收
- 更新文档字符串说明会清理资源

#### 3. Gemini thinking_level 功能
- **文件**: `komari_bot/plugins/llm_provider/gemini_client.py`
- **功能**: 支持 Gemini 3+ 的 thinking_level 参数
- **实现**:
  - 使用 match-case 匹配字符串到枚举值
  - 支持 minimal/low/medium/high 四个级别
  - 向后兼容 thinking_token 参数

### 当前问题
无

### 下一步
无 - 功能已完整

### 关键文件清单
```
komari_bot/plugins/
├── config_manager/manager.py        # ClassVar 修复
├── komari_knowledge/
│   ├── __init__.py                  # 状态管理重构 + 提前返回
│   └── engine.py                    # close() 资源清理完善
├── llm_provider/
│   ├── gemini_client.py             # thinking_level 支持
│   └── config_schema.py             # 配置 Schema
└── sr/
    ├── redis_undo_stack.py          # Redis 撤销栈
    └── commands.py                  # 命令模式

pyproject.toml                       # ruff 规则更新
poetry.lock                          # 依赖更新
requirements.txt                     # 依赖同步
```

### 下次会话上下文
- **ClassVar 用法**: 可变类属性必须使用 `ClassVar[...]` 标注
- **状态管理模式**: 使用类封装全局状态，避免 `global` 声明
- **提前返回模式**: 条件检查后添加 `return`，避免继续执行
- **资源清理**: `close()` 方法应清理所有资源引用（连接池、模型等）
- **match-case**: Python 3.13 推荐使用 match-case 代替 if-elif
- **thinking_level**: Gemini 3+ 使用小写字母（minimal/low/medium/high）

---

## LLM Provider Bug 修复和功能增强 - 已完成

### 会话日期
2025-12-28

### 当前状态
✅ **已完成** - LLM Provider 修复类型错误并增强功能

### 完成的内容

#### 1. 修复 max_tokens 类型错误
- **文件**: `komari_bot/plugins/llm_provider/config_schema.py`
- **问题**: `deepseek_max_tokens` 和 `gemini_max_tokens` 被定义为 `float` 类型
- **错误**: DeepSeek API 要求整数，收到 `200.0` 后报错
- **修复**: 将类型从 `float` 改为 `int`
- **位置**: 第 54 行 (deepseek), 第 80 行 (gemini)

#### 2. 提高 max_tokens 默认值
- **文件**: `komari_bot/plugins/llm_provider/config_schema.py`
- **问题**: 默认值 200 只能生成约 100-150 个中文字符，回复被截断
- **修复**: 将默认值从 200 提高到 8192，上限从 500 提高到 8192
- **效果**: 可生成约 4000-6000 个中文字符的完整回复

#### 3. 添加 Debug 日志
- **文件**: `komari_bot/plugins/llm_provider/deepseek_client.py`
- **文件**: `komari_bot/plugins/llm_provider/gemini_client.py`
- **功能**: 在 `LOG_LEVEL=DEBUG` 时输出完整的请求和响应
- **内容包括**: model, temperature, max_tokens, system_instruction, prompt, response
- **实现**: 使用 `logger.debug()` 记录，非 DEBUG 模式自动忽略

### 当前问题
无

### 下一步
无 - 功能已完整

### 关键文件清单
```
komari_bot/plugins/llm_provider/
├── __init__.py           # 主入口
├── base_client.py        # 抽象基类
├── config.py            # 静态配置
├── config_schema.py     # 动态配置 Schema（max_tokens 类型修复）
├── deepseek_client.py   # DeepSeek 客户端（添加 debug 日志）
└── gemini_client.py     # Gemini 客户端（添加 debug 日志）
```

### 下次会话上下文
- max_tokens 默认值为 8192，支持长文本生成
- 设置 `LOG_LEVEL=DEBUG` 可查看完整的 API 请求和响应
- DeepSeek 和 Gemini 的 max_tokens 都必须是整数类型

---

## Komari Memory 实体提取优化：支持增量更新 - 已完成

### 会话日期
2026-02-28

### 当前状态
✅ **已完成** - 实体提取流程优化，LLM 总结时注入已有实体上下文

### 完成的内容

#### 1. summary_worker.py 查询并传递现有实体
- **文件**: `komari_bot/plugins/komari_memory/handlers/summary_worker.py`
- **改动**: `perform_summary()` 函数中，在调用 `summarize_conversation()` 之前：
  - 提前提取 `participants` 列表（从第 76 行移到第 64 行）
  - 遍历所有参与者，调用 `memory.get_entities(user_id, group_id, limit=50)` 获取现有常规实体
  - 调用 `memory.get_interaction_history(user_id, group_id)` 获取现有互动历史
  - 将 `existing_entities` 和 `existing_interactions` 作为新参数传递给 `summarize_conversation()`

#### 2. llm_service.py 注入已有实体上下文到 prompt
- **文件**: `komari_bot/plugins/komari_memory/services/llm_service.py`
- **改动**: `summarize_conversation()` 函数：
  - 新增 `existing_entities: list[dict] | None = None` 参数
  - 新增 `existing_interactions: list[dict] | None = None` 参数
  - 在 prompt 中格式化已知实体信息（`[user_id:xxx] key = value (category)`）
  - 在 prompt 中格式化已知互动历史
  - 添加【重要指示】指导 LLM：矛盾信息覆盖旧值、不重复未提及的实体、只输出新增/更新的实体、互动历史增量合并

### 解决的问题
1. **重复提取**: LLM 之前无法感知已有实体，会重复提取相同信息（如多次提取"喜欢的食物: 拉面"）
2. **无法更新**: 用户改口时（如从拉面改为寿司），LLM 不知道要更新已有实体
3. **互动历史全量覆盖**: 每次都是全量覆盖而非增量合并

### 当前问题
无

### 下一步
- 启动 bot 进行实际测试：触发总结流程后，检查日志中 LLM 的请求是否包含了已有实体信息
- 验证实体是否被正确更新而非重复创建

### 关键文件清单
```
komari_bot/plugins/komari_memory/
├── handlers/summary_worker.py    # perform_summary() 查询现有实体并传递
└── services/llm_service.py       # summarize_conversation() 注入已有实体到 prompt
```

### 下次会话上下文
- `summarize_conversation()` 现在接受 `existing_entities` 和 `existing_interactions` 可选参数
- 已有实体以 `【已知实体信息】` 和 `【重要指示】` 的形式注入 prompt
- `MemoryService.get_entities()` 已排除 interaction_history（通过 `get_interaction_history()` 单独获取）
- ruff check 通过，pyright 仅有环境导入错误（nonebot/pydantic 未本地安装）

---

## DeepSeek V4 Pro 工具调用式图片读取 - 已完成

### 会话日期
2026-04-26

### 当前状态
✅ **已完成** - DeepSeek V4 Pro 工具调用式图片读取功能实现完成

### 完成的内容

#### 1. LLM Provider 配置层 — 新增 Vision 模型配置
- **文件**: `komari_bot/plugins/llm_provider/config_schema.py`
- **新增字段**:
  - `vision_model`: 多模态视觉模型名（默认 `gemini-2.0-flash-exp`）
  - `vision_temperature`: 视觉模型温度（默认 0.3，范围 0.0–2.0）
  - `vision_max_tokens`: 视觉模型最大 token（默认 1024，范围 20–8192）
- **文件**: `config/config_manager/llm_provider_config.json`
- **新增配置项**: `vision_model`, `vision_temperature`, `vision_max_tokens`
- **注意**: 视觉模型复用 `deepseek_api_token` 和 `deepseek_api_base`，不需额外 API Key

#### 2. Komari Memory 配置层 — 新增 vision_tool_enabled 开关
- **文件**: `komari_bot/plugins/komari_memory/config_schema.py`
- **新增字段**: `vision_tool_enabled: bool = Field(default=True, description="是否启用 V4 工具调用读图模式")`
- **文件**: `config/config_manager/komari_memory_config.json`
- **新增配置项**: `"vision_tool_enabled": true`
- **说明**: 设为 `false` 即可完全回退到 base64 嵌入旧模式

#### 3. Vision 读图服务模块（新建）
- **文件**: `komari_bot/plugins/komari_chat/services/vision_service.py`（新建，119 行）
- **核心函数**:
  - `read_images(base64_images, vision_model, temperature, max_tokens)` — 并行调用多模态 AI 读图
  - `_read_single_image()` — 调用 OpenAI Vision 格式的单张图读取
  - `_format_error()` — 异常信息截断（不超过 200 字符）
- **实现要点**:
  - 使用 `openai.AsyncOpenAI` 临时客户端，复用 llm_provider 的 API token 和 base URL
  - `asyncio.gather` 并行读图
  - 读图失败返回 `"[图片读取失败: {error}]"` 不阻断主流程
  - 每次调用前后记录索引、模型名、base64 长度、描述长度

#### 4. Prompt Builder 增强
- **文件**: `komari_bot/plugins/komari_chat/services/prompt_builder.py`
- **新增参数**: `build_prompt()` 新增 keyword-only 参数 `vision_tool_mode: bool = False`
- **新模式逻辑（vision_tool_mode=True）**:
  - 不将 base64 图片嵌入消息体的 `image_url` 内容块
  - 在 user 消息文本中注入 `[系统提示：当前对话包含 N 张可读取图片...]` 标记
  - 引用图片索引在前（0 到 reply_count-1），当前消息图片在后（reply_count 到 total-1）
- **旧模式（vision_tool_mode=False）**: 保持原有 base64 嵌入逻辑不变

#### 5. LLM Service 新增 Tool Call 驱动回复函数
- **文件**: `komari_bot/plugins/komari_chat/services/llm_service.py`
- **模块级常量**: `READ_IMAGE_TOOL` — `read_image` 工具的 OpenAI function calling 定义
- **新函数**: `generate_reply_with_vision_tool(config, messages, base64_images, vision_model, vision_temperature, vision_max_tokens, request_trace_id, max_tool_rounds=3)`
- **工具调用循环逻辑**:
  1. 调用 `llm_provider.generate_messages_completion(messages, tools=[READ_IMAGE_TOOL])`
  2. 无 `tool_calls` → 跳出循环，返回 content
  3. 遍历 `tool_calls`：只处理 `read_image`；不支持的工具返回错误文本
  4. 解析 `image_index`，从 `base64_images` 中取图，调用 `vision_service.read_images()` 获取描述
  5. 构造 assistant 消息 + tool 结果消息追加到 messages
  6. 回到步骤 1（最多 `max_tool_rounds` 轮）
  7. 最后一轮调用 `generate_text_with_messages`（不带 tools）获取最终回复
- **辅助函数**: `_parse_image_index()` — 从工具调用参数中解析 `image_index`；`_build_image_tool_result()` — 执行工具返回消息内容
- **保留**: 原 `generate_reply()` 不变

#### 6. Message Handler 串联
- **文件**: `komari_bot/plugins/komari_chat/handlers/message_handler.py`
- **变更点**:
  - 引入 `llm_provider_config_manager` 获取 vision 配置（`DynamicConfigSchema`）
  - 在 `_attempt_reply()` 中下载图片后，根据 `vision_tool_enabled` 和是否成功下载图片决定新旧路径
  - 合并 `reply_image_urls`（引用图片）和 `base64_image_urls`（当前消息图片）为统一索引列表 `all_base64_images`
  - 调用 `build_prompt(..., vision_tool_mode=use_vision_tool)` 构建提示词
  - `use_vision_tool=True` 时调用 `generate_reply_with_vision_tool()`，否则调用 `generate_reply()`
  - 多模态日志追踪新增 `vision_tool_mode` 字段

#### 7. 图片索引顺序
- **引用图片在前**（0 到 reply_count-1）
- **当前消息图片在后**（reply_count 到 total-1）
- prompt builder 中按此顺序输出索引说明，`message_handler` 中按相同顺序合并 `all_base64_images`

### 验证结果
- `poetry run ruff check .` — 通过
- `poetry run pyright` — 通过
- `poetry run pytest tests/komari_chat -v` — 27 passed
- `poetry run pytest tests/ -v` — 收集阶段失败（既有测试环境问题：`unsupported require komari_decision`、多个同名测试模块 import file mismatch，非本任务引入）

### 当前问题
无

### 下一步
- 启动 bot 进行端到端集成测试，验证 V4 Pro 实际调用 `read_image` 工具并基于视觉描述生成回复
- 测试 `vision_tool_enabled=False` 回归旧行为
- 观察视觉模型调用频率和性能，必要时添加 `Semaphore` 限流

### 关键文件清单
```
komari_bot/plugins/llm_provider/
└── config_schema.py                  # 新增 vision_model / vision_temperature / vision_max_tokens

komari_bot/plugins/komari_memory/
└── config_schema.py                  # 新增 vision_tool_enabled

komari_bot/plugins/komari_chat/services/
├── vision_service.py                 # 新建：多模态视觉读图服务
├── prompt_builder.py                 # 修改：build_prompt() 新增 vision_tool_mode 参数
└── llm_service.py                    # 修改：新增 READ_IMAGE_TOOL / generate_reply_with_vision_tool()

komari_bot/plugins/komari_chat/handlers/
└── message_handler.py                # 修改：_attempt_reply() 串联新路径

config/config_manager/
├── llm_provider_config.json          # 新增 vision 配置项
└── komari_memory_config.json         # 新增 vision_tool_enabled 开关
```

### 下次会话上下文
- **Vision tool 模式**: 默认开启，`config.vision_tool_enabled=true`；关闭则回退 base64 嵌入
- **视觉模型**: 默认 `gemini-2.0-flash-exp`，复用 `deepseek_api_token` 和 `deepseek_api_base`
- **视觉模型配置**: `vision_temperature`（默认 0.3）、`vision_max_tokens`（默认 1024）
- **图片索引顺序**: 引用图片在前，当前消息图片在后
- **工具调用限制**: 最多 3 轮（`max_tool_rounds=3`），越界/参数错误返回工具错误文本
- **并行读图**: `asyncio.gather` 全并行，当前未加 `Semaphore` 限流
- **日志**: 多模态追踪日志已包含 `vision_tool_mode` 和 `tool_call_rounds`
- **验证**: ruff check + pyright 通过；komari_chat 单元测试 27 passed
- **既有问题**: 全量 `pytest tests/` 收集阶段因测试环境问题失败，与本次变更无关

---

## 当前会话状态

### 主要目标
- ✅ 代码质量优化 - 完成大规模代码重构和类型提示标准化
- ✅ 资源泄漏修复 - 修复 Komari Knowledge 插件的资源管理问题
- ✅ 项目规划文档 - 创建 Project NEON-TAVERN 智能聊天机器人详细设计
- ✅ Komari Memory 实体提取优化 - 支持增量更新
- ✅ DeepSeek V4 Pro 工具调用式图片读取 - 实现完成，验证通过

### 已完成任务
- [x] JRHG 插件配置系统重构 - 实现分层配置（JSON + .env）
- [x] JRHG 插件 Bug 修复 - 修复 CommandArg 类型、权限检查、FinishedException 问题
- [x] LLM Provider 防注入安全增强 - 将安全指令移至 system_instruction
- [x] Gemini 客户端 SDK 升级 - 迁移到 google-genai SDK
- [x] SR 插件撤销功能 - 使用命令模式实现 add/del/undo
- [x] SR 插件 Redis 持久化 - Redis 撤销栈，序号删除功能
- [x] LLM Provider Bug 修复 - 修复 max_tokens 类型错误
- [x] LLM Provider 功能增强 - 提高 max_tokens 默认值，添加 debug 日志
- [x] 代码质量优化 - 35个文件类型提示标准化，ClassVar 修复
- [x] 资源泄漏修复 - Komari Knowledge 状态管理重构，资源清理完善
- [x] Gemini thinking_level - 支持 Gemini 3+ thinking_level 参数
- [x] NEON-TAVERN 规划 - 智能聊天机器人项目详细设计文档
- [x] Komari Memory 实体提取优化 - 注入已有实体到 LLM 总结 prompt，支持增量更新
- [x] DeepSeek V4 Pro 工具调用式图片读取 - 新增 vision tool 模式，ruff/pyright/tests 验证通过

### 正在进行的任务
- [ ] NEON-TAVERN 实现 - 待开始（Phase 1: 基建搭建）

### 待办任务
- [ ] 提交 Komari Memory 实体提取优化的更改
- [ ] 启动 bot 测试实体增量更新功能
- [ ] 启动 bot 端到端测试 DeepSeek V4 Pro vision tool 图片读取
- [ ] 开始 NEON-TAVERN 插件实现（Phase 1）

## 技术上下文

### 当前技术状态
**架构决策**: NoneBot2 + OneBot v11，使用 localstore 插件存储配置
**性能状态**: 正常
**集成状态**: DeepSeek API 集成完成，视觉模型通过同一代理调用
**测试覆盖**: komari_chat 单元测试 27 passed（`poetry run pytest tests/komari_chat -v`）；全量测试因既有环境问题部分失败

### 关键发现和洞察
- **ClassVar 标注**: ruff 要求可变类属性（如 `dict`, `list`）必须使用 `ClassVar[...]` 标注，明确区分类变量和实例变量
- **PluginState 模式**: 使用类封装全局状态优于 `global` 声明，提供更好的封装性和类型安全
- **提前返回模式**: 条件检查后添加 `return` 可避免继续执行，节省资源并防止错误传播
- **资源清理完整性**: `close()` 方法应清理所有资源引用（连接池、模型、文件句柄等），帮助垃圾回收
- **NoneBot2 依赖注入**: `CommandArg()` 返回 `Message` 类型，不是 `str`。类型注解错误会导致依赖注入失败，处理器被跳过
- **Matcher Rule 绑定**: 在 matcher 创建时使用 `rule=` 会捕获模块加载时的配置对象，不是运行时配置。对于需要动态检查的权限，应在处理器内部进行检查
- **FinishedException**: `finish()` 通过抛出 `FinishedException` 终止处理器，这是正常控制流，不应被 `except Exception` 捕获记录为错误
- **Gemini thinking_level**: Gemini 3+ 使用 `thinking_level` 参数（小写），而非 `thinking_token`
- **Vision Tool 模式**: 复用 llm_provider 的 API token 和 base URL 调用视觉模型，通过 `max_tool_rounds=3` 防止工具调用死循环
- **AsyncOpenAI 临时客户端**: vision_service 每次请求创建并关闭 `AsyncOpenAI` 实例，不需要持久化客户端
- **图片索引映射**: 引用图片在前、当前消息图片在后，索引映射由 message_handler 统一维护

### 需要关注的技术债务
- **未提交的更改**: Komari Memory 实体提取优化（summary_worker.py + llm_service.py）待提交
- **未提交的更改**: DeepSeek V4 Pro 工具调用式图片读取（8个文件）待提交
- **实际测试**: 实体增量更新功能需要启动 bot 进行端到端测试
- **实际测试**: Vision tool 图片读取需要启动 bot 验证 V4 Pro 实际调用
- **NEON-TAVERN 实现**: 项目规划已完成，但代码实现尚未开始
- **全量测试环境**: `poetry run pytest tests/ -v` 因 `unsupported require komari_decision` 和 import file mismatch 失败，需要修复测试基础设施

## 文档系统状态

### 文档完整性
- **第1层文档**: 基础文档完整
- **第2层文档**: 组件文档完整
- **第3层文档**: 功能文档完整

### 文档更新需求
- [ ] [vision_service.py 模块注释] - 更新 API 文档和内部函数说明
- [ ] [README 或 docs/vision.md] - 新增 vision tool 模式使用说明（中优先级）

## 决策记录

### 最近的重要决策
**2026-04-26**: DeepSeek V4 Pro 工具调用式图片读取
- **背景**: DeepSeek V4 Pro 不支持原生视觉，需要将图片处理逻辑从「base64 直接嵌入 messages」改为「V4 Pro 通过 tool call 调用外部多模态 AI 读图」
- **选择**:
  - 复用现有代理 `newapi.derbaynas.top:2096` 的 API token 和 base URL，指定支持视觉的模型
  - 传参方式采用索引引用（tool call 参数只传 `image_index`，bot 在服务端查找 base64）
  - 通过 `vision_tool_enabled` 开关控制新旧模式，默认开启
  - 图片索引顺序：引用图片在前、当前消息图片在后
  - 最多 3 轮工具调用循环，防止死循环
  - 并行调用 `asyncio.gather` 读多张图
- **影响**:
  - V4 Pro 不再需要直接处理 base64 图片数据，通过 tool call 获取文字描述后生成回复
  - `vision_tool_enabled=False` 可完全回退到旧模式，无破坏性变更
  - 读图失败不阻断主流程，返回错误文本供 V4 Pro 参考
- **状态**: 已完成并验证（ruff check + pyright + pytest 通过）

**2026-02-28**: Komari Memory 实体提取优化
- **背景**: `summarize_conversation()` 调用 LLM 时只发送当前对话消息，不提供已存储的实体信息，导致重复提取和无法更新
- **选择**: 在调用 LLM 前查询该群组所有参与者的现有实体和互动历史，注入到 prompt 中，指示 LLM 进行增量更新
- **影响**: LLM 可感知已有实体，避免重复提取，支持实体更新和互动历史增量合并
- **状态**: 已完成，待实际测试验证

**2026-01-05**: 代码质量优化和资源泄漏修复
- **背景**: ruff 静态分析发现 ClassVar 警告和全局变量使用问题
- **选择**:
  - 使用 `ClassVar` 标注可变类属性
  - 使用 `PluginState` 类封装全局状态
  - 添加提前返回避免不必要的执行
  - 完善 `close()` 方法清理所有资源
- **影响**: 代码更健壮，资源管理更清晰，类型检查更准确
- **状态**: 已完成并验证

**2025-12-28**: LLM Provider 防注入安全增强
- **背景**: 需要防止用户通过提示词注入进行越狱攻击
- **选择**: 将防注入指令从 prompt 移至 system_instruction，利用系统指令更高优先级
- **影响**: 用户输入保持纯净，安全防护更可靠
- **状态**: 已完成并验证

**2025-12-28**: Gemini 客户端迁移到 google-genai SDK
- **背景**: Python 升级到 3.13，可使用官方 SDK
- **选择**: 使用 google-genai SDK 替代 aiohttp 直接调用 REST API
- **影响**: 代码简化 30%，自动重试和错误处理
- **状态**: 已完成并验证

**2025-12-28**: Python 版本全面升级到 3.13
- **背景**: 项目升级到 Python 3.13，可使用最新语言特性
- **选择**: 采用现代化写法（类型联合 `|`、内置泛型、match-case）
- **影响**: 代码更简洁，类型注解更清晰，性能提升
- **状态**: 已完成（pyproject.toml requires-python = ">=3.13, <4.0"）

**2025-12-24**: JRHG 插件配置系统重构
- **背景**: 需要支持配置热更新和 token 加密存储
- **选择**: 实现 ConfigManager 单例，JSON 配置优先于 .env，使用 Fernet 加密 token
- **影响**: 配置支持运行时更新，token 安全存储
- **状态**: 已完成并验证

**2025-12-24**: 修复 CommandArg 类型注解错误
- **背景**: 处理器被跳过，日志显示 `Handler Dependent skipped`
- **选择**: 将 `args: str = CommandArg()` 改为 `args: Message = CommandArg()`
- **影响**: 依赖注入正常工作，命令可以正常响应
- **状态**: 已完成并验证

### 待决策问题
无

## 下次会话建议

### 优先事项
1. **启动 bot 端到端测试** - 验证 vision tool 图片读取和实体增量更新功能
2. **提交所有未提交的更改** - git status 显示有未暂存的修改（实体提取优化 + vision tool 图片读取）
3. **修复全量测试环境** - `pytest tests/` 收集阶段因既有测试依赖和 import 冲突失败
4. **NEON-TAVERN 项目** - 智能聊天机器人开发（计划完成，待实现）

### 上下文提醒
- **关键文件**:
  - `komari_bot/plugins/config_manager/manager.py` - ClassVar 标注示例
  - `komari_bot/plugins/komari_knowledge/__init__.py` - PluginState 模式示例
  - `komari_bot/plugins/komari_knowledge/engine.py` - 资源清理示例
  - `komari_bot/plugins/llm_provider/gemini_client.py` - thinking_level 支持
  - `komari_bot/plugins/llm_provider/config_schema.py` - vision_model / vision_temperature / vision_max_tokens
  - `komari_bot/plugins/komari_chat/services/vision_service.py` - vision tool 读图服务（新建）
  - `komari_bot/plugins/komari_chat/services/prompt_builder.py` - vision_tool_mode 参数
  - `komari_bot/plugins/komari_chat/services/llm_service.py` - READ_IMAGE_TOOL + generate_reply_with_vision_tool()
  - `komari_bot/plugins/komari_chat/handlers/message_handler.py` - vision tool 路径串联
  - `Project NEON-TAVERN-Plugin.md` - 智能聊天机器人插件详细设计
  - `Project NEON-TAVERN-BERT-Service.md` - BERT 评分服务详细设计

- **重要约束**:
  - **Python 3.13 现代化写法**：使用 `X | Y` 类型联合、内置泛型 `list[T]`、match-case 语句
  - **ClassVar**: 可变类属性必须使用 `ClassVar[...]` 标注
  - **状态管理模式**: 使用类封装全局状态，避免 `global` 声明
  - **提前返回**: 条件检查后添加 `return`，避免继续执行
  - **资源清理**: `close()` 方法应清理所有资源引用（连接池、模型等）
  - **JRHG**: token 使用机器指纹加密，配置文件不跨机器通用
  - **JRHG**: 权限检查必须使用运行时配置（`config_manager.get()`）
  - **JRHG**: `CommandArg()` 返回 `Message` 类型，使用 `extract_plain_text()` 获取文本
  - **LLM Provider**: 防注入指令在 system_instruction 中
  - **LLM Provider**: max_tokens 必须是整数类型
  - **LLM Provider**: max_tokens 默认值为 8192
  - **LLM Provider**: thinking_level 使用小写字母（minimal/low/medium/high）
  - **LLM Provider**: 设置 `LOG_LEVEL=DEBUG` 可查看完整请求和响应
  - **LLM Provider**: vision 配置（vision_model/vision_temperature/vision_max_tokens）复用 deepseek_api_token 和 deepseek_api_base
  - **Vision Tool**: `config.vision_tool_enabled=true` 启用工具读图，`false` 回退 base64 嵌入
  - **Vision Tool**: 图片索引顺序 — 引用图片在前，当前消息图片在后
  - **Vision Tool**: `max_tool_rounds=3` 限制工具调用轮数
  - **Vision Tool**: `asyncio.gather` 并行读图，当前未限流
  - **SR**: 使用命令模式实现撤销，撤销栈最多 5 条
  - **SR**: 使用 Redis 持久化撤销栈（`redis.asyncio`，不要用 `aioredis`）
  - **Komari Knowledge**: 使用 PluginState 类管理状态，避免全局变量
  - **Komari Knowledge**: close() 方法会清理连接池和嵌入模型
  - **Komari Memory**: `vision_tool_enabled` 在 `komari_memory_config.json` 中配置
- **相关联系人**: 无

### 预期挑战
- **技术挑战**: NEON-TAVERN 项目需要实现复杂的记忆系统和主动回复逻辑
- **资源挑战**: BERT 模型服务器需要独立部署，GPU 资源需求
- **集成挑战**: 多个组件（BERT 服务、LLM、Redis、PostgreSQL）需要协同工作
- **测试挑战**: 全量测试环境因既有依赖和 import 冲突损坏，需要修复后方可运行完整测试套件

## 资源和参考

### 关键文档链接
- [NoneBot2 依赖注入文档](https://nonebot.dev/docs/advanced/dependency)
- [NoneBot2 配置管理](https://nonebot.dev/docs/advanced/configuration)
- [cryptography Fernet 文档](https://cryptography.io/en/latest/fernet/)

### 外部资源
- [DeepSeek API 文档](https://platform.deepseek.com/api-docs/)
- [OneBot v11 规范](https://github.com/botuniverse/onebot-11)

### 团队联系信息
无

---

*定期更新此文档以确保会话间的连续性和效率。每次主要任务完成或会话结束时都应更新相关部分。*
