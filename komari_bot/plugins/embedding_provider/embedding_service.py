"""统一的向量嵌入服务。"""

from __future__ import annotations

import math
from typing import Protocol

import aiohttp
from nonebot import logger

from .request_safety import (
    RequestSafetyConfigProtocol,
    build_request_timeout,
    content_fingerprint,
    request_with_retry,
)


class EmbeddingConfigProtocol(RequestSafetyConfigProtocol, Protocol):
    """EmbeddingService 运行所需的最小配置接口。"""

    embedding_model: str
    embedding_api_url: str
    embedding_api_key: str
    embedding_dimension: int


class EmbeddingResponseValidationError(ValueError):
    """Embedding API 返回结构不满足一一对应约束。"""


class EmbeddingService:
    """提供基于远程 OpenAI 兼容 API 的文本嵌入服务。"""

    def __init__(self, config: EmbeddingConfigProtocol) -> None:
        self.config = config
        self._http_session: aiohttp.ClientSession | None = None

    async def _get_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession(
                timeout=build_request_timeout(self.config)
            )
        return self._http_session

    async def embed(self, text: str, instruction: str = "") -> list[float]:
        """生成单条文本嵌入。"""
        vectors = await self.embed_batch([text], instruction=instruction)
        if not vectors:
            logger.error("[EmbeddingProvider] 单条 embedding 未返回向量")
            msg = "单条 embedding 未返回向量"
            raise EmbeddingResponseValidationError(msg)
        return vectors[0]

    async def embed_batch(
        self,
        texts: list[str],
        instruction: str = "",
    ) -> list[list[float]]:
        """批量生成文本嵌入。"""
        if not texts:
            return []
        return await self._embed_api(texts, instruction=instruction)

    @staticmethod
    async def _post_json(
        session: aiohttp.ClientSession,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object],
    ) -> object:
        async with session.post(url, headers=headers, json=payload) as response:
            response.raise_for_status()
            return await response.json()

    async def _request_embedding_payload(
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
                )
            except aiohttp.ClientResponseError as error:
                if fallback_payload is None or error.status not in {400, 422}:
                    raise
                logger.warning(
                    "[EmbeddingProvider] 服务端拒绝 instruction，使用兼容请求: "
                    "request_hash={} status={}",
                    request_hash,
                    error.status,
                )
                return await self._post_json(
                    session,
                    url=url,
                    headers=headers,
                    payload=fallback_payload,
                )

        return await request_with_retry(
            _request_once,
            service_name="embedding_api",
            request_hash=request_hash,
            config=self.config,
        )

    @staticmethod
    def _validate_embedding_response(
        payload: object,
        *,
        expected_count: int,
        expected_dimension: int,
    ) -> list[list[float]]:
        """验证向量数量、索引顺序、维度和有限数值。"""
        if not isinstance(payload, dict):
            raise EmbeddingResponseValidationError("响应顶层必须是对象")
        data = payload.get("data")
        if not isinstance(data, list):
            msg = "响应 data 必须是数组"
            raise EmbeddingResponseValidationError(msg)
        if len(data) != expected_count:
            msg = f"向量数量不匹配：expected={expected_count} actual={len(data)}"
            raise EmbeddingResponseValidationError(msg)

        vectors: list[list[float]] = []
        for expected_index, item in enumerate(data):
            if not isinstance(item, dict):
                raise EmbeddingResponseValidationError("向量条目必须是对象")

            index = item.get("index")
            if isinstance(index, bool) or not isinstance(index, int):
                msg = "向量 index 必须是整数"
                raise EmbeddingResponseValidationError(msg)
            if index != expected_index:
                msg = (
                    "向量 index 必须唯一且按输入顺序排列："
                    f"expected={expected_index} actual={index}"
                )
                raise EmbeddingResponseValidationError(msg)

            raw_vector = item.get("embedding")
            if not isinstance(raw_vector, list):
                msg = "embedding 必须是数组"
                raise EmbeddingResponseValidationError(msg)
            if len(raw_vector) != expected_dimension:
                msg = (
                    "向量维度不匹配："
                    f"expected={expected_dimension} actual={len(raw_vector)}"
                )
                raise EmbeddingResponseValidationError(msg)

            vector: list[float] = []
            for value in raw_vector:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise EmbeddingResponseValidationError("向量元素必须是数值")
                numeric_value = float(value)
                if not math.isfinite(numeric_value):
                    raise EmbeddingResponseValidationError("向量元素必须是有限数")
                vector.append(numeric_value)
            vectors.append(vector)

        return vectors

    async def _embed_api(
        self,
        texts: list[str],
        instruction: str = "",
    ) -> list[list[float]]:
        url = self.config.embedding_api_url
        if not url:
            logger.error("[EmbeddingProvider] embedding_api_url 为空")
            msg = "配置了 API 模式但是 embedding_api_url 为空"
            raise ValueError(msg)

        headers: dict[str, str] = {}
        if self.config.embedding_api_key:
            headers["Authorization"] = f"Bearer {self.config.embedding_api_key}"

        payload: dict[str, object] = {
            "model": self.config.embedding_model,
            "input": texts,
            "dimensions": self.config.embedding_dimension,
        }
        normalized_instruction = instruction.strip()
        fallback_payload: dict[str, object] | None = None
        if normalized_instruction:
            payload["instruction"] = normalized_instruction
            fallback_payload = {
                "model": self.config.embedding_model,
                "input": texts,
                "dimensions": self.config.embedding_dimension,
            }

        request_hash = content_fingerprint([normalized_instruction, *texts])
        logger.info(
            "[EmbeddingProvider] 请求 embedding: model={} input_count={} "
            "input_chars={} request_hash={}",
            self.config.embedding_model,
            len(texts),
            sum(len(text) for text in texts),
            request_hash,
        )

        session = await self._get_http_session()
        response_payload = await self._request_embedding_payload(
            session=session,
            url=url,
            headers=headers,
            payload=payload,
            fallback_payload=fallback_payload,
            request_hash=request_hash,
        )
        try:
            return self._validate_embedding_response(
                response_payload,
                expected_count=len(texts),
                expected_dimension=self.config.embedding_dimension,
            )
        except EmbeddingResponseValidationError as error:
            logger.error(
                "[EmbeddingProvider] embedding 响应校验失败: request_hash={} reason={}",
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
            logger.debug("[EmbeddingProvider] HTTP Session 已关闭")


__all__ = [
    "EmbeddingConfigProtocol",
    "EmbeddingResponseValidationError",
    "EmbeddingService",
]
