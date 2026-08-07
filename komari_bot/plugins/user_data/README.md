# 用户数据插件

为 NoneBot2 提供当前好感度数据服务。当前版本已移除旧版每日随机好感、累计好感、历史查询、`.jrhg` 指令与旧用户属性能力；好感度只保留单一当前值。

## 功能特性

- 当前好感度：每个用户一个 `0-400` 的当前值，默认初始值为 `0`。
- 四阶段判定：`0-99`、`100-199`、`200-299`、`300-400`。
- PostgreSQL 持久化：SQLModel ORM 表（`orm_models.py`），连接与引擎生命周期由 nonebot-plugin-orm 托管，无 SQLite 运行时依赖。
- 原子调整：好感度增减在 SQL 事务中完成，并 clamp 到 `[0, 400]`。

## 配置说明

配置由 `config_manager` 管理：

```python
plugin_enable = True
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

### user_favorability_adjustment_ledger

| 字段名 | 类型 | 说明 |
|--------|------|------|
| operation_id | TEXT | 主键，幂等操作 ID |
| user_id | TEXT | 用户 ID |
| requested_delta | INTEGER | 请求的好感度增量 |
| before_value / after_value | INTEGER | 调整前后值（回填） |
| result_updated_at / created_at | TIMESTAMPTZ | 结果回填时间 / 创建时间 |

两张表的 DDL 由 Alembic 基线 `0001` 统一管理，运行时不建表、不删表；旧版 SQLite 时代的 `last_updated`、`daily_favor`、`cumulative_favor` 列不存在于当前结构，历史好感度不迁移。

## 聊天集成

`komari_chat` 在生成回复前读取当前好感度并注入 `<favorability_stage>`。LLM 必须先调用 `record_favorability_delta` 工具记录本轮变化；业务代码只会在最终回复成功生成后提交 delta，避免工具重试造成重复写库。
