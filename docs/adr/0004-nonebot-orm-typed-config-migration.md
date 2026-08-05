# 数据库层移交 nonebot-plugin-orm，配置存储改强类型表

v2.0.0 起，数据库连接池、engine/session 生命周期与 Schema 迁移全部移交 nonebot-plugin-orm（SQLAlchemy 2.x + Alembic），删除自研的 `common/postgres.py` 共享池注册表（含 12 个调用点）及 `database_config.py` 的 PG 配置部分。核心理由：自研连接管理是在重复 SQLAlchemy 已解决十几年的问题，本项目不应承载这一模块；同时配置项（Pydantic schema）频繁变动却缺乏版本化迁移，弃用字段只能靠启动时幂等清理语句和一次性脚本，是真实技术债。

## Considered Options

- 独立 Alembic + 纯 raw SQL 迁移：迁移收益相同，但连接池与生命周期仍需自研维护，不符合"不自研基础设施"的目标，否决。
- 配置维持通用 JSONB KV 表 + 手写 Alembic 数据迁移：autogenerate 对 KV 表完全失效（改字段不变表结构），主痛点得不到工具红利，否决。

## 决策要点

- 配置表（`komari_plugin_configs`、`komari_prompt_configs`）与简单关系表（封禁、绑定、好感度、提案、公告、场景）改为 SQLModel 强类型表（Pydantic 与 SQLAlchemy 单一定义来源，避免双定义漂移）；字段新增/弃用由 autogenerate 生成迁移；重复的 version 字段随之删除。仅为确实需要扩展的配置项保留 JSONB 列。
- 含向量、触发器、`UNLOGGED`、advisory lock 等特殊 DDL 的表（记忆四层、知识库、帮助、`komari_agent_run_log_index`、`komari_search_index_versions`）维持 Core / raw SQL 查询路径，但其 DDL 同样收编进 Alembic 版本链（`op.execute` 手写 revision）。分界线以表为单位，不以插件为单位。
- v2.0.0 的 Alembic revision 只建新表，旧 JSONB 表保留；存量配置值由独立的离线迁移脚本（读旧 JSONB → upsert 新表 → 输出决算报告）一次性搬运，主代码不含任何兼容性迁移；旧表的 `DROP` 由后续版本的 autogenerate revision 执行。
- 容器 entrypoint 在启动 Gunicorn 前自动执行 `alembic upgrade head`，失败则 fail fast；部署层本就强制单 worker，无迁移竞态。

## Consequences

- 日常工作流变为：改 SQLModel 类 → autogenerate revision → 发版 → 容器启动自动迁移，全程零人工干预数据库。
- `pg_pool_process_budget` 等自研连接预算防护随单 engine 架构自然消解，池语义改由 SQLAlchemy 池参数表达。
- SQLModel 引入 Pydantic v2 兼容窗口的版本锁定约束，依赖升级时需留意。