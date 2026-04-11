"""Komari Management 统一管理 API 挂载辅助。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from komari_bot.common.management_api import resolve_management_settings

KNOWLEDGE_API_PREFIX = "/api/komari-knowledge/v1"
MEMORY_API_PREFIX = "/api/komari-memory/v1"
LLM_PROVIDER_API_PREFIX = "/api/llm-provider/v1"

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True, slots=True)
class ManagementApiComponents:
    """统一管理 API 注册所需组件。"""

    register_knowledge_api: Callable[..., None]
    knowledge_engine_getter: Callable[[], object | None]
    register_memory_api: Callable[..., None]
    memory_service_getter: Callable[[], object | None]
    register_llm_provider_api: Callable[..., None]
    reply_log_reader_getter: Callable[[], object | None]


def register_management_api_for_driver(
    *,
    driver: object,
    config: object,
    component_loader: Callable[[], ManagementApiComponents],
    logger: Any,
) -> bool:
    """按驱动与集中配置决定是否挂载统一管理 API。"""
    if not getattr(config, "plugin_enable", False):
        logger.info("[Komari Management] 插件未启用，跳过管理 API 注册")
        return False

    settings = resolve_management_settings(
        config,
        logger=logger,
        warning_prefix="[Komari Management]",
    )
    if settings is None:
        return False

    driver_type = getattr(driver, "type", None)
    server_app = getattr(driver, "server_app", None)
    if driver_type != "fastapi" or server_app is None:
        logger.warning("[Komari Management] 当前驱动不是 FastAPI，无法挂载管理 API")
        return False

    components = component_loader()
    components.register_knowledge_api(
        server_app,
        api_token=settings.api_token,
        allowed_origins=settings.allowed_origins,
        engine_getter=components.knowledge_engine_getter,
    )
    components.register_memory_api(
        server_app,
        api_token=settings.api_token,
        allowed_origins=settings.allowed_origins,
        service_getter=components.memory_service_getter,
    )
    components.register_llm_provider_api(
        server_app,
        api_token=settings.api_token,
        allowed_origins=settings.allowed_origins,
        reader_getter=components.reply_log_reader_getter,
    )
    logger.info(
        "[Komari Management] 管理 API 已注册: %s, %s, %s",
        KNOWLEDGE_API_PREFIX,
        MEMORY_API_PREFIX,
        LLM_PROVIDER_API_PREFIX,
    )
    return True
