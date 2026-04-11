"""Komari Management 统一管理接口插件。"""

from __future__ import annotations

from nonebot import get_driver, logger
from nonebot.plugin import PluginMetadata, require

from .api_runtime import ManagementApiComponents, register_management_api_for_driver
from .config_schema import DynamicConfigSchema

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
    memory_plugin = require("komari_memory")
    llm_provider_plugin = require("llm_provider")

    return ManagementApiComponents(
        register_knowledge_api=knowledge_plugin.register_knowledge_api,
        knowledge_engine_getter=knowledge_plugin.get_engine,
        register_memory_api=memory_plugin.register_memory_api,
        memory_service_getter=memory_plugin.get_memory_service,
        register_llm_provider_api=llm_provider_plugin.register_llm_provider_api,
        reply_log_reader_getter=llm_provider_plugin.get_reply_log_reader,
    )


driver = get_driver()
state = PluginState()
state.api_registered = register_management_api_for_driver(
    driver=driver,
    config=config_manager.get(),
    component_loader=_load_management_components,
    logger=logger,
)
