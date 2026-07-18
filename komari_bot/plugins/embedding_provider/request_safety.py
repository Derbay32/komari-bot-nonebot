"""Embedding/Rerank 远程请求的超时、重试与脱敏元数据。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import TYPE_CHECKING, Protocol

import aiohttp
from nonebot import logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


class RequestSafetyConfigProtocol(Protocol):
    """远程请求安全配置。"""

    request_connect_timeout_seconds: float
    request_read_timeout_seconds: float
    request_total_timeout_seconds: float
    request_retry_attempts: int
    request_retry_backoff_seconds: float
    response_max_bytes: int


class RemoteServiceRequestError(RuntimeError):
    """不包含 URL、正文或响应内容的稳定远程请求异常。"""


class RemoteResponseTooLargeError(RuntimeError):
    """远程响应声明或实际解压后的正文超过硬上限。"""


class RemoteResponseDecodeError(RuntimeError):
    """远程响应不是合法 JSON。"""


def build_request_timeout(config: RequestSafetyConfigProtocol) -> aiohttp.ClientTimeout:
    """构造同时限制连接、读取和总时长的 aiohttp 超时。"""
    connect_timeout = float(config.request_connect_timeout_seconds)
    return aiohttp.ClientTimeout(
        total=float(config.request_total_timeout_seconds),
        connect=connect_timeout,
        sock_connect=connect_timeout,
        sock_read=float(config.request_read_timeout_seconds),
    )


def content_fingerprint(parts: Iterable[str]) -> str:
    """生成不可逆的长度分隔内容指纹，用于脱敏关联请求。"""
    digest = hashlib.sha256()
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()[:16]


async def read_bounded_json_response(
    response: aiohttp.ClientResponse,
    *,
    max_bytes: int,
) -> object:
    """按解压后的实际字节数流式读取 JSON，避免超大响应占满内存。"""
    if max_bytes <= 0:
        raise ValueError("响应字节上限必须为正整数")
    content_length = response.content_length
    if content_length is not None and content_length > max_bytes:
        raise RemoteResponseTooLargeError("远程响应超过字节上限")

    body = bytearray()
    async for chunk in response.content.iter_chunked(min(65_536, max_bytes + 1)):
        if len(body) + len(chunk) > max_bytes:
            raise RemoteResponseTooLargeError("远程响应超过字节上限")
        body.extend(chunk)
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        message = "远程响应不是合法 JSON"
        raise RemoteResponseDecodeError(message) from exc


def _get_status(error: Exception) -> int | None:
    if isinstance(error, aiohttp.ClientResponseError):
        return error.status
    return None


def _is_retryable(error: Exception) -> bool:
    if isinstance(
        error,
        (
            TimeoutError,
            aiohttp.ClientConnectionError,
            aiohttp.ClientPayloadError,
            aiohttp.ServerTimeoutError,
        ),
    ):
        return True
    status = _get_status(error)
    return status in _RETRYABLE_STATUS_CODES


async def request_with_retry[T](
    operation: Callable[[], Awaitable[T]],
    *,
    service_name: str,
    request_hash: str,
    config: RequestSafetyConfigProtocol,
) -> T:
    """仅重试瞬时网络/限流/服务端故障，并输出脱敏诊断。"""
    max_attempts = max(1, int(config.request_retry_attempts))
    base_delay = max(0.0, float(config.request_retry_backoff_seconds))
    total_deadline = float(config.request_total_timeout_seconds)

    try:
        async with asyncio.timeout(total_deadline):
            for attempt in range(1, max_attempts + 1):
                try:
                    return await operation()
                except Exception as error:
                    status = _get_status(error)
                    if attempt < max_attempts and _is_retryable(error):
                        delay = base_delay * (2 ** (attempt - 1))
                        logger.warning(
                            "[EmbeddingProvider] {} 请求暂时失败，将重试: "
                            "request_hash={} attempt={}/{} error_type={} status={}",
                            service_name,
                            request_hash,
                            attempt,
                            max_attempts,
                            type(error).__name__,
                            status,
                        )
                        if delay > 0:
                            await asyncio.sleep(delay)
                        continue

                    logger.error(
                        "[EmbeddingProvider] {} 请求失败: request_hash={} attempts={} "
                        "error_type={} status={}",
                        service_name,
                        request_hash,
                        attempt,
                        type(error).__name__,
                        status,
                    )
                    msg = f"{service_name} 请求失败（{type(error).__name__}）"
                    raise RemoteServiceRequestError(msg) from None
    except TimeoutError:
        logger.error(
            "[EmbeddingProvider] {} 请求超过业务总时限: request_hash={} "
            "deadline_seconds={}",
            service_name,
            request_hash,
            total_deadline,
        )
        msg = f"{service_name} 请求超时"
        raise RemoteServiceRequestError(msg) from None

    raise AssertionError("远程请求重试循环异常退出")


__all__ = [
    "RemoteResponseDecodeError",
    "RemoteResponseTooLargeError",
    "RemoteServiceRequestError",
    "RequestSafetyConfigProtocol",
    "build_request_timeout",
    "content_fingerprint",
    "read_bounded_json_response",
    "request_with_retry",
]
