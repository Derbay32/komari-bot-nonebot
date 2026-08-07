# 解散 common 包，按领域分层命名顶层共享包

`komari_bot/common/` 已膨胀为 25 个模块的扁平堆场，名字本身不承载任何归属信息。我们将其整体解散，按「这是什么的边界」重新归入 7 个顶层包：`core/`（运行时地基）、`db/`（PostgreSQL 边界）、`llm/`（LLM 边界共享件）、`config/`（配置体系基座）、`management/`（管理 API 边界）、`memory/`（记忆领域纯逻辑）、`onebot/`（平台适配辅助）。测试目录按被测对象镜像归位，`common/` 目录与包级 import 副作用（`nonebot_compat` 隐式安装）一并删除，不留兼容 shim。

## Considered Options

- **保留 common，内部再分子目录**：被拒绝——治标不治本，`common/` 作为「不知道放哪就扔这」的语义仍在，目录会继续长大。
- **采用 `core/ services/ repositories/` 字面三层**：被拒绝——本项目中 Service 与 Repository 是插件内部的分层概念（`MemoryService`、`ConversationRepository` 等），顶层同名会造成词汇冲突；且 common 中的模块全是协议类型、连接适配、schema 工具等支撑件，没有一个是业务意义上的 Service 或 Repository，硬塞会名不副实。最终采纳的是其「规整分层命名」的风格，而非字面包名。

## Consequences

- 全仓库约 160 个文件的 import 被机械改写；`python -m` 入口从 `komari_bot.common.orm_bootstrap` 变为 `komari_bot.db.orm_bootstrap`（prestart.sh、CI、文档同步更新）。
- `nonebot_compat` 补丁只剩显式安装点（`docker/bot.py`、`tests/conftest.py`、`db/orm_bootstrap.py`），import 包即装补丁的隐式行为不再存在。
- 新共享模块的归属规则变为「按边界选桶」，新增模块时不再有 common 这个兜底选项。
