# Komari Memory

本插件是 Hindsight 共享长期记忆的调用侧。
本插件不维护对话摘要、用户画像、互动历史或忘却任务。

## 代码边界

保留文件：

- `__init__.py`
- `config_schema.py`
- `services/config_interface.py`
- `services/hindsight_service.py`
- `services/hindsight_retain.py`

顶层只暴露：

- `KomariMemoryConfigSchema`
- `HindsightMemoryService`
- `HindsightRecallSession`
- `HindsightRetainRequest`
- `HindsightUnavailableError`
- `get_hindsight_memory`

TSK-197 已删除旧 agent、core、database、handlers、repositories、api、
prompt_schema 与旧服务文件。
旧总结、画像、互动与忘却 worker 不再注册。

## 生命周期

1. 插件启动时创建一个稳定 caller。
2. 启动探测最多执行一次。
3. 配置读取失败或探测失败时记录安全诊断。
4. 探测失败不影响其他插件启动。
5. 插件关闭时只关闭自有客户端与在途会话。
6. 插件关闭时不关闭 Redis 与共享 ORM。

## 配置

配置表为 `komari_memory_config`。
正文只有 5 个字段：

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `hindsight_base_url` | `""` | Hindsight 服务基础地址 |
| `hindsight_api_key` | `""` | Hindsight 服务凭据 |
| `hindsight_bank_id` | `""` | 共享记忆库标识 |
| `hindsight_request_timeout_seconds` | `10.0` | 单次请求超时 |
| `hindsight_recovery_interval_seconds` | `30.0` | 失败后恢复探测最小间隔 |

`hindsight_api_key` 标记为秘密字段。
共享记忆是聊天的必需依赖，不提供停用开关。
`startup_required=False` 保留。

## 召回

1. 每次回复任务打开一个会话。
2. 会话开始时读取一次设置快照。
3. 会话复用一个官方客户端。
4. 聊天只执行一次自动召回。
5. 模型工具 `recall_memory` 复用同一会话。
6. 召回失败时停止本次回复。
7. 空召回视为成功。
8. 召回内容经 `UntrustedContext(source_type="memory")` 进入非 system 上下文。
9. 不按来源群过滤已接收记忆。
10. 工具参数不能选择 bank、地址或凭据。

## 写入

1. 只有 `DELIVERED` 父履约允许提交角色回复记忆。
2. 回复准备阶段冻结请求。
3. 请求的 `source_id` 为 `reply:<fulfillment_id>`。
4. 执行器按冻结的目标指纹打开会话。
5. 地址或 bank 变化时拒绝改投。
6. 接收确认表示 Hindsight 已接收。
7. 接收确认不表示提取或索引已完成。
8. 记忆失败不撤销已确认送达事实。

## 正式来源存储

旧四层表仍保留在数据库中：

- `komari_memory_conversations`
- `komari_memory_user_profile`
- `komari_memory_interaction_history`
- 旧实体与兼容历史表

运行时不读取这些表。
迁移 `0030` 不删除这些表、向量、来源索引或旧任务表。
运维在停写窗口内用 `scripts/migrate_memory_to_hindsight.py` 一次性导出。
导出完成后再由运维处置来源存储。
代码退役不表示生产数据已迁移或删除。

## 验证

Hindsight 行为使用 SDK 边界替身验证。
近期上下文上限使用真实本地 Redis 验证。
迁移链使用真实 PostgreSQL 一次性库验证。
验收命令：

```bash
poetry run pytest tests/komari_memory tests/komari_context
poetry run pytest tests/db/test_migration_0030_hindsight_memory_retirement.py
poetry run python -m komari_bot.db.orm_bootstrap check
```
