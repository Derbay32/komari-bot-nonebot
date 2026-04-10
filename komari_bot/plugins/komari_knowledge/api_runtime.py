"""Komari Knowledge REST API 挂载辅助。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .api import API_PREFIX, KnowledgeEngineProtocol, register_knowledge_api

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


def register_management_api_for_driver(
    *,
    driver: object,
    config: object,
    engine_getter: Callable[[], KnowledgeEngineProtocol | None],
    logger: Any,
) -> bool:
    """按当前驱动与配置决定是否挂载管理 API。"""
    if not getattr(config, "plugin_enable", False):
        logger.info("[Komari Knowledge] 插件未启用，跳过管理 API 注册")
        return False

    if not getattr(config, "api_enabled", True):
        logger.info("[Komari Knowledge] REST 管理接口已禁用，跳过注册")
        return False

    api_token = getattr(config, "api_token", "")
    if not isinstance(api_token, str) or not api_token.strip():
        logger.warning("[Komari Knowledge] 未配置 api_token，跳过管理 API 注册")
        return False

    driver_type = getattr(driver, "type", None)
    server_app = getattr(driver, "server_app", None)
    if driver_type != "fastapi" or server_app is None:
        logger.warning("[Komari Knowledge] 当前驱动不是 FastAPI，无法挂载管理 API")
        return False

    allowed_origins = _normalize_origins(getattr(config, "api_allowed_origins", []))
    register_knowledge_api(
        server_app,
        api_token=api_token.strip(),
        allowed_origins=allowed_origins,
        engine_getter=engine_getter,
    )
    logger.info("[Komari Knowledge] 管理 API 已注册: %s", API_PREFIX)
    return True


def _normalize_origins(raw_value: Any) -> Sequence[str]:
    """归一化 Origin 白名单配置。"""
    if isinstance(raw_value, list):
        return [str(item).strip() for item in raw_value if str(item).strip()]
    if isinstance(raw_value, str):
        return [item.strip() for item in raw_value.split(",") if item.strip()]
    return []
