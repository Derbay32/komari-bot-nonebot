# Config Manager 插件

通用配置管理插件，负责把各业务插件的动态配置存储到 PostgreSQL。

## 功能特性

- **PostgreSQL 持久化**：每个配置资源一张强类型单行表 `komari_<插件>_config`（SQLModel，主键 `id=1`、CAS 修订号 `revision`、写入时间 `updated_at`），表结构由 Alembic 迁移链管理（`migrations/versions/0002` 建 14 张表），运行时无 DDL。
- **dotenv 初始化**：PG 中缺少配置时，从 NoneBot 已加载的 `.env` / schema 默认值初始化。
- **运行时更新**：`update_field()` 会校验字段并写回 PostgreSQL（revision CAS，冲突重载重试）。
- **跨进程刷新**：应用事件循环上轮询各表 `revision`（亚秒级）检测外部变更；已移除 asyncpg LISTEN/NOTIFY。
- **资源级并发**：工厂注册表只锁定实例创建，各插件管理器使用独立操作锁。
- **完整生命周期**：同步兼容桥使用专用事件循环线程，应用关闭时会回收一次性引擎连接、线程和事件循环；共享引擎归 nonebot-plugin-orm 托管，不 dispose。
- **通用设计**：接受任何 SQLModel/Pydantic Schema（`TypedConfigModel` 基类见 `komari_bot/config/typed_config.py`）。

## 配置表

配置存储源是各插件 `config_schema.py` 中的 SQLModel 强类型表（如 `komari_memory_config` / `komari_knowledge_config`），由 Alembic 迁移 0002 建表：

- 单行使用，主键 `id` 恒为 1；
- `revision` 为跨进程 CAS 修订号，写操作原子自增；
- `updated_at` 为最后写入时间（带时区），由存储层显式赋值；
- 列表/字典/嵌套配置使用 JSONB 列。

旧版 JSONB KV 表 `komari_plugin_configs` 在 v2.0.0 保留（仅存量数据，不再读写），`DROP` 由后续版本的 autogenerate revision 执行。数据库连接不进入配置表：连接唯一权威是 nonebot-plugin-orm 的 `SQLALCHEMY_DATABASE_URL`，Redis 引导配置来自 `.env` / 进程环境变量（`komari_bot/config/redis_config.py`）。

## 使用方法

```python
from nonebot.plugin import require
from pydantic import BaseModel, Field

config_manager_plugin = require("config_manager")

class MyConfig(BaseModel):
    version: str = Field(default="1.0")
    plugin_enable: bool = Field(default=True)
    timeout: int = Field(default=30)

config_manager = config_manager_plugin.get_config_manager("my_plugin", MyConfig)

# 模块加载阶段的同步用法
config = config_manager.get()

# 异步处理器与 FastAPI 路由必须使用异步接口
config = await config_manager.get_async()
await config_manager.update_field_async("timeout", 60)
config = await config_manager.reload_async()
```

对列表、映射等需要“读取当前值后再修改”的字段，必须使用原子变换接口。变换函数可能在跨进程 CAS 冲突后基于最新值重新执行，因此只能做纯内存变换：

```python
def append_unique(current: list[str]) -> list[str]:
    return current if "new-item" in current else [*current, "new-item"]

await config_manager.mutate_field_async("items", append_unique)
```

## API 参考

### `get_config_manager(plugin_name, config_schema)`

从唯一工厂注册表获取配置管理器实例；相同插件名返回同一实例，重复注册不同 Schema 会被拒绝。注册表锁只保护实例创建，不会串行化不同插件的配置读写。

### `initialize()` / `get()`

从 PostgreSQL 读取配置；如果 PG 中没有对应插件配置，则从 dotenv / schema 默认值初始化并写入 PG。
同步接口只用于模块加载等非异步阶段；事件处理器应使用 `initialize_async()` / `get_async()`。

### `update_field(field_name, value)`

更新单个字段，使用 Pydantic schema 重新校验后写入 PG。
写入采用 JSONB 字段补丁和递增修订号，检测到并发更新时会重载并重试；异步代码使用
`update_field_async()`，避免阻塞事件循环。

### `mutate_field_async(field_name, mutator)`

从 PostgreSQL 最新修订读取字段，应用纯变换后使用修订号 CAS 提交。遇到冲突会重新读取并重跑变换；字段值未变化时不创建无意义的新修订。适用于列表追加、删除和映射局部修改。

### `reload()`

从 PostgreSQL 重新读取配置。`reload_from_json()` 仅作为临时兼容别名存在，内部同样调用 `reload()`。
异步代码使用 `reload_async()`。

### `config_source`

返回配置来源描述，例如：`postgresql:komari_memory_config`。

## 旧 JSON 迁移

旧版 `config/config_manager/*_config.json` 不再被运行时读取。需要迁移时执行：

```bash
poetry run python scripts/migrate_json_config_to_pg.py --config-dir config/config_manager
```

脚本会把 JSON 内容写入 legacy JSONB 表 `komari_plugin_configs`（v2.0.0 起仅作为存量中转，新表数据由 `scripts/migrate_legacy_config_to_typed_tables.py` 统一搬运），不会删除本地旧 JSON 文件。
