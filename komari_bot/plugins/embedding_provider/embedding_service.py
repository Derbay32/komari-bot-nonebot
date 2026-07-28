"""统一的向量嵌入服务。"""

import asyncio
from pathlib import Path
from typing import Any, Protocol

import aiohttp
from nonebot import logger


class EmbeddingConfigProtocol(Protocol):
    """EmbeddingService 运行所需的最小配置接口。"""

    embedding_source: str
    embedding_model: str
    embedding_api_url: str
    embedding_api_key: str
    embedding_dimension: int


class EmbeddingService:
    """提供文本嵌入服务，支持本地 fastembed 和远程 OpenAI 兼容 API。"""

    def __init__(self, config: EmbeddingConfigProtocol) -> None:
        self.config = config
        self._embed_model: Any = None
        self._http_session: aiohttp.ClientSession | None = None

    @property
    def source(self) -> str:
        return self.config.embedding_source

    async def _get_local_model(self) -> Any:
        # 延迟加载
        if self._embed_model is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as e:
                msg = "使用 local 模式需安装 fastembed，请运行 pip install fastembed"
                raise ImportError(msg) from e

            cache_dir = Path.home() / ".cache" / "komari_embeddings"
            cache_dir.mkdir(parents=True, exist_ok=True)

            loop = asyncio.get_running_loop()
            self._embed_model = await loop.run_in_executor(
                None,
                lambda: TextEmbedding(
                    model_name=self.config.embedding_model,
                    cache_dir=str(cache_dir),
                ),
            )
            logger.info(f"[EmbeddingProvider] 本地向量模型加载完成 (缓存: {cache_dir})")
        return self._embed_model

    async def _get_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    @staticmethod
    def _merge_instruction(instruction: str, text: str) -> str:
        """将 instruction 与输入文本合并（本地模型回退用）。"""
        instruction_clean = instruction.strip()
        text_clean = text.strip()
        if not instruction_clean:
            return text_clean
        return (
            "<instruction>\n"
            f"{instruction_clean}\n"
            "</instruction>\n"
            "<input>\n"
            f"{text_clean}\n"
            "</input>"
        )

    async def embed(self, text: str, instruction: str = "") -> list[float]:
        """生成单条文本嵌入。"""
        vectors = await self.embed_batch([text], instruction=instruction)
        if not vectors:
            logger.error("[EmbeddingProvider] embed failed to return vectors.")
            raise RuntimeError("Embed failed to return vectors.")  # noqa: TRY003
        return vectors[0]

    async def embed_batch(
        self, texts: list[str], instruction: str = ""
    ) -> list[list[float]]:
        """批量生成文本嵌入。"""
        if not texts:
            return []

        if self.source == "api":
            return await self._embed_api(texts, instruction=instruction)
        return await self._embed_local(texts, instruction=instruction)

    async def _embed_local(
        self, texts: list[str], instruction: str = ""
    ) -> list[list[float]]:
        model = await self._get_local_model()
        loop = asyncio.get_running_loop()
        input_texts = (
            [self._merge_instruction(instruction, text) for text in texts]
            if instruction.strip()
            else texts
        )
        embeddings = await loop.run_in_executor(
            None, lambda: list(model.embed(input_texts))
        )
        return [emb.tolist() for emb in embeddings]

    async def _embed_api(
        self, texts: list[str], instruction: str = ""
    ) -> list[list[float]]:
        url = self.config.embedding_api_url
        if not url:
            logger.error("[EmbeddingProvider] embedding_api_url 为空")
            raise ValueError("配置了 API 模式但是 embedding_api_url 为空")  # noqa: TRY003

        headers = {}
        if self.config.embedding_api_key:
            headers["Authorization"] = f"Bearer {self.config.embedding_api_key}"

        payload = {
            "model": self.config.embedding_model,
            "input": texts,
        }
        if getattr(self.config, "embedding_dimension", None):
            payload["dimensions"] = self.config.embedding_dimension
        if instruction.strip():
            payload["instruction"] = instruction.strip()

        session = await self._get_http_session()
        logger.info(
            f"[EmbeddingProvider] 正在请求 API 生成 {len(texts)} 条文本的嵌入向量 (Model: {self.config.embedding_model})"
        )

        try:
            async with session.post(url, headers=headers, json=payload) as resp:
                try:
                    resp.raise_for_status()
                except aiohttp.ClientResponseError as e:
                    if (
                        instruction.strip()
                        and e.status in {400, 422}
                        and "instruction" in payload
                    ):
                        logger.warning(
                            "[EmbeddingProvider] 服务端可能不支持 instruction，降级重试"
                        )
                        fallback_payload = {
                            "model": self.config.embedding_model,
                            "input": texts,
                        }
                        if getattr(self.config, "embedding_dimension", None):
                            fallback_payload["dimensions"] = (
                                self.config.embedding_dimension
                            )
                        async with session.post(
                            url, headers=headers, json=fallback_payload
                        ) as fallback_resp:
                            fallback_resp.raise_for_status()
                            fallback_data = await fallback_resp.json()
                            return [
                                item.get("embedding", [])
                                for item in fallback_data.get("data", [])
                            ]
                    error_body = await resp.text()
                    logger.error(
                        f"[EmbeddingAPI] Error {e.status}: {error_body}. Payload: {payload}"
                    )
                    raise
                data = await resp.json()

                # OpenAI format: {"data": [{"embedding": [...]}, ...]}
                return [item.get("embedding", []) for item in data.get("data", [])]
        except Exception as e:
            logger.exception(f"[EmbeddingProvider] 向量嵌入 API 调用失败: {e}")
            raise

    async def cleanup(self) -> None:
        """释放资源。"""
        if self._embed_model is not None:
            self._embed_model = None
            logger.debug("[EmbeddingProvider] 本地向量模型已释放")

        if self._http_session is not None and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None
            logger.debug("[EmbeddingProvider] HTTP Session 已关闭")
