"""JRHG 配置访问接口。"""

from nonebot.plugin import require

from .config_schemas import DynamicConfigSchema

config_manager_plugin = require("config_manager")

_config_manager = config_manager_plugin.get_config_manager(
    "jrhg",
    DynamicConfigSchema,
)


def get_config() -> DynamicConfigSchema:
    """获取当前 JRHG 配置。"""
    return _config_manager.get()
