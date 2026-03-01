"""基于在线 API 的 Rerank 服务。"""

from dataclasses import dataclass

import aiohttp
from nonebot import logger

from .config_schema import DynamicConfigSchema


@dataclass
class RerankResult:
    index: int
    relevance_score: float


class RerankService:
    """调用在线 Rerank API (兼容 Jina/Cohere 格式)。"""

    def __init__(self, config: DynamicConfigSchema) -> None:
        self.config = config
        self._http_session: aiohttp.ClientSession | None = None

    @property
    def enabled(self) -> bool:
        return self.config.rerank_enabled

    async def _get_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    async def rerank(
        self, query: str, documents: list[str], top_n: int | None = None
    ) -> list[RerankResult]:
        """对文档集进行重排。"""
        if not self.enabled:
            # 如果未启用，则返回原始顺序并赋予降序伪分数
            return [
                RerankResult(
                    index=i, relevance_score=1.0 - (i / max(len(documents), 1))
                )
                for i in range(len(documents))
            ]
        if not documents:
            return []
        url = self.config.rerank_api_url
        if not url:
            logger.error("[EmbeddingProvider] rerank_api_url 为空")
            raise ValueError("启用了 rerank，但是 rerank_api_url 为空")  # noqa: TRY003
        n = top_n if top_n is not None else self.config.rerank_top_n
        # 不能超过文档数
        n = min(n, len(documents))
        headers = {}
        if self.config.rerank_api_key:
            headers["Authorization"] = f"Bearer {self.config.rerank_api_key}"
        payload = {
            "model": self.config.rerank_model,
            "query": query,
            "documents": documents,
            "top_n": n,
        }
        session = await self._get_http_session()
        logger.info(
            f"[EmbeddingProvider] 正在使用 {self.config.rerank_model} 对 {len(documents)} 个文档进行重排 (Query: '{query}')"
        )
        try:
            async with session.post(url, headers=headers, json=payload) as resp:
                resp.raise_for_status()
                data = await resp.json()
                # Jina/Cohere format: {"results": [{"index": 0, "relevance_score": 0.95}, ...]}
                results = [
                    RerankResult(
                        index=item.get("index", 0),
                        relevance_score=item.get("relevance_score", 0.0),
                    )
                    for item in data.get("results", [])
                ]

                # 按分数的降序排列以防万一
                results.sort(key=lambda x: x.relevance_score, reverse=True)
                return results
        except Exception as e:
            logger.exception(f"[EmbeddingProvider] Rerank API 调用失败: {e}")
            raise

    async def cleanup(self) -> None:
        """释放资源。"""
        if self._http_session is not None and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None
            logger.debug("[EmbeddingProvider] Rerank HTTP Session 已关闭")
