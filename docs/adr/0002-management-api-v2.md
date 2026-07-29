# 管理 API v2.0.0 破坏性升级

Komari Management 从 v1 升级到 v2.0.0，一次性删除全部旧兼容入口、旧单 Token 鉴权和弃用别名，所有路由统一迁移到 `/api/v2/<插件名>` 前缀。这是一次有意的破坏性升级：v1 的兼容层（legacy token fallback、deprecated alias、410 stub）已经完成了过渡使命，继续维护它们只会增加代码负担和安全面。

## Status

accepted

## 决策内容

### 路由

- 新前缀格式：`/api/v2/<插件名>`，版本号置于第二层路径。
- 旧 v1 路由（含 `/api/llm-provider/v1/reply-logs` 弃用别名和 `/interaction-histories` 410 stub）全部不注册，由 FastAPI 自然返回 404。不编写任何 stub 代码。
- Swagger/OpenAPI 文档入口改为 `/api/docs` 和 `/api/openapi.json`，不带版本号，后续大版本无需再动。

### 鉴权

- 删除 `api_token` 字段及其完整回退链路（`_legacy_credential()`、`resolve_management_settings()` fallback、启动弃用警告、`LEGACY_TOKEN_REMOVAL_VERSION` 常量）。
- 启动时检测 PG `komari_plugin_configs` 中残留的旧 `api_token` 键：物理删除并输出明确 warning，模式与 `agent_run_logger` 清理旧 `llm_log_*` 键一致。
- 只接受 `api_credentials` 具名凭据。默认推荐配置为单个全权限（`["*"]`）凭据供管理后台使用，细粒度权限留给后续扩展。

### 权限

- `llm_logs:read` 重命名为 `agent_run_logs:read`。
- 新增 `search:read`，保护 `komari_search` provider-descriptors 端点。
- 启动时检测凭据中的旧 `llm_logs:read`，打 warning 提示运维改为新名，不自动改写凭据数据。
- 其余权限名和蕴含关系不变。

### 新增端点

- `GET /api/v2/komari-search/provider-descriptors`：从 Pydantic schema 动态生成 provider 描述，按字段名前缀分组，供前端按 provider 动态渲染配置表单。权限为 `search:read`。

### 不变的部分

- 现有端点的请求/响应 Schema 不变。
- `X-Komari-Change-Reason` 审计头覆盖范围不变。
- CORS 机制、Prompt `If-Match` / revision 机制不变。

### 配置 Schema

- 删除 `api_token: str` 字段。
- `version` 默认值从 `"1.0"` bump 到 `"2.0"`，纯元数据，不做运行时检测或迁移逻辑。

### 迁移

- 无独立迁移脚本。启动时自动清理旧键，运维迁移靠文档（README / CHANGELOG）。

## Considered Options

**路由版本位置**：`/api/<插件名>/v2` vs `/api/v2/<插件名>`。选了后者——版本号前置符合一般 REST 直觉，且未来 v3 只需改一层路径。

**旧路由处置**：410 stub vs 308 重定向 vs 不注册（自然 404）。选了不注册——鉴权层已变，重定向无意义；410 stub 需要维护代码；v2.0.0 是彻底断裂，前端必然重写，不需要"指路"。

**旧 api_token 清理**：静默忽略 vs 主动删除 + 报错。选了主动删除——项目已有先例（agent_run_logger），明确报错降低运维排查成本。

**权限重命名时机**：v2 一并改 vs 保持现状。选了一并改——前端已全量适配，边际成本为零，消除 `llm_logs` 与 `agent-run-logs` 的命名割裂。

**provider-descriptors 生成方式**：静态硬编码 vs Pydantic 反射。选了反射——与 config API 的 field_descriptions 模式一致，schema 变更时端点自动跟上。

**Schema 变更**：趁机调整 vs 不动。选了不动——v2 的破坏性已足够大，Schema 稳定让前端迁移路径收敛为"改 base URL + 改权限配置"。

## Consequences

- 前端必须一次性适配全部新路由前缀和新权限名，不能渐进迁移。
- 运维升级后如果仍用旧 `api_token`，管理 API 不会启动，必须配置 `api_credentials`。
- 凭据中残留的 `llm_logs:read` 不会被自动修正，运维需要手动改为 `agent_run_logs:read`，否则对应端点返回 403。
- 旧路由返回 404 而非 410，外部监控如果依赖 410 状态码判断退役状态需要调整。
