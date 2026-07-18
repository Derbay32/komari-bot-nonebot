"""基于在线 API 的 Rerank 服务。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

import aiohttp
from nonebot import logger

from .request_safety import (
    build_request_timeout,
    content_fingerprint,
    read_bounded_json_response,
    request_with_retry,
)

if TYPE_CHECKING:
    from .config_schema import DynamicConfigSchema


@dataclass(frozen=True, slots=True)
class RerankResult:
    index: int
    relevance_score: float


class RerankResponseValidationError(ValueError):
    """Rerank API 返回结构不满足索引和分数约束。"""


class RerankService:
    """调用在线 Rerank API（兼容 Jina/Cohere 格式）。"""

    def __init__(self, config: DynamicConfigSchema) -> None:
        self.config = config
        self._http_session: aiohttp.ClientSession | None = None

    @property
    def enabled(self) -> bool:
        return self.config.rerank_enabled

    async def _get_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession(
                timeout=build_request_timeout(self.config)
            )
        return self._http_session

    @staticmethod
    async def _post_json(
        session: aiohttp.ClientSession,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object],
        max_response_bytes: int,
    ) -> object:
        async with session.post(url, headers=headers, json=payload) as response:
            response.raise_for_status()
            return await read_bounded_json_response(
                response,
                max_bytes=max_response_bytes,
            )

    async def _request_rerank_payload(
        self,
        *,
        session: aiohttp.ClientSession,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object],
        fallback_payload: dict[str, object] | None,
        request_hash: str,
    ) -> object:
        async def _request_once() -> object:
            try:
                return await self._post_json(
                    session,
                    url=url,
                    headers=headers,
                    payload=payload,
                    max_response_bytes=self.config.response_max_bytes,
                )
            except aiohttp.ClientResponseError as error:
                if fallback_payload is None or error.status not in {400, 422}:
                    raise
                logger.warning(
                    "[EmbeddingProvider] Rerank 服务端拒绝 instruction，"
                    "使用兼容请求: request_hash={} status={}",
                    request_hash,
                    error.status,
                )
                return await self._post_json(
                    session,
                    url=url,
                    headers=headers,
                    payload=fallback_payload,
                    max_response_bytes=self.config.response_max_bytes,
                )

        return await request_with_retry(
            _request_once,
            service_name="rerank_api",
            request_hash=request_hash,
            config=self.config,
        )

    @staticmethod
    def _validate_rerank_response(
        payload: object,
        *,
        document_count: int,
        top_n: int,
    ) -> list[RerankResult]:
        """验证结果条数、索引唯一性/范围及有限分数。"""
        if not isinstance(payload, dict):
            raise RerankResponseValidationError("响应顶层必须是对象")
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            msg = "响应 results 必须是数组"
            raise RerankResponseValidationError(msg)
        if len(raw_results) > top_n:
            msg = f"Rerank 结果超过 top_n：top_n={top_n} actual={len(raw_results)}"
            raise RerankResponseValidationError(msg)

        seen_indices: set[int] = set()
        results: list[RerankResult] = []
        for item in raw_results:
            if not isinstance(item, dict):
                msg = "Rerank 条目必须是对象"
                raise RerankResponseValidationError(msg)
            index = item.get("index")
            if isinstance(index, bool) or not isinstance(index, int):
                msg = "Rerank index 必须是整数"
                raise RerankResponseValidationError(msg)
            if index < 0 or index >= document_count:
                msg = f"Rerank index 越界：index={index} documents={document_count}"
                raise RerankResponseValidationError(msg)
            if index in seen_indices:
                msg = f"Rerank index 重复：index={index}"
                raise RerankResponseValidationError(msg)
            seen_indices.add(index)

            score = item.get("relevance_score")
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                msg = "relevance_score 必须是数值"
                raise RerankResponseValidationError(msg)
            numeric_score = float(score)
            if not math.isfinite(numeric_score):
                msg = "relevance_score 必须是有限数"
                raise RerankResponseValidationError(msg)
            results.append(
                RerankResult(index=index, relevance_score=numeric_score)
            )

        results.sort(key=lambda item: item.relevance_score, reverse=True)
        return results

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int | None = None,
        instruction: str = "",
    ) -> list[RerankResult]:
        """对文档集进行重排。"""
        if not self.enabled:
            return [
                RerankResult(
                    index=index,
                    relevance_score=1.0 - (index / max(len(documents), 1)),
                )
                for index in range(len(documents))
            ]
        if not documents:
            return []

        url = self.config.rerank_api_url
        if not url:
            logger.error("[EmbeddingProvider] rerank_api_url 为空")
            msg = "启用了 rerank，但是 rerank_api_url 为空"
            raise ValueError(msg)

        requested_top_n = top_n if top_n is not None else self.config.rerank_top_n
        result_limit = max(1, min(requested_top_n, len(documents)))
        headers: dict[str, str] = {}
        if self.config.rerank_api_key:
            headers["Authorization"] = f"Bearer {self.config.rerank_api_key}"

        payload: dict[str, object] = {
            "model": self.config.rerank_model,
            "query": query,
            "documents": documents,
            "top_n": result_limit,
        }
        normalized_instruction = instruction.strip()
        fallback_payload: dict[str, object] | None = None
        if normalized_instruction:
            payload["instruction"] = normalized_instruction
            fallback_payload = {
                "model": self.config.rerank_model,
                "query": query,
                "documents": documents,
                "top_n": result_limit,
            }

        request_hash = content_fingerprint(
            [normalized_instruction, query, *documents]
        )
        logger.info(
            "[EmbeddingProvider] 请求 rerank: model={} document_count={} "
            "query_chars={} document_chars={} top_n={} request_hash={}",
            self.config.rerank_model,
            len(documents),
            len(query),
            sum(len(document) for document in documents),
            result_limit,
            request_hash,
        )

        session = await self._get_http_session()
        response_payload = await self._request_rerank_payload(
            session=session,
            url=url,
            headers=headers,
            payload=payload,
            fallback_payload=fallback_payload,
            request_hash=request_hash,
        )
        try:
            return self._validate_rerank_response(
                response_payload,
                document_count=len(documents),
                top_n=result_limit,
            )
        except RerankResponseValidationError as error:
            logger.error(
                "[EmbeddingProvider] rerank 响应校验失败: request_hash={} reason={}",
                request_hash,
                str(error),
            )
            raise

    async def cleanup(self) -> None:
        """释放资源。"""
        session = self._http_session
        self._http_session = None
        if session is not None and not session.closed:
            await session.close()
            logger.debug("[EmbeddingProvider] Rerank HTTP Session 已关闭")


__all__ = ["RerankResponseValidationError", "RerankResult", "RerankService"]
