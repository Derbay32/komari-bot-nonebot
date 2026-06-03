# Config Manager 插件

通用配置管理插件，负责把各业务插件的动态配置存储到 PostgreSQL。

## 功能特性

- **PostgreSQL 持久化**：配置存储在 `komari_plugin_configs` 表。
- **dotenv 初始化**：PG 中缺少配置时，从 NoneBot 已加载的 `.env` / schema 默认值初始化。
- **运行时更新**：`update_field()` 会校验字段并写回 PostgreSQL。
- **线程安全**：使用锁机制保证多线程环境下的配置访问安全。
- **通用设计**：接受任何 Pydantic `BaseModel` 子类作为配置 Schema。

## 配置表

```sql
CREATE TABLE IF NOT EXISTS komari_plugin_configs (
    plugin_name VARCHAR(128) PRIMARY KEY,
    schema_name VARCHAR(128) NOT NULL,
    config_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    version VARCHAR(32) NOT NULL DEFAULT '1.0',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

`database` 连接配置不写入该表；PG / Redis 引导配置来自 `.env`、`.env.dev`、`.env.prod` 或进程环境变量。

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
config = config_manager.get()
config_manager.update_field("timeout", 60)
config = config_manager.reload()
```

## API 参考

### `get_config_manager(plugin_name, config_schema)`

获取配置管理器单例实例。

### `initialize()` / `get()`

从 PostgreSQL 读取配置；如果 PG 中没有对应插件配置，则从 dotenv / schema 默认值初始化并写入 PG。

### `update_field(field_name, value)`

更新单个字段，使用 Pydantic schema 重新校验后写入 PG。

### `reload()`

从 PostgreSQL 重新读取配置。`reload_from_json()` 仅作为临时兼容别名存在，内部同样调用 `reload()`。

### `config_source`

返回配置来源描述，例如：`postgres:komari_plugin_configs/komari_memory`。

## 旧 JSON 迁移

旧版 `config/config_manager/*_config.json` 不再被运行时读取。需要迁移时执行：

```bash
poetry run python scripts/migrate_json_config_to_pg.py --config-dir config/config_manager
```

脚本会跳过 `database_config.json`，且不会删除本地旧 JSON 文件。
