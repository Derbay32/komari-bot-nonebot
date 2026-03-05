"""
Komari Bot Embedding Provider。

提供统一的向量嵌入服务和重排服务。
支持本地 fastembed 以及在线 API 兼容格式。
"""

from nonebot import get_driver, logger
from nonebot.plugin import require

# 这些导入需要放在 require 之上或者按需加载以防止循环依赖
from .config_schema import DynamicConfigSchema
from .embedding_service import EmbeddingService
from .rerank_service import RerankResult, RerankService

# 依赖 config_manager 插件
config_manager_plugin = require("config_manager")

# 获取配置管理器
config_manager = config_manager_plugin.get_config_manager(
    "embedding_provider", DynamicConfigSchema
)

driver = get_driver()


class ProviderState:
    def __init__(self) -> None:
        self.embedding_service: EmbeddingService | None = None
        self.rerank_service: RerankService | None = None


# 全局状态实例
state = ProviderState()


@driver.on_startup
async def on_startup() -> None:
    """初始化服务。"""
    config = config_manager.get()

    state.embedding_service = EmbeddingService(config)
    state.rerank_service = RerankService(config)

    logger.info(f"[EmbeddingProvider] 插件启动完成 (模式: {config.embedding_source})")


@driver.on_shutdown
async def on_shutdown() -> None:
    """清理服务。"""
    if state.embedding_service:
        await state.embedding_service.cleanup()
        state.embedding_service = None

    if state.rerank_service:
        await state.rerank_service.cleanup()
        state.rerank_service = None

    logger.info("[EmbeddingProvider] 插件已关闭")


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
    return config_manager.get().embedding_model
