# Komari Management 前端对接说明

> 最后同步：2026-07-22。本文以当前后端代码与 OpenAPI Schema 为准，面向管理后台前端开发。

Komari Management 统一把配置、提示词、判定场景、维护公告以及各业务插件的管理 API 挂载到 NoneBot2 的 FastAPI 应用。前端不需要对接独立的管理服务，所有接口共用同一后端 Origin、Bearer 鉴权和 CORS 配置。

## 1. 联调入口

- Swagger UI：由 `FASTAPI_DOCS_URL` 配置，项目建议值为 `/api/komari-management/docs`
- OpenAPI JSON：由 `FASTAPI_OPENAPI_URL` 配置，项目建议值为 `/api/komari-management/openapi.json`
- API Base URL：使用机器人后端的 HTTP Origin，例如 `https://bot.example.com`

Swagger/OpenAPI 文档本身可以公开访问，但下列业务接口全部需要 Bearer Token。

后端只在以下条件都满足时注册管理 API：

1. `komari_management.plugin_enable=true`；
2. 至少存在一条有效的 `api_credentials`，或仍在使用符合强度要求的旧版 `api_token`；
3. NoneBot2 使用 FastAPI Driver；
4. 前端 Origin 已加入 `api_allowed_origins`，跨域访问时需要完整匹配协议、主机和端口。

`api_allowed_origins` 是启动时配置，修改后需要重启后端。CORS 已允许所有请求方法和请求头，但不会替代 Bearer 鉴权。

## 2. 公共请求约定

### 2.1 鉴权

每个请求都应发送：

```http
Authorization: Bearer <management-token>
```

前端不得把 Token 写入 URL、日志、错误上报、Local Storage 明文快照或页面可序列化状态。是否采用仅内存存储或安全代理层，由前端部署方式决定。

鉴权错误：

- `401`：没有 Token、Token 错误或凭据已到撤销时间；
- `403`：Token 有效，但没有当前接口所需权限。

具名凭据支持精确权限、资源通配符（例如 `config:*`）和全局通配符 `*`。写权限自动包含同资源的读权限；`announce:send` 自动包含 `announce:read`。

| 功能 | 读权限 | 写权限 |
| --- | --- | --- |
| 动态配置 | `config:read` | `config:write` |
| 提示词 | `prompt:read` | `prompt:write` |
| 判定场景 | `scene:read` | `scene:write` |
| 维护公告 | `announce:read` | `announce:send` |
| 知识库 | `knowledge:read` | `knowledge:write` |
| 帮助库 | `help:read` | `help:write` |
| 记忆库 | `memory:read` | `memory:write` |
| Agent Run 日志 | `llm_logs:read` | 无写接口 |
| 用户封禁 | `user_ban:read` | `user_ban:write` |

### 2.2 审计写请求头

以下写接口必须发送非空的变更原因：

```http
X-Komari-Change-Reason: update-sentry-pii-setting
```

变更原因必须是不超过 200 字的可打印文本。缺失或空值会返回：

```json
{
  "detail": "写操作必须提供 X-Komari-Change-Reason"
}
```

对应状态码为 `400`。当前需要该请求头的接口是：

- 配置：字段更新、资源重载；
- 提示词：整份替换、单字段更新；
- Scene：全量写入、局部更新、同步；
- 维护公告：发送；
- 用户封禁：封禁、解封。

这些接口还接受：

```http
X-Request-ID: web-019f7909-201b-7002
```

调用方不传时后端会生成，但前端应主动生成并记录。格式为 1～64 个字符，首字符必须是字母或数字，后续只允许字母、数字、`_`、`.`、`:`、`-`。

维护公告使用 `X-Request-ID` 作为持久化幂等键。重试同一次发送必须复用原 ID 和完全相同的请求体；新公告必须生成新 ID，不能让同一个 ID 对应不同内容。

### 2.3 推荐请求封装

```ts
type AuditHeadersOptions = {
  reason: string;
  requestId: string;
};

export function managementHeaders(
  token: string,
  audit?: AuditHeadersOptions,
): HeadersInit {
  return {
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
    ...(audit
      ? {
          "X-Komari-Change-Reason": audit.reason,
          "X-Request-ID": audit.requestId,
        }
      : {}),
  };
}
```

