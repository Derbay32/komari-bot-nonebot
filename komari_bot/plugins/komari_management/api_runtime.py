"""Komari Management 统一管理 API 挂载辅助。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI

from komari_bot.management.management_api import resolve_management_settings

from .announce_api import register_announce_api
from .config_api import register_config_api
from .dsh_api import API_PREFIX as AGENT_API_PREFIX
from .dsh_api import register_dsh_management_api
from .prompt_api import register_prompt_api
from .reply_fulfillment_api import register_reply_fulfillment_api

KNOWLEDGE_API_PREFIX = "/api/v2/komari-knowledge"
HELP_API_PREFIX = "/api/v2/komari-help"
SEARCH_API_PREFIX = "/api/v2/komari-search"
ANNOUNCE_API_PREFIX = "/api/v2/komari-announce"
MANAGEMENT_CONFIG_API_PREFIX = "/api/v2/komari-management-config"
MANAGEMENT_PROMPT_API_PREFIX = "/api/v2/komari-management-prompt"
USER_BAN_API_PREFIX = "/api/v2/komari-user-bans"
REPLY_FULFILLMENT_API_PREFIX = "/api/v2/reply-fulfillments"
GROUP_ADMISSION_API_PREFIX = "/api/v2/group-admission"

if TYPE_CHECKING:
    from collections.abc import Callable

    from komari_bot.management.management_api import (
        ManagementTokenSource,
    )

    from .config_schema import ManagementConfig
    from .managed_resources import ManagedConfigResource, ManagedPromptResource


@dataclass(frozen=True, slots=True)
class ManagementApiComponents:
    """统一管理 API 注册所需组件。"""

    register_knowledge_api: Callable[..., None]
    knowledge_engine_getter: Callable[[], object | None]
    register_help_api: Callable[..., None]
    help_engine_getter: Callable[[], object | None]
    register_search_api: Callable[..., None]
    register_user_ban_api: Callable[..., None]
    user_ban_service_getter: Callable[[], object]
    register_group_admission_api: Callable[..., None]
    reply_fulfillment_service_getter: Callable[[], object | None]
    config_resources: tuple[ManagedConfigResource, ...]
    prompt_resources: tuple[ManagedPromptResource, ...]
    register_character_binding_repair_api: Callable[..., None]
    character_binding_repair_service_getter: Callable[[], object | None]
    register_roulette_management_api: Callable[..., None]
    roulette_observation_getter: Callable[[], object | None]
    register_decision_api: Callable[..., None]
    dsh_management_client_getter: Callable[[], object | None] | None = None


def register_management_api_for_driver(
    *,
    driver: object,
    config: ManagementConfig,
    component_loader: Callable[[], ManagementApiComponents],
    logger: Any,
) -> bool:
    """按驱动与集中配置决定是否挂载统一管理 API。"""
    if not config.komari_management_plugin_enable:
        logger.info("[Komari Management] 插件未启用，跳过管理 API 注册")
        return False

    settings = resolve_management_settings(
        credentials=config.komari_management_api_credentials,
        allowed_origins=config.komari_management_api_allowed_origins,
        logger=logger,
        warning_prefix="[Komari Management]",
    )
    if settings is None:
        return False

    server_app = getattr(driver, "server_app", None)
    # Combined drivers expose names such as "fastapi+httpx+websockets".
    if not isinstance(server_app, FastAPI):
        logger.warning("[Komari Management] 当前驱动不是 FastAPI，无法挂载管理 API")
        return False

    components = component_loader()
    token_source: ManagementTokenSource = settings.credential_source
    components.register_knowledge_api(
        server_app,
        api_token=token_source,
        allowed_origins=settings.allowed_origins,
        engine_getter=components.knowledge_engine_getter,
    )
    components.register_help_api(
        server_app,
        api_token=token_source,
        allowed_origins=settings.allowed_origins,
        engine_getter=components.help_engine_getter,
    )
    components.register_search_api(
        server_app,
        api_token=token_source,
        allowed_origins=settings.allowed_origins,
    )
    components.register_user_ban_api(
        server_app,
        api_token=token_source,
        allowed_origins=settings.allowed_origins,
        service_getter=components.user_ban_service_getter,
    )
    components.register_group_admission_api(
        server_app,
        api_token=token_source,
        allowed_origins=settings.allowed_origins,
    )
    components.register_character_binding_repair_api(
        server_app,
        api_token=token_source,
        allowed_origins=settings.allowed_origins,
        service_getter=components.character_binding_repair_service_getter,
    )
    components.register_roulette_management_api(
        server_app,
        api_token=token_source,
        allowed_origins=settings.allowed_origins,
        observation_getter=components.roulette_observation_getter,
    )
    components.register_decision_api(
        server_app,
        api_token=token_source,
        allowed_origins=settings.allowed_origins,
    )
    register_reply_fulfillment_api(
        server_app,
        api_token=token_source,
        allowed_origins=settings.allowed_origins,
        service_getter=components.reply_fulfillment_service_getter,
    )
    register_config_api(
        server_app,
        api_token=token_source,
        allowed_origins=settings.allowed_origins,
        resources=components.config_resources,
    )
    register_prompt_api(
        server_app,
        api_token=token_source,
        allowed_origins=settings.allowed_origins,
        resources=components.prompt_resources,
    )
    register_dsh_management_api(
        server_app,
        api_token=token_source,
        allowed_origins=settings.allowed_origins,
        client_getter=components.dsh_management_client_getter or (lambda: None),
    )
    register_announce_api(
        server_app,
        api_token=token_source,
        allowed_origins=settings.allowed_origins,
        status_page_url=config.komari_management_announce_status_page_url,
        announce_max_group_count=config.komari_management_announce_max_group_count,
        announce_send_interval_seconds=config.komari_management_announce_send_interval_seconds,
        announce_request_cooldown_seconds=config.komari_management_announce_request_cooldown_seconds,
    )
    docs_url = getattr(server_app, "docs_url", None) or "未启用"
    openapi_url = getattr(server_app, "openapi_url", None) or "未启用"
    logger.info(
        "[Komari Management] 管理 API 已注册: "
        f"{KNOWLEDGE_API_PREFIX}, {HELP_API_PREFIX}, "
        f"{SEARCH_API_PREFIX}, "
        f"{MANAGEMENT_CONFIG_API_PREFIX}, {MANAGEMENT_PROMPT_API_PREFIX}, {ANNOUNCE_API_PREFIX}, "
        f"{AGENT_API_PREFIX}, "
        f"{USER_BAN_API_PREFIX}, "
        f"{GROUP_ADMISSION_API_PREFIX}, "
        f"{REPLY_FULFILLMENT_API_PREFIX}"
    )
    logger.info(
        f"[Komari Management] 管理文档入口: docs={docs_url}, openapi={openapi_url}"
    )
    return True
