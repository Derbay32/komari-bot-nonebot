# Agent Run Logger

`agent_run_logger` 是独立于 `llm_provider` 的请求级完整日志插件。业务插件负责定义任务边界并显式下传 `AgentRunCollector`，provider 只负责请求 LLM。

## 依赖与调用方

- 直接依赖：`config_manager`、`nonebot_plugin_apscheduler`、项目共享 PostgreSQL 配置。
- 调用方：`komari_chat`、`komari_memory`、`group_history_summary`、`komari_debug`。
- 管理挂载：`komari_management` 从本插件获取 API 注册函数和 reader。
- 禁止反向依赖：`llm_provider` 不得 import 本插件，也不得自行写持久 LLM 日志。

## 任务边界

- `chat_reply/chat_reply`：查询重写至 `ReplyResult`，包括视觉、搜索、画像、好感度和全部回复轮次；不包括 QQ 发送与后续副作用。
- `group_history_summary/group_history_summary`：一次 `execute_group_summary()`，包括规划、历史工具、最终总结和渲染；不包括 QQ 投递。
- `scheduled_summary/conversation_processing`：单个群 processing 快照的所有分块、fallback、画像 Agent、重试、入库和 ACK。
- `scheduled_summary/interaction_processing`：单个用户互动 processing 快照的分块归并、重试、入库和 ACK。
- `scheduled_summary/forgetting_conversation` 与 `forgetting_interaction`：单条记忆的全部模糊化重试、向量化和最终 CAS。

没有执行 LLM 的空定时任务或纯缓存命中通过 `skip_if_no_calls` 跳过落盘。成功、异常和取消都调用同一个幂等结束接口，同一 collector 最多产生一个物理 JSONL 行。

## 存储契约

- 权威正文：`logs/agent_run_logger/YYYY-MM-DD.jsonl`，schema version 3，目录 `0700`、文件和锁文件 `0600`。
- 查询索引：PostgreSQL `UNLOGGED komari_agent_run_log_index`，只含定位与聚合元数据，可从 JSONL 完整重建。
- 顺序：文件锁内追加并获得 byte offset/length，释放锁，再写 PG；索引失败不回滚 JSONL。
- 维护：启动及每 5 分钟对账；每日本地时间 04:00 清理。默认只保留当前日志日。
- 配置：`log_enabled=true`、`retention_days=1`。旧 `llm_provider.llm_log_*` 字段不兼容、不迁移，启动后从 PG 配置 JSONB 中物理删除。

## 内容边界

本插件是受权限保护的完整调试日志，不做普通正文脱敏。用户消息、历史、画像、网页结果、prompt、回复、reasoning 与工具文本按原文保存；仅过滤 API key、Authorization、Cookie、密码等显式凭据，并摘要化图片 URL、data URL、base64 和二进制。

管理 API 使用 `llm_logs:read`：

- `GET /api/agent-run-logs/v1/runs`
- `GET /api/agent-run-logs/v1/runs/{run_id}`

`.debug` 报告继续使用独立的二次脱敏格式，禁止把完整日志对象交给消息投递代码。
