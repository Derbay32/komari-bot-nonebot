# Komari Management

统一挂载本地管理 API，并复用 NoneBot2 FastAPI 驱动官方 Swagger/OpenAPI 文档。

## 功能

- 挂载知识库管理接口：`/api/komari-knowledge/v1`
- 挂载记忆库管理接口：`/api/komari-memory/v1`
- 挂载 reply 日志接口：`/api/llm-provider/v1`
- 复用 FastAPI 官方文档入口：
  - `FASTAPI_DOCS_URL=/api/komari-management/docs`
  - `FASTAPI_OPENAPI_URL=/api/komari-management/openapi.json`

## 配置

动态配置存储在 PostgreSQL `komari_plugin_configs` 表，首次缺失时从 `.env` / schema 默认值初始化。

管理 API 配置响应使用 `config_source` 标识来源，例如 `postgres:komari_plugin_configs/komari_management`。

每个受管配置 Schema 必须通过 `model_config.json_schema_extra.default_apply_mode` 声明默认生效模式（`immediate`、`rebuild` 或 `restart`），字段可用 `Field(json_schema_extra=...)` 覆盖。秘密字段必须显式声明 `secret=true`，禁止只按字段名猜测。

配置详情中的 `field_states` 会分别展示持久化值、可确认的当前生效值、来源、生效模式和 `restart_required`。对于进程启动或服务构建时形成的快照，接口无法可靠读取当前值，因此 `effective_value` 返回 `null`，避免误报为已热更新；秘密值在所有位置统一显示为 `******`。

## 说明

- Swagger/OpenAPI 文档页公开访问，具体管理接口仍然要求 `Authorization: Bearer <api_token>`
- 业务插件自己的管理接口挂载逻辑已经移除，统一由本插件负责注册
- 若知识库、记忆库或 reply 日志读取器未初始化，对应接口会返回 `503`
