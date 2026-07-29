# user_ban 管理 API

## 功能语义

`user_ban` 按 QQ 号全局控制两种权限：`chat` 只影响 `komari_chat` 的实际回复，`command` 影响其余全部用户 matcher。数据库以 `(user_id, ban_scope)` 为主键，`expires_at` 为空表示永久封禁，`reason` 保存可选理由。

临时封禁在到期时立即停止拦截；30 秒自然解封任务负责删除到期记录并尝试发送一次私信。SUPERUSER 的记录可以保存和查询，但运行时始终绕过封禁。

## 鉴权与前缀

启用 `komari_management` 后，接口挂载在 `/api/v2/komari-user-bans`，并复用统一管理 API 的具名 Bearer 凭据：

```http
Authorization: Bearer <management-token>
```

## 查询封禁列表

```http
GET /api/v2/komari-user-bans/bans?scope=all&page=1&page_size=20
```

`scope` 支持 `chat`、`command`、`all`；`page_size` 范围为 1 至 100。响应只包含当前有效记录。

## 查询单个用户

```http
GET /api/v2/komari-user-bans/bans/10086
```

响应包含 `active_scopes`、各作用域记录和 `superuser_bypass`。QQ 号必须是不带前导零的正整数字符串。

## 新增或覆盖封禁

```http
POST /api/v2/komari-user-bans/bans
Content-Type: application/json

{
  "user_id": "10086",
  "scope": "all",
  "duration": "7d",
  "reason": "多次刷屏"
}
```

`duration` 支持 `permanent`、`Nm`、`Nh`、`Nd`、`Nw`，省略时默认为永久，最短一分钟、最长十年。理由可省略，最长 500 个字符。API 操作者固定记录为 `management_api`。

## 手动解封

```http
DELETE /api/v2/komari-user-bans/bans/10086/all
```

末尾作用域支持 `chat`、`command`、`all`。不存在有效记录时返回幂等成功，`changed` 为 `false`。

## 修改与通知响应

封禁和解封响应包含：

- `changed`：权限或封禁元数据是否发生变化。
- `action`：`created`、`updated`、`removed` 或 `unchanged`。
- `status`：修改后的当前有效封禁状态。
- `notification`：`attempted`、`sent` 与可选 `error`。

私信使用 OneBot `send_private_msg` 普通文本。无在线 Bot、非好友限制或 QQ 风控导致发送失败时，数据库修改仍然生效，接口返回失败详情且不自动重试。

NapCat 官方虽将 `markdown` 标记为可收发，但发送只能位于双层合并转发中，无法作为普通私聊消息直接发送：[消息格式兼容情况](https://napneko.github.io/develop/msg)、[请求接口兼容情况](https://napneko.github.io/develop/api)。
