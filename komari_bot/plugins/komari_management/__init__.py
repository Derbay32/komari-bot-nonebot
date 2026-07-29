"""Komari Management 统一管理接口插件。"""

from __future__ import annotations

from pathlib import Path

from nonebot import get_driver, logger
from nonebot.plugin import PluginMetadata, require

from komari_bot.common.prompt_storage import close_prompt_storage_if_created
from komari_bot.plugins.agent_run_logger.config_schema import (
    AgentRunLoggerConfigSchema,
)
from komari_bot.plugins.embedding_provider.config_schema import (
    DynamicConfigSchema as EmbeddingProviderConfigSchema,
)
from komari_bot.plugins.group_history_summary.config_schema import (
    DynamicConfigSchema as GroupHistorySummaryConfigSchema,
)
from komari_bot.plugins.group_history_summary.prompt_template import (
    DEFAULTS as GROUP_HISTORY_PROMPT_DEFAULTS,
)
from komari_bot.plugins.komari_chat.services.prompt_template import (
    _DEFAULTS as KOMARI_CHAT_PROMPT_DEFAULTS,
)
from komari_bot.plugins.komari_decision.config_schema import KomariDecisionConfigSchema
from komari_bot.plugins.komari_help.config_schema import (
    DynamicConfigSchema as HelpConfigSchema,
)
from komari_bot.plugins.komari_knowledge.config_schema import (
    DynamicConfigSchema as KnowledgeConfigSchema,
)
from komari_bot.plugins.komari_memory.config_schema import KomariMemoryConfigSchema
from komari_bot.plugins.komari_memory.services.summary_prompt_template import (
    DEFAULTS as KOMARI_MEMORY_SUMMARY_PROMPT_DEFAULTS,
)
from komari_bot.plugins.komari_sentry.config_schema import KomariSentryConfigSchema
from komari_bot.plugins.llm_provider.config_schema import (
    DynamicConfigSchema as LlmProviderConfigSchema,
)
from komari_bot.plugins.sr.config_schema import (
    DynamicConfigSchema as SrConfigSchema,
)
from komari_bot.plugins.user_data.config_schema import (
    DynamicConfigSchema as UserDataConfigSchema,
)

from .announcement_repository import close_announcement_dispatch_repository
from .api_runtime import ManagementApiComponents, register_management_api_for_driver
from .config_schema import DynamicConfigSchema, ManagementCredentialSchema
from .managed_resources import ManagedConfigResource, ManagedPromptResource
from .startup_cleanup import cleanup_management_v1_config

config_manager_plugin = require("config_manager")
config_manager = config_manager_plugin.get_config_manager(
    "komari_management",
    DynamicConfigSchema,
)

__plugin_meta__ = PluginMetadata(
    name="komari_management",
    description="统一挂载 Komari 本地管理 API，并复用 FastAPI 官方 Swagger/OpenAPI 文档",
    usage="自动运行，无需命令",
    config=DynamicConfigSchema,
)


class PluginState:
    """插件运行时状态。"""

    def __init__(self) -> None:
        self.api_registered = False


