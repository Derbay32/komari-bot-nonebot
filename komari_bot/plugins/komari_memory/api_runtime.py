"""Komari Memory REST API 挂载辅助。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from komari_bot.common.management_api import load_shared_management_settings

from .api import API_PREFIX, MemoryServiceProtocol, register_memory_api

if TYPE_CHECKING:
    from collections.abc import Callable


def register_management_api_for_driver(
    *,
    driver: object,
    config: object,
    service_getter: Callable[[], MemoryServiceProtocol | None],
    logger: Any,
) -> bool:
    """按当前驱动与配置决定是否挂载记忆管理 API。"""
    if not getattr(config, "plugin_enable", False):
        logger.info("[Komari Memory] 插件未启用，跳过管理 API 注册")
        return False

    if not getattr(config, "api_enabled", True):
        logger.info("[Komari Memory] 本地 REST 管理接口已禁用，跳过注册")
        return False

    shared_settings = load_shared_management_settings(logger)
    if shared_settings is None:
        return False

    driver_type = getattr(driver, "type", None)
    server_app = getattr(driver, "server_app", None)
    if driver_type != "fastapi" or server_app is None:
        logger.warning("[Komari Memory] 当前驱动不是 FastAPI，无法挂载管理 API")
        return False

    register_memory_api(
        server_app,
        api_token=shared_settings.api_token,
        allowed_origins=shared_settings.allowed_origins,
        service_getter=service_getter,
    )
    logger.info("[Komari Memory] 管理 API 已注册: %s", API_PREFIX)
    return True
