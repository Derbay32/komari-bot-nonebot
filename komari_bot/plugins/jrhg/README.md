# JRHG (今日好感) 插件

基于NoneBot2框架的智能问候插件，集成DeepSeek API，支持好感度系统和白名单管理。

## 功能特性

- 🤖 **AI智能问候**: 基于DeepSeek API生成个性化问候语
- 💝 **好感度系统**: 每日生成1-100好感度，影响AI回复态度
- 📊 **数据持久化**: 使用SQLite存储用户好感度数据
- 🔒 **权限控制**: 支持用户和群聊白名单，SUPERUSER可管理插件
- 🎯 **态度适配**: 根据好感度自动调整AI回复的友好程度
- 🔄 **每日重置**: 每天0点自动生成新的好感度

## 安装依赖

```bash
pip install aiohttp aiosqlite
```

## 配置说明

在bot的配置文件中添加以下配置：

```python
# DeepSeek API配置
deepseek_api_url = "https://api.deepseek.com/v1/chat/completions"
deepseek_api_token = "your_deepseek_api_token_here"  # 必填
deepseek_model = "deepseek-chat"
deepseek_temperature = 0.7
deepseek_frequency_penalty = 0.0
deepseek_default_prompt = "你是小鞠，一个可爱的AI助手。请根据你对用户的好感度，用相应的态度和用户打招呼。好感度越高，你的语气应该越热情友好。"

# 插件开关
jrhg_plugin_enable = False  # 默认关闭

# 白名单配置（可选）
jrhg_user_whitelist = []  # 用户白名单，为空则允许所有用户
jrhg_group_whitelist = []  # 群聊白名单，为空则允许所有群聊
```

## 使用方法

### 基本命令

- `/jrhg` - 获取今日好感问候
- `/jrhg on` - 管理员开启插件
- `/jrhg off` - 管理员关闭插件
- `/jrhg status` - 管理员查看插件状态

### 好感度等级

- **1-20**: 非常冷淡 - 用疏远的语气回应
- **21-40**: 冷淡 - 用有距离感的语气回应
- **41-60**: 中性 - 用礼貌的语气回应
- **61-80**: 友好 - 用热情的语气回应
- **81-100**: 非常友好 - 用亲密的语气回应

### 响应格式

```
[AI生成的问候内容]
【小鞠对用户昵称今日的好感为好感值】
```

## 插件依赖

本插件依赖 `user_data`、`permission_manager`、`config_manager` 插件来管理用户数据、配置文件、使用权限。请确保：

1. `user_data`、`permission_manager`、`config_manager` 插件已正确安装在 `plugins/user_data/` 目录
2. 插件配置文件正确设置
3. 相关依赖包已安装

## 文件结构

```
plugins/
└── jrhg/                         # JRHG插件
    ├── __init__.py               # 主插件逻辑
    ├── config.py                 # 配置模型
    ├── deepseek_client.py        # DeepSeek API 客户端
    └── README.md                 # 说明文档
```

## 注意事项

1. **API密钥安全**: 请妥善保管DeepSeek API密钥，不要泄露
2. **权限管理**: 只有SUPERUSER可以管理插件开关
3. **白名单设置**: 建议在生产环境中设置白名单以控制使用权限
4. **网络依赖**: 插件需要访问DeepSeek API，请确保网络连接正常
5. **数据备份**: 建议定期备份SQLite数据库文件

## 故障排除

### 常见问题

1. **插件无法启动**
   - 检查 `user_data` 插件是否正确安装
   - 确认所有依赖包已安装
   - 查看日志文件获取详细错误信息

2. **API调用失败**
   - 验证DeepSeek API密钥是否正确
   - 检查网络连接和API URL
   - 确认API配额和可用性

3. **权限错误**
   - 确认用户ID和群组ID配置正确
   - 检查SUPERUSER配置
   - 验证白名单设置

### 日志查看

插件会记录详细的运行日志，包括：
- API调用状态
- 用户操作记录
- 错误和异常信息
