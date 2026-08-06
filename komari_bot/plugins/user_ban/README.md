# user_ban 用户封禁插件

`user_ban` 使用 PostgreSQL 持久化 QQ 用户封禁状态，并将权限分为两个独立作用域：

- `chat`：只禁止 `komari_chat` 实际生成和发送聊天回复；普通群消息仍按原规则参与判定和对话缓冲。
- `command`：禁止除 `komari_chat` 外的所有用户事件 matcher，包括命令、群聊总结和表情投票。

封禁可以永久生效，也可以设置分钟、小时、天或周级期限；到期记录会立即停止拦截，并由每 30 秒执行的任务清理。封禁存储不可用时继续采用故障关闭。

## 多 worker 缓存

运行时以 5 秒为间隔检查 `komari_user_ban_cache_state` 的单行修订号。修订号未变化时只传输一个整数；发生封禁、解封或到期删除后，写事务会原子推进修订号，各 worker 随后使用 `REPEATABLE READ` 重建一致快照。三张表均为 SQLModel ORM 模型（`orm_models.py`），表结构由 Alembic 基线统一管理；首次存储可连接确认与首次快照加载均使用单飞锁，避免并发请求重复初始化或重复拉取全表。

## 管理命令

所有管理命令仅限 SUPERUSER：

```text
.ban chat|command|all <user_id> [permanent|Nm|Nh|Nd|Nw] [理由...]
.unban chat|command|all <user_id>
.ban status <user_id>
.ban list [chat|command|all] [page]
```

示例：

```text
.ban chat 10086
.ban command 10086 2h 频繁调用命令
.ban all 10086 7d 多次刷屏
.ban chat 10086 permanent 长期限制聊天回复
```

旧式 `.ban <scope> <user_id>` 保持兼容，表示永久封禁且不填写理由。重复封禁会覆盖目标作用域的期限、理由与操作者；内容完全相同时保持幂等。

## 私信通知

新增或更新封禁、手动解封及自然解封后，机器人会尝试发送一次普通文本私信。发送失败只记录日志并反馈给管理入口，不会回滚权限状态，也不会自动重试。

NapCat 虽支持 `markdown` 消息段，但官方说明发送端只能用于双层合并转发，不能直接发送普通消息，因此本插件使用 `send_private_msg` 纯文本通知。

## 管理 API

统一管理 API 启用后会挂载 `/api/v2/komari-user-bans`，复用 `komari_management` 的具名 Bearer 凭据与 CORS 配置。接口定义、请求示例和响应字段见 [`API.md`](./API.md)。

被封用户触发受限功能时不会收到提示。SUPERUSER 始终绕过封禁，但仍可保存对应 QQ 的封禁记录并接收生命周期私信。
