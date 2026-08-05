"""LLM 网关请求协议相关的共享类型。

请求 API 枚举被多个插件的配置 Schema 与网关公共 API 共用，
放在 common 层避免业务插件直接 import llm_provider 包触发插件加载副作用。
"""

from typing import Literal

type RequestApi = Literal["chat_completions", "responses"]
"""LLM 网关请求 API 枚举：Chat Completions 或 Responses。"""

DEFAULT_REQUEST_API: RequestApi = "chat_completions"
"""所有模型槽位的默认请求 API（升级部署零行为变化）。"""
