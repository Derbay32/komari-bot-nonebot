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

## 说明

- Swagger/OpenAPI 文档页公开访问，具体管理接口仍然要求 `Authorization: Bearer <api_token>`
- 业务插件自己的管理接口挂载逻辑已经移除，统一由本插件负责注册
- 若知识库、记忆库或 reply 日志读取器未初始化，对应接口会返回 `503`
