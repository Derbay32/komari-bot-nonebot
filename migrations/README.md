# 数据库迁移

数据库结构由 `nonebot-plugin-orm` 与 Alembic 版本链统一管理。应用运行时不再创建或修改表结构。

## 全新数据库

配置 `SQLALCHEMY_DATABASE_URL` 与实际使用的 `EMBEDDING_DIMENSION` 后执行：

```bash
poetry run python -m komari_bot.common.orm_bootstrap upgrade head
```

容器会在 Gunicorn 启动前自动执行同一命令；迁移失败时容器拒绝启动。

## 已有数据库升级到基线

`0001` 是现有 v2.0.0 前数据库结构的完整基线。已有数据库已经包含这些对象，不得直接执行 `0001` 的建表操作；完成备份并确认当前结构来自迁移前版本后，将数据库标记到基线：

```bash
poetry run python -m komari_bot.common.orm_bootstrap stamp 0001
poetry run python -m komari_bot.common.orm_bootstrap upgrade head
```

`stamp` 只写入 Alembic 版本号，不执行 DDL，也不会校正结构差异。结构不符合基线的数据库必须先按原版本升级流程修复，再执行 `stamp`。

## 开发校验

```bash
poetry run python -m komari_bot.common.orm_bootstrap check
```

该命令用于检查 SQLAlchemy/SQLModel 元数据与版本链是否同步。特殊 raw SQL 对象由手写 revision 管理。

## v2.0.0 存量配置搬运（一次性离线迁移）

`0002` / `0003` 只建新强类型表，旧 JSONB KV 表（`komari_plugin_configs`、
`komari_prompt_configs`）保留；存量配置值由独立离线脚本一次性搬运，脚本不
删除旧表数据（旧表 DROP 由后续 autogenerate revision 负责）：

```bash
poetry run python scripts/migrate_legacy_config_to_typed_tables.py \
    --dsn postgresql://user:pass@host:5432/komari_bot
# 或使用环境变量（两种形式均可）：
SQLALCHEMY_DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/komari_bot \
    poetry run python scripts/migrate_legacy_config_to_typed_tables.py
```

要点：

- 对新表执行 `INSERT ... ON CONFLICT (id) DO UPDATE`，可重复执行且幂等；
  `revision` / `updated_at` 继承 legacy 行（缺失时分别取 1 / 当前 UTC 时间）。
- 只覆盖旧 JSONB 中实际存在的键；缺失列在已播种的新表行上保持原值，空表
  上按列类型回退中性默认值（bool→false、int→0、float→0.0、str→''、JSONB→{}）。
- 脚本不读取仓库 `.env`，不依赖应用启动播种；建议在应用首次启动前执行，
  执行后按 stdout 决算报告核对「已迁移键 / 丢弃弃用键 / 落回默认值列」。
- 脚本独立实现（不 import 运行时代码、不新增第三方依赖），键→列映射静态
  写死在脚本内并与本目录迁移版本逐列一致；如后续迁移新增列，需同步更新
  脚本声明后再执行。