前端应使用 JSON 原生类型传值，例如布尔字段发送 `true`，不要发送字符串 `"true"`。`DELETE` 返回 `204` 时不要继续解析 JSON。

### 2.4 错误响应

常规错误结构为：

```ts
type ApiError = {
  detail: string | Record<string, unknown> | Array<Record<string, unknown>>;
};
```

`detail` 不能只按字符串处理：FastAPI 参数校验和部分业务错误会返回对象或数组。例如维护公告冷却时返回：

```json
{
  "detail": {
    "message": "维护通知发送过于频繁，请稍后再试",
    "remaining_seconds": 12.5
  }
}
```

建议统一处理以下状态码：

| 状态码 | 前端行为 |
| --- | --- |
| `400` | 展示请求条件错误；优先检查 `X-Komari-Change-Reason` |
| `401` | 清除当前管理凭据并回到登录页 |
| `403` | 展示权限不足，不要按登录失效处理 |
| `404` | 刷新列表，目标可能已不存在 |
| `409` | 处理并发更新、幂等冲突或正在执行状态 |
| `410` | 当前接口已退役，切换到响应给出的替代接口 |
| `422` | 展示字段校验信息 |
| `429` | 按 `remaining_seconds` 禁用提交按钮 |
| `503` | 服务依赖未就绪，可保留页面并允许稍后重试 |

## 3. 动态配置页面

Base path：`/api/komari-management-config/v1`

| 方法 | 路径 | 权限 | 用途 |
| --- | --- | --- | --- |
| `GET` | `/resources` | `config:read` | 获取资源摘要及字段元数据 |
| `GET` | `/resources/{resource_id}` | `config:read` | 获取配置值与生效状态 |
| `PATCH` | `/resources/{resource_id}/fields/{field_name}` | `config:write` | 更新单个字段 |
| `POST` | `/resources/{resource_id}/reload` | `config:write` | 从 PostgreSQL 重载资源 |

当前资源 ID：

```text
komari_management
komari_memory
komari_knowledge
komari_help
agent_run_logger
llm_provider
embedding_provider
group_history_summary
komari_decision
komari_sentry
sr
user_data
```

字段更新请求体只有一层 `value`：

```json
{
  "value": true
}
```

例如更新 `komari_sentry.send_default_pii`：

```http
PATCH /api/komari-management-config/v1/resources/komari_sentry/fields/send_default_pii
Authorization: Bearer <management-token>
Content-Type: application/json
X-Komari-Change-Reason: update-sentry-pii-setting
X-Request-ID: config-komari-sentry-pii-001

{"value":true}
```

`send_default_pii` 默认关闭且重启后生效。该开关仅控制是否向 Sentry 上报
user 上下文（用户标识）；其余诊断数据（异常正文、breadcrumb、Sentry Logs
正文、事务/span、堆栈局部变量等）默认全量上报，只通过黑名单式脱敏隐藏
凭据类字段（API Key、Token、密码、连接串、DSN、API 形状 URL 等）。
已经发送的历史事件不会补发或恢复。

详情响应中的关键字段：

```ts
type ConfigFieldMetadata = {
  secret: boolean;
  apply_mode: "immediate" | "rebuild" | "restart";
};

type ConfigFieldState = ConfigFieldMetadata & {
  configured_value: unknown;
  effective_value: unknown;
  source: string;
  effective_source:
    | "dynamic_config"
    | "service_snapshot"
    | "process_startup_snapshot";
  restart_required: boolean;
};
```

前端渲染规则：

- 使用 `field_descriptions` 作为字段说明，使用 `field_metadata` 和 `field_states` 展示生效方式；
- `apply_mode=immediate` 表示接口成功后已可确认即时生效；
- `apply_mode=rebuild` 表示需要相关服务重建，`effective_value` 可能为 `null`；
- `apply_mode=restart` 表示必须重启进程，`restart_required=true`；
- `secret=true` 的值始终返回 `"******"`，包括 `values`、`configured_value` 和可展示的 `effective_value`；
- 不得把 `"******"` 当成真实值回写。秘密字段应使用空白密码框，仅在用户明确输入新值时提交；
- `api_credentials` 整个字段都按秘密值处理。编辑它时需要让操作者重新输入完整替换值，不能从 GET 响应恢复 Token。

