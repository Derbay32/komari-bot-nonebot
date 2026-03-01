"""Komari Bot Embedding Provider 插件配置。"""

from nonebot.plugin import PluginMetadata

from .config_schema import DynamicConfigSchema

__plugin_meta__ = PluginMetadata(
    name="embedding_provider",
    description="Komari Bot 统一的向量嵌入与重排 (Rerank) 服务",
    usage="被其他插件通过 require('embedding_provider') 调用",
    config=DynamicConfigSchema,
)
