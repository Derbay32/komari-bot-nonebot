# 数据库迁移

数据库结构由 `nonebot-plugin-orm` 与 Alembic 版本链统一管理。应用运行时不再创建或修改表结构。

## 全新数据库

配置 `SQLALCHEMY_DATABASE_URL` 与实际使用的 `EMBEDDING_DIMENSION` 后执行：

```bash
poetry run python -m komari_bot.db.orm_bootstrap upgrade head
```

容器会在 Gunicorn 启动前自动执行同一命令；迁移失败时容器拒绝启动。

## 已有数据库升级到基线

`0001` 是现有 v2.0.0 前数据库结构的完整基线。已有数据库已经包含这些对象，不得直接执行 `0001` 的建表操作；完成备份并确认当前结构来自迁移前版本后，将数据库标记到基线：

```bash
poetry run python -m komari_bot.db.orm_bootstrap stamp 0001
poetry run python -m komari_bot.db.orm_bootstrap upgrade head
```

`stamp` 只写入 Alembic 版本号，不执行 DDL，也不会校正结构差异。结构不符合基线的数据库必须先按原版本升级流程修复，再执行 `stamp`。

## 开发校验

```bash
poetry run python -m komari_bot.db.orm_bootstrap check
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

## 回复履约模型停机切换发布程序（0010 / 0011，一次性）

适用于从 0009 或更早版本升级到包含 0010 / 0011 的版本。0010 把旧回复
履约宽表数据回填进新父子表并保留旧表；0011 完成原子切换、删除旧表并把
`komari_chat_config` 的六个 `reply_commit_*` 列一对一改名为
`reply_fulfillment_*`（值原样保留），同时新增
`reply_fulfillment_retry_max_seconds`（默认 3600 秒）。

**0011 不可逆：切换完成后不存在任何 downgrade 路径，唯一回滚手段是恢复
升级前备份。**

### 前置条件

1. 完成数据库全量备份。检查：备份文件可用于恢复。
2. 停止全部旧版 Bot 进程。检查：无进程继续写入旧履约表。
3. 确认新版本代码已部署到目标主机或镜像。检查：`poetry run python -m komari_bot.db.orm_bootstrap heads` 输出 `0011`。

### 发布步骤

1. 执行 `poetry run python -m komari_bot.db.orm_bootstrap upgrade head`。
   检查：命令退出码为 0，`alembic_version` 为 `0011`。
2. 若命令失败且错误含 `ambiguous_failed_count=`：0010 预检发现超出幂等
   证据窗口的旧 FAILED 行，整个升级已自动回滚，旧版可继续运行。处置：
   按 `minimum_fulfillment_id=` 给出的最小身份人工核查该行，决定删除或
   修正后回到步骤 1。错误输出只含数量与最小身份，不含回复正文。
3. 若命令失败且错误含 `missing_backfill_count=` 或
   `commitment_mismatch_count=`：0010 之后仍有旧表写入（旧进程未停净），
   升级已自动回滚。处置：确认全部旧进程停止后回到步骤 1。
4. 验证切换结果：旧表 `komari_chat_reply_commit_outbox` 已删除。
   检查：`SELECT to_regclass('komari_chat_reply_commit_outbox')` 返回空。
5. 抽查配置保值：六个改名列的值与升级前一致。
   检查：`SELECT reply_fulfillment_worker_interval_seconds, reply_fulfillment_retry_max_seconds FROM komari_chat_config WHERE id = 1`。
6. 启动新版 Bot。检查：启动日志无迁移错误，管理 API 履约列表可读取。

### 切换后运维

- 待确认送达的历史回复不会被自动重发；由人工在管理 API 逐条确认送达或
  标记未送达。
- 耗尽重试次数的承诺会触发私聊告警；处置后通过管理 API 对指定承诺续跑，
  再次耗尽可能产生新一代告警。

### 失败回滚矩阵

| 故障点 | 结果 | 回滚动作 |
| --- | --- | --- |
| 0010 预检或回填失败 | 事务整体回滚，版本停留原样 | 旧版继续运行，处置后重试 |
| 0011 切换校验失败 | 事务整体回滚，版本停留 0010，旧表仍在 | 可 `downgrade 0009` 或修复后重试 |
| 0011 完成后任何故障 | 无 downgrade 路径 | 停新版，恢复升级前备份，启动旧版 |