## 4. Prompt 编辑页面

Base path：`/api/komari-management-prompt/v1`

| 方法 | 路径 | 权限 | 用途 |
| --- | --- | --- | --- |
| `GET` | `/resources` | `prompt:read` | 获取 Prompt 资源列表 |
| `GET` | `/resources/{resource_id}` | `prompt:read` | 获取字段值和 revision |
| `PUT` | `/resources/{resource_id}` | `prompt:write` | 整份替换 Prompt 字段对象 |
| `PATCH` | `/resources/{resource_id}/fields/{field_name}` | `prompt:write` | 更新单个 Prompt 字段 |

当前资源 ID：

```text
komari_chat
komari_memory_summary
group_history_summary
```

所有 Prompt 写操作除审计请求头外，还必须发送当前详情响应里的 `revision`：

```http
If-Match: "7"
```

这里只接受强 ETag；`W/"7"` 会返回 `422`。后端目前通过响应 JSON 的 `revision` 字段返回版本号，不依赖响应 `ETag` 头。

单字段更新：

```json
{
  "value": "新的 Prompt 内容"
}
```

整份替换直接发送字段对象，不要包在 `values` 中：

```json
{
  "system_prompt": "……",
  "memory_ack": "……",
  "memory_ack_role": "assistant",
  "output_instruction": "……",
  "cot_prefix": "……",
  "cot_prefix_role": "assistant"
}
```

`PUT` 会把没有提交的字段恢复为该资源的代码默认值，因此前端执行整份替换时应提交当前详情的完整 `values`，再覆盖用户修改的字段。

收到 `409` 表示 revision 已过期。前端应重新 GET 最新详情，保留用户草稿并提示比较差异，不能静默覆盖。

## 5. Komari Decision Scene 页面

Base path：`/api/komari-decision-scenes/v1`

| 方法 | 路径 | 权限 | 用途 |
| --- | --- | --- | --- |
| `GET` | `/scenes` | `scene:read` | 获取 Scene 摘要列表 |
| `GET` | `/scenes/{scene_key}` | `scene:read` | 获取包含 `content_text` 的详情 |
| `PUT` | `/scenes/{scene_key}` | `scene:write` | 创建或全量替换 |
| `PATCH` | `/scenes/{scene_key}` | `scene:write` | 局部更新 |
| `POST` | `/sync` | `scene:write` | 构建/同步 Scene Set |

`PUT` 请求体：

```json
{
  "scene_type": "general",
  "content_text": "场景模板正文",
  "enabled": true,
  "order_index": 10
}
```

`PATCH` 可提交 `content_text`、`enabled`、`order_index` 中任意需要修改的字段。

固定 Scene `NOISE`、`MEANINGFUL`、`CALL_DIRECT`、`CALL_MENTION` 不允许改为 `general`，也不允许禁用。保存模板后由操作者触发 `/sync`；同步响应中的 `pending_count` 大于 0 表示 embedding 仍可能在后台生成，不能把 HTTP 成功直接显示成“全部就绪”。

## 6. 维护公告页面

Base path：`/api/komari-announce/v1`

| 方法 | 路径 | 权限 | 用途 |
| --- | --- | --- | --- |
| `GET` | `/groups` | `announce:read` | 获取所有在线 Bot 可见的群 |
| `POST` | `/maintenance` | `announce:send` | 向选中的群发送维护公告 |

群列表会合并多个 Bot 的结果。`bot_ids` 表示可以访问该群的 Bot，`unavailable_bot_count` 表示拉取群列表失败的 Bot 数量。

发送请求：

```json
{
  "title": "数据库维护",
  "content": "升级 PostgreSQL\n重建向量索引",
  "scheduled_time": "2026-07-20 02:00-03:00 CST",
  "group_ids": [10001, 10002]
}
```

对接注意事项：