def _load_management_components() -> ManagementApiComponents:
    """加载统一管理 API 所需的业务插件组件。"""
    knowledge_plugin = require("komari_knowledge")
    help_plugin = require("komari_help")
    memory_plugin = require("komari_memory")
    agent_run_logger_plugin = require("agent_run_logger")
    search_plugin = require("komari_search")
    user_ban_plugin = require("user_ban")

    return ManagementApiComponents(
        register_knowledge_api=knowledge_plugin.register_knowledge_api,
        knowledge_engine_getter=knowledge_plugin.get_engine,
        register_help_api=help_plugin.register_help_api,
        help_engine_getter=help_plugin.get_engine,
        register_memory_api=memory_plugin.register_memory_api,
        memory_service_getter=memory_plugin.get_memory_service,
        memory_redis_getter=memory_plugin.get_redis_manager,
        register_agent_run_log_api=agent_run_logger_plugin.register_agent_run_log_api,
        agent_run_log_reader_getter=agent_run_logger_plugin.get_agent_run_log_reader,
        register_search_api=search_plugin.register_search_api,
        register_user_ban_api=user_ban_plugin.register_user_ban_api,
        user_ban_service_getter=user_ban_plugin.get_service,
        config_resources=(
            ManagedConfigResource(
                resource_id="komari_management",
                display_name="Komari Management",
                manager_getter=lambda: config_manager,
            ),
            ManagedConfigResource(
                resource_id="komari_memory",
                display_name="Komari Memory",
                manager_getter=lambda: config_manager_plugin.get_config_manager(
                    "komari_memory",
                    KomariMemoryConfigSchema,
                ),
            ),
            ManagedConfigResource(
                resource_id="komari_knowledge",
                display_name="Komari Knowledge",
                manager_getter=lambda: config_manager_plugin.get_config_manager(
                    "komari_knowledge",
                    KnowledgeConfigSchema,
                ),
            ),
            ManagedConfigResource(
                resource_id="komari_help",
                display_name="Komari Help",
                manager_getter=lambda: config_manager_plugin.get_config_manager(
                    "komari_help",
                    HelpConfigSchema,
                ),
            ),
            ManagedConfigResource(
                resource_id="agent_run_logger",
                display_name="Agent Run Logger",
                manager_getter=lambda: config_manager_plugin.get_config_manager(
                    "agent_run_logger",
                    AgentRunLoggerConfigSchema,
                ),
            ),
            ManagedConfigResource(
                resource_id="llm_provider",
                display_name="LLM Provider",
                manager_getter=lambda: config_manager_plugin.get_config_manager(
                    "llm_provider",
                    LlmProviderConfigSchema,
                ),
            ),
            ManagedConfigResource(
                resource_id="embedding_provider",
                display_name="Embedding Provider",
                manager_getter=lambda: config_manager_plugin.get_config_manager(
                    "embedding_provider",
                    EmbeddingProviderConfigSchema,
                ),
            ),
            ManagedConfigResource(
                resource_id="group_history_summary",
                display_name="Group History Summary",
                manager_getter=lambda: config_manager_plugin.get_config_manager(
                    "group_history_summary",
                    GroupHistorySummaryConfigSchema,
                ),
            ),
            ManagedConfigResource(
                resource_id="komari_decision",
                display_name="Komari Decision",
                manager_getter=lambda: config_manager_plugin.get_config_manager(
                    "komari_decision",
                    KomariDecisionConfigSchema,
                ),
            ),
            ManagedConfigResource(
                resource_id="komari_sentry",
                display_name="Komari Sentry",
                manager_getter=lambda: config_manager_plugin.get_config_manager(
                    "komari_sentry",
                    KomariSentryConfigSchema,
                ),
            ),
            ManagedConfigResource(
                resource_id="sr",
                display_name="SR",
                manager_getter=lambda: config_manager_plugin.get_config_manager(
                    "sr",
                    SrConfigSchema,
                ),
            ),
            ManagedConfigResource(
                resource_id="user_data",
                display_name="User Data",
                manager_getter=lambda: config_manager_plugin.get_config_manager(
                    "user_data",
                    UserDataConfigSchema,
                ),
            ),
        ),
        prompt_resources=(
            ManagedPromptResource(
                resource_id="komari_chat",
                display_name="Komari Chat Prompt",
                defaults=KOMARI_CHAT_PROMPT_DEFAULTS,
                legacy_file_path=Path("config") / "prompts" / "komari_memory.yaml",
            ),
            ManagedPromptResource(
                resource_id="komari_memory_summary",
                display_name="Komari Memory Summary Prompt",
                defaults=KOMARI_MEMORY_SUMMARY_PROMPT_DEFAULTS,
                legacy_file_path=Path("config")
                / "prompts"
                / "komari_memory_summary.yaml",
            ),
            ManagedPromptResource(
                resource_id="group_history_summary",
                display_name="Group History Summary Prompt",
                defaults=GROUP_HISTORY_PROMPT_DEFAULTS,
                legacy_file_path=Path("config")
                / "prompts"
                / "group_history_summary.yaml",
            ),
        ),
    )


driver = get_driver()
state = PluginState()


async def _get_management_credential_source() -> list[ManagementCredentialSchema]:
    """动态返回具名管理凭据配置。"""
    config = await config_manager.get_async()
    return config.api_credentials


state.api_registered = register_management_api_for_driver(
    driver=driver,
    config=config_manager.get(),
    component_loader=_load_management_components,
    logger=logger,
    api_token_getter=_get_management_credential_source,
)


@driver.on_startup
async def _cleanup_management_v1_config() -> None:
    """清理 v1 管理配置残留并提示旧权限名。"""
    await cleanup_management_v1_config(logger=logger)


@driver.on_shutdown
async def _close_management_resources() -> None:
    """关闭管理插件持有的 Prompt 与公告账本资源。"""
    close_prompt_storage_if_created()
    await close_announcement_dispatch_repository()
