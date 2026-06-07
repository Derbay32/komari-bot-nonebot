# 用户数据插件

为 NoneBot2 提供用户属性存储与当前好感度服务。当前版本已移除旧版每日随机好感、累计好感、历史查询与 `.jrhg` 指令；好感度只保留单一当前值。

## 功能特性

- 用户属性管理：按 `user_id + attribute_name` 存储通用属性。
- 当前好感度：每个用户一个 `0-400` 的当前值，默认初始值为 `0`。
- 四阶段判定：`0-99`、`100-199`、`200-299`、`300-400`。
- PostgreSQL 持久化：使用共享 PostgreSQL 连接池，无 SQLite 运行时依赖。
- 原子调整：好感度增减在 SQL 事务中完成，并 clamp 到 `[0, 400]`。

## 配置说明

配置由 `config_manager` 管理：

```python
plugin_enable = True
data_retention_days = 30              # 用户属性保留天数
initial_favorability = 0              # 新用户初始当前好感度
max_favorability_delta_per_reply = 5  # 单次回复最大变化绝对值
```

## API 接口

### 好感度相关

```python
from komari_bot.plugins.user_data import (
    adjust_user_favorability,
    get_favorability_stage,
    get_user_favorability,
)

favorability = await get_user_favorability(user_id)
stage = get_favorability_stage(favorability.favorability)
result = await adjust_user_favorability(user_id, delta=1)
```

### 用户属性相关

```python
from komari_bot.plugins.user_data import (
    get_user_attribute,
    get_user_attributes,
    set_user_attribute,
)

await set_user_attribute(user_id, "level", "advanced")
level = await get_user_attribute(user_id, "level")
attributes = await get_user_attributes(user_id)
```

## 数据模型

### UserFavorability

```python
class UserFavorability:
    user_id: str
    favorability: int
    stage_index: int
    stage_name: str
    stage_prompt: str
    updated_at: str
```

### FavorabilityAdjustmentResult

```python
class FavorabilityAdjustmentResult:
    user_id: str
    before: int
    delta: int
    after: int
    stage_index: int
    stage_name: str
    updated_at: str
```

## 数据库结构

### user_favorability

| 字段名 | 类型 | 说明 |
|--------|------|------|
| user_id | TEXT | 主键，用户 ID |
| favorability | INTEGER | 当前好感度，范围 `[0, 400]` |
| updated_at | TIMESTAMPTZ | 最后更新时间 |

启动建表时如果检测到旧版 `last_updated`、`daily_favor` 或 `cumulative_favor` 列，会直接删除旧 `user_favorability` 表并重建；旧好感度历史不会迁移。

### user_attributes

| 字段名 | 类型 | 说明 |
|--------|------|------|
| id | BIGSERIAL | 主键 |
| user_id | TEXT | 用户 ID |
| attribute_name | TEXT | 属性名称 |
| attribute_value | TEXT | 属性值 |
| created_at | TIMESTAMPTZ | 创建时间 |
| updated_at | TIMESTAMPTZ | 更新时间 |

## 聊天集成

`komari_chat` 在生成回复前读取当前好感度并注入 `<favorability_stage>`。LLM 必须先调用 `record_favorability_delta` 工具记录本轮变化；业务代码只会在最终回复成功生成后提交 delta，避免工具重试造成重复写库。