- 必须携带 `X-Komari-Change-Reason`；
- 必须由前端稳定生成 `X-Request-ID`，提交后在请求完成前禁止重复点击；
- 网络错误重试时复用相同 ID 和请求体，后端会返回已保存的结果或明确的冲突状态；
- `results[].status` 为 `success`、`unreachable` 或 `failed`，HTTP `200` 不代表所有群都发送成功；
- 遇到 `error_code=delivery_unknown` 时展示“需要人工核对”，禁止前端自动换新 request ID 重发；
- 单次群数量还受动态配置 `announce_max_group_count` 限制。

## 7. 用户封禁页面

Base path：`/api/komari-user-bans/v1`

| 方法 | 路径 | 权限 | 用途 |
| --- | --- | --- | --- |
| `GET` | `/bans?scope=all&page=1&page_size=20` | `user_ban:read` | 分页查询有效封禁 |
| `GET` | `/bans/{user_id}` | `user_ban:read` | 查询一个 QQ 用户 |
| `POST` | `/bans` | `user_ban:write` | 创建或覆盖封禁 |
| `DELETE` | `/bans/{user_id}/{scope}` | `user_ban:write` | 解除指定作用域封禁 |

封禁请求：

```json
{
  "user_id": "10086",
  "scope": "all",
  "duration": "7d",
  "reason": "违规操作"
}
```

- `user_id` 必须是无前导零的正整数字符串，不能作为 JSON number 发送；
- `scope` 可为 `chat`、`command`、`all`；
- `duration` 可为 `permanent`，或正整数加 `m`、`h`、`d`、`w`，最长十年；
- `superuser_bypass=true` 表示该用户虽有记录但运行时仍会绕过封禁；
- `notification` 是私信通知尝试结果，通知失败不会回滚封禁操作；
- `POST` 和 `DELETE` 都必须携带审计请求头。

## 8. 知识库与帮助库页面

### 8.1 知识库

Base path：`/api/komari-knowledge/v1`

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/knowledge?q=&category=&limit=20&offset=0` | 筛选和分页 |
| `GET` | `/knowledge/{id}` | 获取详情 |
| `POST` | `/knowledge` | 创建 |
| `PATCH` | `/knowledge/{id}` | 局部更新 |
| `DELETE` | `/knowledge/{id}` | 删除，成功返回 `204` |
| `POST` | `/search` | 测试实际检索结果 |

知识分类：`general`、`character`、`setting`、`plot`、`other`、`custom`。创建时 `content` 和非空 `keywords` 必填；检索请求为 `{"query":"关键词","limit":5}`。

### 8.2 帮助库

Base path：`/api/komari-help/v1`

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/help?q=&category=&limit=20&offset=0` | 筛选和分页 |
| `GET` | `/help/{id}` | 获取详情 |
| `POST` | `/help` | 创建 |
| `PATCH` | `/help/{id}` | 局部更新 |
| `DELETE` | `/help/{id}` | 删除，成功返回 `204` |
| `POST` | `/search` | 测试实际检索结果 |
| `POST` | `/scan` | 扫描并同步插件帮助，重复执行可能返回 `409` |

帮助分类：`command`、`feature`、`faq`、`other`。创建时 `title`、`content` 必填；`keywords`、`plugin_name`、`notes` 可选。

知识库和帮助库的 GET/搜索需要对应读权限，增删改和帮助扫描需要对应写权限。目前这些接口不要求 `X-Komari-Change-Reason`。

## 9. 记忆库页面

Base path：`/api/komari-memory/v1`

| 数据区 | 端点 |
| --- | --- |
| 对话总结失败队列 | `GET /conversation-dead-letters`、`POST /conversation-dead-letters/{group_id}/{snapshot_id}/requeue` |
| 对话记忆 | `GET/POST /conversations`、`GET/PATCH/DELETE /conversations/{id}` |
| 用户画像 | `GET /user-profiles`、`GET/PUT/DELETE /user-profiles/{group_id}/{user_id}` |
| 互动事件 | `GET /interactions`、`GET/PATCH/DELETE /interactions/{event_id}` |

对话记忆、用户画像和互动事件列表统一使用 `items`、`total`、`limit`、`offset`，单页 `limit` 为 1～100；失败队列响应只有 `items` 和 `limit`。主要筛选项：

