"""LLM Provider REST API 挂载辅助。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from komari_bot.common.management_api import load_shared_management_settings

from .api import API_PREFIX, ReplyLogReaderProtocol, register_llm_provider_api

if TYPE_CHECKING:
    from collections.abc import Callable


def register_management_api_for_driver(
    *,
    driver: object,
    config: object,
    reader_getter: Callable[[], ReplyLogReaderProtocol | None],
    logger: Any,
) -> bool:
    """按当前驱动与配置决定是否挂载 reply 日志 API。"""
    if not getattr(config, "plugin_enable", False):
        logger.info("[LLM Provider] 插件未启用，跳过管理 API 注册")
        return False

    if not getattr(config, "api_enabled", True):
        logger.info("[LLM Provider] 本地 REST 管理接口已禁用，跳过注册")
        return False

    shared_settings = load_shared_management_settings(logger)
    if shared_settings is None:
        return False

    driver_type = getattr(driver, "type", None)
    server_app = getattr(driver, "server_app", None)
    if driver_type != "fastapi" or server_app is None:
        logger.warning("[LLM Provider] 当前驱动不是 FastAPI，无法挂载管理 API")
        return False

    register_llm_provider_api(
        server_app,
        api_token=shared_settings.api_token,
        allowed_origins=shared_settings.allowed_origins,
        reader_getter=reader_getter,
    )
    logger.info("[LLM Provider] 管理 API 已注册: %s", API_PREFIX)
    return True
