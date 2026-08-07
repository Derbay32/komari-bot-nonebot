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

## 落地状态（2026-08-07，v2.0.0）

**状态：已完成**（Project spec #26 tickets 01–12 全部关闭）。

| Ticket | 内容 | 状态 |
|--------|------|------|
| 01 | 基线迁移 0001（全量表结构）与迁移引导 `orm_bootstrap` | 已完成 |
| 02 | `SQLALCHEMY_DATABASE_URL` 连接解析（`orm_config.py`）与旧 `PG_*` 配置下线 | 已完成 |
| 03 | 动态配置强类型表（迁移 0002，14 张 `komari_<插件>_config`）+ `typed_config.py` | 已完成 |
| 04 | 配置存储 CAS/revision 轮询刷新与跨进程一致性 | 已完成 |
| 05 | Prompt 强类型表（迁移 0003，3 张 `komari_prompt_*`）+ `prompt_storage.py` 重写 | 已完成 |
| 06 | 业务关系表 ORM 化（user_ban/character_binding/user_data/komari_custom/komari_management/komari_decision 的 `orm_models.py`） | 已完成 |
| 07 | 存量 legacy 表（`komari_plugin_configs` / `komari_prompt_configs`）保留与运行时清理策略 | 已完成 |
| 08 | 部署链路：`docker/prestart.sh` 自动 `upgrade head` + CI `migration-check.yml` | 已完成 |
| 09 | 离线迁移脚本体系（`migrate_legacy_config_to_typed_tables.py` 等 9 个脚本） | 已完成 |
| 10 | raw SQL 插件连接切换（memory/knowledge/help/agent_run_logger → `orm_connection.py` 共享引擎适配） | 已完成 |
| 11 | 环境变量与文档收尾（删除 `PG_*`、清理失效配置行） | 已完成 |
| 12 | 文档与 v2.0.0 发布说明（AGENTS.md / 插件 README / CHANGELOG） | 已完成 |

### 最终落地与决策原文的差异

- 基线 0001 实际建 **26 张表**（含保留的 `komari_plugin_configs` / `komari_prompt_configs` 两张 legacy 配置表，运行时不读写）；加上 0002 的 14 张配置表与 0003 的 3 张 Prompt 表，版本链共 43 张表。
- 跨进程配置变更通知最终采用「应用事件循环亚秒级轮询 `revision`」而非 asyncpg LISTEN/NOTIFY（0002 迁移注释同步说明）；Prompt 变更传播依赖 `PromptTemplateLoader` 1 秒陈限上限 + 本进程写入即时失效回调。
- `komari_memory/database/init_orm.sql` 与 `komari_knowledge/init_db.sql` 保留为 fail-closed 提示/运维参考文件，不再作为运行时 DDL 来源；手工建表只能使用 `scripts/render_memory_schema.py` 离线渲染。
- 旧脚本 `split_komari_memory_entity_tables.py`、`extract_komari_memory_profile_columns.py`、`drop_legacy_user_attributes_table.py` 已删除（其目标结构已由基线 0001 一次性覆盖）。
- 管理 API 配置来源描述为 `postgresql:<强类型表名>`（如 `postgresql:komari_prompt_komari_chat:komari_chat`），不再使用 `komari_plugin_configs` / `komari_prompt_configs`。