- 对话记忆：`group_id`、`participant`、`q`；
- 用户画像：`group_id`、`user_id`、`q`；
- 互动事件：`user_id`、`q`。

用户画像 PUT 的正文是画像 JSON 本身，不要额外包 `value` 或 `profile`；重要度通过查询参数 `importance=1..5` 传递。路径中的 `user_id` 与正文中的 `user_id` 如果同时存在必须一致。

旧 `/interaction-histories` 系列接口已退役并固定返回 `410`。新前端必须使用 `/interactions`，先按 `user_id` 查询事件 ID，再按事件 ID 读写。

读操作需要 `memory:read`，写操作和失败快照重新入队需要 `memory:write`。当前这些写接口不要求审计请求头。

## 10. Agent Run 日志页面

Base path：`/api/agent-run-logs/v1`

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/runs` | 查询 Agent Run 列表；仅当前页从 JSONL 临时生成预览 |
| `GET` | `/runs/{run_id}` | 获取一条完整 JSONL v3 任务日志 |

列表筛选参数：

- `date=YYYY-MM-DD`，指定后查询单日；
- `days=1..90`，默认 7；
- `run_type`、`task_kind`、`origin=normal|debug`；
- `trace_id`、`model`、`method`；
- `status=success|error|cancelled`；
- `limit=1..100`、`offset>=0`。

详情会返回完整业务输入、输出、prompt/messages、reasoning、工具参数与结果，前端必须把该页面视为最高敏感度运维数据，不得写入浏览器日志、错误上报或公开页面。后端只过滤显式凭据，并把图片 URL、base64、bytes 与渲染图片替换为 MIME/长度/字节数/SHA-256 摘要。debug 私聊诊断仍使用另一套脱敏投影，不等同于此接口。

PostgreSQL 只保存可重建的定位元数据；列表预览来自当前页命中的 JSONL。PG 索引不可用时读取器自动降级扫描保留期文件，读取器整体未初始化时返回 `503`。稳定定位键为 `run_id`。

旧 `/api/llm-provider/v1/reply-logs` 与 `/reply-logs/{date}/{line_number}` 暂时保留为弃用别名，新前端不得继续依赖旧 URL。

## 11. 前端实现优先级与验收清单

建议按以下顺序接入：

1. 公共 Bearer 客户端、权限错误页、统一错误解析；
2. 配置资源与字段编辑，正确展示秘密值和生效模式；
3. Prompt 编辑器，实现 revision 冲突处理和草稿保留；
4. Scene 编辑与同步状态；
5. 公告群选择、幂等提交和逐群结果；
6. 用户封禁；
7. 知识库、帮助库、记忆库 CRUD；
8. Agent Run 日志筛选与完整详情，并为正文区域增加敏感数据提示。

联调验收时至少确认：

- 所有请求都携带 Bearer Token；
- `401` 与 `403` 的页面行为不同；
- 所有审计写接口都携带非空 `X-Komari-Change-Reason`；
- Prompt 写入携带最新 `If-Match`，`409` 不会覆盖本地草稿；
- `204`、字符串 `detail`、对象/数组 `detail` 均可处理；
- 秘密字段掩码不会被回写；
- `restart_required=true` 会明确提示重启；
- 公告重试不会生成新的 request ID，也不会因 HTTP `200` 忽略单群失败；
- 页面不会使用已退役的 `/interaction-histories` 接口；
- 前端生产 Origin 已加入 CORS 白名单。

## 12. 后端运行说明

- 动态配置存储于 PostgreSQL `komari_plugin_configs`；
- Prompt 存储于 PostgreSQL `komari_prompt_configs`，旧 YAML 不是运行时来源；
- 业务插件自己的管理 API 挂载逻辑已经移除，统一由本插件注册；
- 知识库、帮助库、记忆库、Agent Run 日志或 Scene 依赖未初始化时，对应接口可能返回 `503`；
- FastAPI OpenAPI 是字段级类型、格式和长度限制的最终机器可读契约，前端类型应优先从 OpenAPI 生成或定期校对。
