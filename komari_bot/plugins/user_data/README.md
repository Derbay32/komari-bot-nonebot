# 用户数据插件

为NoneBot2提供通用用户数据管理功能，支持用户属性存储和好感度系统。

## 功能特性

- 👤 **用户属性管理**: 存储和管理用户的各种属性数据
- 💝 **好感度系统**: 专门的好感度管理功能
- 💾 **数据持久化**: 使用SQLite数据库存储数据
- 🔄 **自动重置**: 支持每日好感度自动重置
- 📊 **数据统计**: 提供用户和群组统计功能
- 🔧 **API接口**: 提供丰富的API供其他插件调用

## 安装依赖

```bash
pip install aiosqlite pydantic
```

## 配置说明

在bot的配置文件中添加以下配置：

```python
# 数据库配置
user_data_db_path = "user_data.db"  # 数据库文件路径

# 数据清理配置
user_data_data_retention_days = 30  # 数据保留天数，0表示不清理
```

## API接口

### 好感度相关

```python
# 获取或生成用户好感度
favor_result = await generate_or_update_favorability(user_id, group_id)

# 获取用户好感度
favorability = await get_user_favorability(user_id, group_id)

# 获取好感度历史记录
history = await get_favor_history(user_id, group_id, days=7)
```

### 用户属性相关

```python
# 设置用户属性
await set_user_attribute(user_id, group_id, "level", "advanced")

# 获取用户属性
level = await get_user_attribute(user_id, group_id, "level")

# 获取用户所有属性
attributes = await get_user_attributes(user_id, group_id)
```

### 统计功能

```python
# 获取用户总数
user_count = await get_user_count()

# 获取群组总数
group_count = await get_group_count()
```

### 便捷函数

```python
# 获取好感度态度描述
attitude = await get_favor_attitude(75)  # 返回 "友好"

# 格式化好感度回复
response = await format_favor_response(
    ai_response="你好呀！",
    user_nickname="小明",
    daily_favor=75
)
```

## 数据模型

### UserAttribute（用户属性）

```python
class UserAttribute:
    user_id: str          # 用户ID
    group_id: str         # 群组ID
    attribute_name: str   # 属性名称
    attribute_value: str  # 属性值
    created_at: str       # 创建时间
    updated_at: str       # 更新时间
```

### UserFavorability（用户好感度）

```python
class UserFavorability:
    user_id: str           # 用户ID
    group_id: str          # 群组ID
    daily_favor: int       # 每日好感度 (1-100)
    cumulative_favor: int  # 累计好感度
    last_updated: date     # 最后更新日期
```

### FavorGenerationResult（好感度生成结果）

```python
class FavorGenerationResult:
    user_id: str           # 用户ID
    group_id: str          # 群组ID
    daily_favor: int       # 每日好感度
    cumulative_favor: int  # 累计好感度
    is_new_day: bool       # 是否为新的一天
    favor_level: str       # 好感度等级描述
```

## 数据库结构

### user_attributes 表（用户属性）

| 字段名 | 类型 | 说明 |
|--------|------|------|
| id | INTEGER | 主键 |
| user_id | TEXT | 用户ID |
| group_id | TEXT | 群组ID |
| attribute_name | TEXT | 属性名称 |
| attribute_value | TEXT | 属性值 |
| created_at | TIMESTAMP | 创建时间 |
| updated_at | TIMESTAMP | 更新时间 |

### user_favorability 表（好感度）

| 字段名 | 类型 | 说明 |
|--------|------|------|
| user_id | TEXT | 用户ID |
| group_id | TEXT | 群组ID |
| daily_favor | INTEGER | 每日好感度 |
| cumulative_favor | INTEGER | 累计好感度 |
| last_updated | DATE | 最后更新日期 |

## 使用示例

### 在其他插件中使用

```python
# 导入用户数据插件的API
from user_data import (
    generate_or_update_favorability,
    set_user_attribute,
    get_user_attribute,
    format_favor_response
)

async def my_plugin_handler(bot, event):
    user_id = event.get_user_id()
    group_id = str(event.group_id)

    # 生成好感度
    favor_result = await generate_or_update_favorability(user_id, group_id)

    # 根据好感度生成不同回复
    if favor_result.daily_favor > 80:
        response = "今天心情很好呢！"
    else:
        response = "今天过得怎么样？"

    # 设置自定义属性
    await set_user_attribute(user_id, group_id, "last_interaction", str(datetime.now()))

    await bot.send(event, response)
```

### 好感度系统示例

```python
from user_data import get_favor_attitude

async def custom_greeting(bot, event):
    user_id = event.get_user_id()
    group_id = str(event.group_id)

    favor = await get_user_favorability(user_id, group_id)
    if favor:
        attitude = await get_favor_attitude(favor.daily_favor)
        greetings = {
            "非常冷淡": ["嗯。", "你好。"],
            "冷淡": ["你好，有什么事吗？", "嗯，你好。"],
            "中性": ["你好呀！", "嗨，你好！"],
            "友好": ["嗨！很高兴见到你！", "你好呀！今天怎么样？"],
            "非常友好": ["见到你真好！", "亲爱的，你好呀！"]
        }

        import random
        greeting = random.choice(greetings.get(attitude, ["你好。"]))
    else:
        greeting = "你好，初次见面！"

    await bot.send(event, greeting)
```

## 注意事项

1. **并发安全**: 数据库操作支持并发，使用了适当的锁机制
2. **数据备份**: 建议定期备份SQLite数据库文件
3. **性能优化**: 对于大量数据，已创建索引提高查询效率
4. **错误处理**: API调用包含适当的错误处理，建议在调用时也添加异常处理

## 依赖关系

- Python 3.8+
- NoneBot2
- aiosqlite
- pydantic

## 许可证

本插件遵循MIT许可证。