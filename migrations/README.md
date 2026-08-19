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

## 聊天回复最终协议协调式破坏性升级（0012–0015，一次性）

适用于从 0011 或更早版本升级到包含 0012–0015 的版本（TSK-188～194）。本升级把
聊天回复的输出协议、回复 Agent 执行预算、工具调用约束模式与图片理解模式收敛为
`komari_chat` 配置的唯一新契约：**不提供双读、双写、旧字段别名或运行时兼容回退**，
升级后旧字段从 Schema、管理 API 与数据库中一并消失。

### 变更内容

- **0012** `komari_prompt_komari_chat`：删除旧最终输出协议列 `output_instruction`，
  其自定义内容永久丢弃、绝不并入任何新字段；新增 7 个独立行为列
  （`tool_call_instruction` / `image_read_instruction` /
  `profile_read_instruction` / `search_web_instruction` /
  `fetch_page_instruction` / `delegated_vision_instruction` /
  `vision_description_prompt`）。**0012 不可逆**：downgrade 明确拒绝，不承诺还原
  已删除的 `output_instruction` 列或其自定义内容。
- **0013** `komari_chat_config`：新增回复 Agent 执行预算
  `agent_max_rounds`（默认 10）/ `agent_max_tool_calls_per_round`（默认 4）/
  `agent_max_total_tool_calls`（默认 20），并声明跨字段 CHECK。
- **0014** `komari_chat_config`：新增工具调用约束模式 `agent_tool_call_mode`
  （非空默认 `required`，取值 `required` / `prompt_guided`）。
- **0015** `komari_chat_config`：新增图片理解模式 `image_understanding_mode`
  （默认 `delegated`，取值 `native` / `delegated`）与 8 项 `vision_image_download_*`
  下载预算；按旧图片开关 bool 双分支精确迁移（开→`delegated`、关→`native`）并把
  预算原样复制到 chat，随后从 `komari_memory_config` 删除旧开关与 8 项预算列
  （不留 alias / 双读 / fallback）。

新列的正文与初始数据由统一版本化播种（`seed_bootstrap`）写入，0012–0015 迁移只
建列、不承载 Prompt 内容；升级后未播种的应用无法通过冷启动校验（fail fast）。

### 升级步骤（全新库与旧库通用）

全新数据库与 0011 之前旧库的升级均从 0011 处继续：

1. 备份：`pg_dump` 全量备份数据库。备份是唯一保险，0012 起无完整降级路径。
2. 升级到 head：
   ```bash
   poetry run python -m komari_bot.db.orm_bootstrap upgrade head
   ```
3. 播种初始数据（**升级后必须执行**；容器 prestart 已按
   `upgrade head` → `seed_bootstrap` 顺序自动执行）：
   ```bash
   poetry run python -m komari_bot.db.seed_bootstrap
   ```
   播种只新建缺失场景与三个 Prompt 资源单行、只补齐空字段，绝不覆盖非空自定义值、
   不删除管理员场景；旧 `output_instruction` 自定义内容不并入任何字段。
4. 校验模型元数据零漂移：
   ```bash
   poetry run python -m komari_bot.db.orm_bootstrap check
   ```

### 验收命令

真实 PostgreSQL 门控下的协调式升级验收（每个用例在从门控库派生的一次性隔离库内
重建迁移链，结束即 DROP，门控用户需要 CREATEDB 权限）：

```bash
SQLALCHEMY_DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/komari_bot_test \
KOMARI_TEST_POSTGRES_URL=postgresql+asyncpg://user:pass@host:5432/komari_bot_test \
  poetry run pytest \
    tests/db/test_tsk197_fresh_database_gate.py \
    tests/db/test_tsk197_legacy_upgrade_gate.py
```

### 失败回滚矩阵

| 故障点 | 结果 | 回滚动作 |
| --- | --- | --- |
| 0012–0015 任一执行失败 | 该 revision 事务回滚，版本停留原样 | 修复后重试升级 |
| 0012 成功完成后退回 | 0012 downgrade 不可逆，无降级路径 | 停新版，恢复升级前备份，启动旧版 |
| 0013–0015 成功完成后退回 | 可对称 `downgrade` 到 0012，但回退后新配置列消失 | 恢复备份或继续升级 |
