"""
Komari Bot Embedding Provider。

提供统一的向量嵌入服务和重排服务。
支持在线 API 兼容格式。
"""

from typing import Any

from nonebot import get_driver, logger
from nonebot.plugin import require

# 这些导入需要放在 require 之上或者按需加载以防止循环依赖
from .config_schema import DynamicConfigSchema
from .embedding_service import EmbeddingService
from .rerank_service import RerankResult, RerankService

try:
    driver = get_driver()
except ValueError:
    driver = None
    config_manager: Any | None = None
else:
    config_manager_plugin = require("config_manager")
    config_manager = config_manager_plugin.get_config_manager(
        "embedding_provider", DynamicConfigSchema
    )


class ProviderState:
    def __init__(self) -> None:
        self.embedding_service: EmbeddingService | None = None
        self.rerank_service: RerankService | None = None


# 全局状态实例
state = ProviderState()


async def _startup() -> None:
    """初始化服务。"""
    if config_manager is None:
        msg = "EmbeddingProvider 只能在 NoneBot 环境中自动初始化"
        raise RuntimeError(msg)
    config = config_manager.get()

    state.embedding_service = EmbeddingService(config)
    state.rerank_service = RerankService(config)

    logger.info("[EmbeddingProvider] 插件启动完成")


async def _shutdown() -> None:
    """清理服务。"""
    if state.embedding_service:
        await state.embedding_service.cleanup()
        state.embedding_service = None

    if state.rerank_service:
        await state.rerank_service.cleanup()
        state.rerank_service = None

    logger.info("[EmbeddingProvider] 插件已关闭")


if driver is not None:

    @driver.on_startup
    async def on_startup() -> None:
        await _startup()

    @driver.on_shutdown
    async def on_shutdown() -> None:
        await _shutdown()


# --- 公共 API ---


async def embed(text: str, instruction: str = "") -> list[float]:
    """生成单条文本嵌入。"""
    if state.embedding_service is None:
        raise RuntimeError("EmbeddingProvider 尚未初始化")  # noqa: TRY003
    return await state.embedding_service.embed(text, instruction=instruction)


async def embed_batch(texts: list[str], instruction: str = "") -> list[list[float]]:
    """批量生成文本嵌入。"""
    if state.embedding_service is None:
        raise RuntimeError("EmbeddingProvider 尚未初始化")  # noqa: TRY003
    return await state.embedding_service.embed_batch(texts, instruction=instruction)


async def rerank(
    query: str,
    documents: list[str],
    top_n: int | None = None,
    instruction: str = "",
) -> list[RerankResult]:
    """对文档集进行重排。"""
    if state.rerank_service is None:
        raise RuntimeError("EmbeddingProvider 尚未初始化")  # noqa: TRY003
    return await state.rerank_service.rerank(
        query, documents, top_n, instruction=instruction
    )


def is_rerank_enabled() -> bool:
    """检查是否启用了 Rerank。"""
    if state.rerank_service is None:
        return False
    return state.rerank_service.enabled


def get_embedding_model() -> str:
    """获取当前生效的 embedding 模型名。"""
    if state.embedding_service is not None:
        return state.embedding_service.config.embedding_model
    if config_manager is not None:
        return config_manager.get().embedding_model
    return DynamicConfigSchema().embedding_model


def get_embedding_dimension() -> int | None:
    """获取当前生效的 embedding 维度配置。"""
    if state.embedding_service is not None:
        return int(state.embedding_service.config.embedding_dimension)
    if config_manager is not None:
        return int(config_manager.get().embedding_dimension)
    return int(DynamicConfigSchema().embedding_dimension)
