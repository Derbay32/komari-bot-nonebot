"""受资源预算与 SSRF 边界保护的远程图片下载工具。"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import re
import socket
import warnings
from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from nonebot import logger
from PIL import Image, UnidentifiedImageError

if TYPE_CHECKING:
    from collections.abc import Iterable

_DEFAULT_MAX_IMAGE_COUNT = 4
_DEFAULT_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_DEFAULT_MAX_TOTAL_BYTES = 20 * 1024 * 1024
_DEFAULT_MAX_PIXELS = 40_000_000
_DEFAULT_DOWNLOAD_CONCURRENCY = 2
_DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
_DEFAULT_READ_TIMEOUT_SECONDS = 30.0
_DEFAULT_TOTAL_TIMEOUT_SECONDS = 45.0
_READ_CHUNK_SIZE = 64 * 1024
_DOWNLOAD_RETRY_ATTEMPTS = 3
_DOWNLOAD_RETRY_BASE_DELAY = 0.2
_DOWNLOAD_RETRY_MAX_DELAY = 1.0
_MAX_REDIRECTS = 3
_MAX_ANIMATION_FRAMES = 100
_ALLOWED_PORTS = frozenset({80, 443})
_RETRYABLE_STATUS_CODES = frozenset({404, 408, 425, 429, 500, 502, 503, 504})
_DIRECT_IMAGE_SOURCE_RE = re.compile(r"^https?://", re.IGNORECASE)
_FORMAT_MIME_TYPES = {
    "GIF": "image/gif",
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}


def _read_config_int(config: object, field_name: str, default: int) -> int:
    return int(getattr(config, field_name, default))


def _read_config_float(config: object, field_name: str, default: float) -> float:
    return float(getattr(config, field_name, default))


@dataclass(frozen=True)
class ImageDownloadPolicy:
    """单条消息的图片下载资源预算。"""

    max_images: int = _DEFAULT_MAX_IMAGE_COUNT
    max_image_bytes: int = _DEFAULT_MAX_IMAGE_BYTES
    max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES
    max_pixels: int = _DEFAULT_MAX_PIXELS
    concurrency: int = _DEFAULT_DOWNLOAD_CONCURRENCY
    connect_timeout_seconds: float = _DEFAULT_CONNECT_TIMEOUT_SECONDS
    read_timeout_seconds: float = _DEFAULT_READ_TIMEOUT_SECONDS
    total_timeout_seconds: float = _DEFAULT_TOTAL_TIMEOUT_SECONDS

    @classmethod
    def from_config(cls, config: object) -> ImageDownloadPolicy:
        """从动态配置构建策略，并兼容尚未包含新字段的测试替身。"""
        max_total_bytes = _read_config_int(
            config,
            "vision_image_download_total_max_bytes",
            _DEFAULT_MAX_TOTAL_BYTES,
        )
        return cls(
            max_images=_read_config_int(
                config,
                "vision_image_download_max_count",
                _DEFAULT_MAX_IMAGE_COUNT,
            ),
            max_image_bytes=min(
                _read_config_int(
                    config,
                    "vision_image_download_max_bytes",
                    _DEFAULT_MAX_IMAGE_BYTES,
                ),
                max_total_bytes,
            ),
            max_total_bytes=max_total_bytes,
            max_pixels=_read_config_int(
                config,
                "vision_image_download_max_pixels",
                _DEFAULT_MAX_PIXELS,
            ),
            concurrency=_read_config_int(
                config,
                "vision_image_download_concurrency",
                _DEFAULT_DOWNLOAD_CONCURRENCY,
            ),
            connect_timeout_seconds=_read_config_float(
                config,
                "vision_image_download_connect_timeout_seconds",
                _DEFAULT_CONNECT_TIMEOUT_SECONDS,
            ),
            read_timeout_seconds=_read_config_float(
                config,
                "vision_image_download_read_timeout_seconds",
                _DEFAULT_READ_TIMEOUT_SECONDS,
            ),
            total_timeout_seconds=_read_config_float(
                config,
                "vision_image_download_total_timeout_seconds",
                _DEFAULT_TOTAL_TIMEOUT_SECONDS,
            ),
        )


@dataclass(frozen=True)
class _DownloadOutcome:
    data_uri: str | None = None
    retry_reason: str | None = None
    redirect_url: str | None = None
    should_abort: bool = False


class _DownloadBudget:
    """在并发下载间原子共享响应体字节预算。"""

    def __init__(self, max_total_bytes: int) -> None:
        self.max_total_bytes = max_total_bytes
        self.consumed_bytes = 0
        self._lock = asyncio.Lock()

    async def consume(self, byte_count: int) -> bool:
        async with self._lock:
            if self.consumed_bytes + byte_count > self.max_total_bytes:
                return False
            self.consumed_bytes += byte_count
            return True


class _PublicAddressResolver(AbstractResolver):
    """在 aiohttp 实际建连阶段只返回经过校验的公网地址。"""

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        address_infos = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            port,
            family=family,
            type=socket.SOCK_STREAM,
        )
        results: list[ResolveResult] = []
        seen: set[tuple[socket.AddressFamily, str]] = set()
        has_blocked_address = False

        for address_family, _socket_type, protocol, _canonical_name, address in (
            address_infos
        ):
            raw_ip = address[0]
            try:
                ip = ipaddress.ip_address(raw_ip)
            except ValueError:
                continue
            if _is_blocked_ip(ip):
                has_blocked_address = True
                continue

            key = (address_family, str(ip))
            if key in seen:
                continue
            seen.add(key)
            results.append(
                ResolveResult(
                    hostname=host,
                    host=str(ip),
                    port=port,
                    family=address_family,
                    proto=protocol,
                    flags=0,
                )
            )

        if has_blocked_address:
            raise OSError("目标主机同时解析到内网或保留地址")
        if not results:
            raise OSError("目标主机未解析到可用公网地址")
        return results

    async def close(self) -> None:
        return None


@runtime_checkable
class _SegmentWithData(Protocol):
    data: object


@runtime_checkable
class _SegmentWithType(Protocol):
    type: object


def _extract_segment_data(segment: object) -> dict[str, Any]:
    if isinstance(segment, _SegmentWithData) and isinstance(segment.data, dict):
        return segment.data
    if isinstance(segment, dict):
        data = segment.get("data")
        if isinstance(data, dict):
            return data
    return {}


def _extract_segment_type(segment: object) -> str:
    if isinstance(segment, _SegmentWithType):
        return str(segment.type)
    if isinstance(segment, dict):
        return str(segment.get("type", ""))
    return ""


def _normalize_image_source(value: object) -> str | None:
    text = str(value or "").strip()
    if not text or not _DIRECT_IMAGE_SOURCE_RE.match(text):
        return None
    return text


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not ip.is_global or ip.is_multicast


def _safe_url_label(url: str) -> str:
    """生成不含路径、查询参数和用户信息的日志标签。"""
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname or "<unknown>"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme.lower()}://{hostname}{port}"
    except ValueError:
        return "<invalid-url>"


async def _validate_download_url(url: str) -> bool:
    """校验 URL 结构与字面地址；域名地址在实际建连时校验。"""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        logger.warning("[ImageDownloader] 图片 URL 解析失败: {}", exc)
        return False

    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(ord(character) < 32 for character in url)
    ):
        logger.warning(
            "[ImageDownloader] 拒绝不支持的图片 URL: url={}",
            _safe_url_label(url),
        )
        return False

    effective_port = port or (443 if parsed.scheme.lower() == "https" else 80)
    if effective_port not in _ALLOWED_PORTS:
        logger.warning(
            "[ImageDownloader] 拒绝非标准端口图片 URL: url={}",
            _safe_url_label(url),
        )
        return False

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        logger.warning(
            "[ImageDownloader] 拒绝本地主机图片 URL: url={}",
            _safe_url_label(url),
        )
        return False

    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None

    if literal_ip is not None and _is_blocked_ip(literal_ip):
        logger.warning(
            "[ImageDownloader] 拒绝内网或保留地址图片: url={}",
            _safe_url_label(url),
        )
        return False
    return True


def extract_image_sources(message: Iterable[object]) -> tuple[list[str], int]:
    """从消息段中提取可下载图片来源及图片段总数。"""
    sources: list[str] = []
    image_count = 0

    for segment in message:
        if _extract_segment_type(segment) != "image":
            continue

        image_count += 1
        data = _extract_segment_data(segment)
        for key in ("url", "file"):
            source = _normalize_image_source(data.get(key))
            if source is not None:
                sources.append(source)
                break

    return sources, image_count


def _get_retry_delay(attempt: int) -> float:
    return min(
        _DOWNLOAD_RETRY_BASE_DELAY * (2 ** (attempt - 1)),
        _DOWNLOAD_RETRY_MAX_DELAY,
    )


def _detect_image_mime_type(data: bytes, max_pixels: int) -> str | None:
    """按真实文件内容验图并解码全部帧，返回可信 MIME 类型。"""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                image_format = image.format
                mime_type = _FORMAT_MIME_TYPES.get(image_format or "")
                if mime_type is None:
                    return None

                width, height = image.size
                frame_count = int(getattr(image, "n_frames", 1))
                if (
                    width <= 0
                    or height <= 0
                    or frame_count <= 0
                    or frame_count > _MAX_ANIMATION_FRAMES
                    or width * height * frame_count > max_pixels
                ):
                    return None
                image.verify()

            with Image.open(BytesIO(data)) as decoded_image:
                if decoded_image.format != image_format:
                    return None
                for frame_index in range(frame_count):
                    decoded_image.seek(frame_index)
                    decoded_image.load()
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        ValueError,
    ):
        return None
    return mime_type


async def _read_image_bytes(
    resp: aiohttp.ClientResponse,
    url: str,
    policy: ImageDownloadPolicy,
    budget: _DownloadBudget,
) -> bytes | None:
    """分块读取响应体，并执行单图及整条消息的共享字节限制。"""
    content_length = resp.content_length
    if content_length is not None and content_length > policy.max_image_bytes:
        logger.warning(
            "[ImageDownloader] 图片声明大小超过单图上限: bytes={} url={}",
            content_length,
            _safe_url_label(url),
        )
        return None

    buffer = bytearray()
    async for chunk in resp.content.iter_chunked(_READ_CHUNK_SIZE):
        next_size = len(buffer) + len(chunk)
        if next_size > policy.max_image_bytes:
            logger.warning(
                "[ImageDownloader] 图片响应体超过单图上限: bytes={} url={}",
                next_size,
                _safe_url_label(url),
            )
            return None
        if not await budget.consume(len(chunk)):
            logger.warning(
                "[ImageDownloader] 单条消息图片响应体超过总上限: url={}",
                _safe_url_label(url),
            )
            return None
        buffer.extend(chunk)

    return bytes(buffer)


async def _handle_download_response(
    resp: aiohttp.ClientResponse,
    url: str,
    attempt: int,
    policy: ImageDownloadPolicy,
    budget: _DownloadBudget,
) -> _DownloadOutcome:
    if resp.status != 200:
        return _handle_non_success_response(resp, url, attempt)

    data = await _read_image_bytes(resp, url, policy, budget)
    if data is None:
        return _DownloadOutcome(should_abort=True)
    if not data:
        if attempt < _DOWNLOAD_RETRY_ATTEMPTS:
            return _DownloadOutcome(retry_reason="empty body")
        logger.warning(
            "[ImageDownloader] 图片内容为空: url={}",
            _safe_url_label(url),
        )
        return _DownloadOutcome(should_abort=True)

    mime_type = await asyncio.to_thread(
        _detect_image_mime_type,
        data,
        policy.max_pixels,
    )
    if mime_type is None:
        logger.warning(
            "[ImageDownloader] 图片格式、完整性或像素规模校验失败: url={}",
            _safe_url_label(url),
        )
        return _DownloadOutcome(should_abort=True)

    encoded = base64.b64encode(data).decode("ascii")
    return _DownloadOutcome(data_uri=f"data:{mime_type};base64,{encoded}")


def _handle_non_success_response(
    resp: aiohttp.ClientResponse,
    url: str,
    attempt: int,
) -> _DownloadOutcome:
    if 300 <= resp.status < 400:
        location = resp.headers.get("Location")
        if not location:
            logger.warning(
                "[ImageDownloader] 重定向缺少 Location: url={}",
                _safe_url_label(url),
            )
            return _DownloadOutcome(should_abort=True)
        return _DownloadOutcome(redirect_url=urljoin(str(resp.url), location))

    if resp.status in _RETRYABLE_STATUS_CODES and attempt < _DOWNLOAD_RETRY_ATTEMPTS:
        return _DownloadOutcome(retry_reason=f"HTTP {resp.status}")

    logger.warning(
        "[ImageDownloader] 下载失败: HTTP {} url={}",
        resp.status,
        _safe_url_label(url),
    )
    return _DownloadOutcome(should_abort=True)


async def _download_single_image(
    session: aiohttp.ClientSession,
    url: str,
    policy: ImageDownloadPolicy | None = None,
    budget: _DownloadBudget | None = None,
) -> str | None:
    """下载单张图片并返回经过内容校验的 base64 data URI。"""
    active_policy = policy or ImageDownloadPolicy()
    active_budget = budget or _DownloadBudget(active_policy.max_total_bytes)
    current_url = url
    redirects = 0
    attempt = 1

    while attempt <= _DOWNLOAD_RETRY_ATTEMPTS:
        if not await _validate_download_url(current_url):
            return None

        try:
            async with session.get(current_url, allow_redirects=False) as resp:
                outcome = await _handle_download_response(
                    resp,
                    current_url,
                    attempt,
                    active_policy,
                    active_budget,
                )
        except (TimeoutError, aiohttp.ClientError) as exc:
            if attempt < _DOWNLOAD_RETRY_ATTEMPTS:
                outcome = _DownloadOutcome(retry_reason=str(exc))
            else:
                logger.warning(
                    "[ImageDownloader] 下载失败: {} url={}",
                    exc,
                    _safe_url_label(current_url),
                )
                outcome = _DownloadOutcome(should_abort=True)
        except Exception:
            logger.warning(
                "[ImageDownloader] 下载未知错误: url={}",
                _safe_url_label(current_url),
                exc_info=True,
            )
            outcome = _DownloadOutcome(should_abort=True)

        if outcome.data_uri is not None:
            return outcome.data_uri
        if outcome.should_abort:
            return None

        if outcome.redirect_url is not None:
            redirects += 1
            if redirects > _MAX_REDIRECTS:
                logger.warning(
                    "[ImageDownloader] 图片重定向次数超过上限: url={}",
                    _safe_url_label(current_url),
                )
                return None
            current_url = outcome.redirect_url
            continue

        if outcome.retry_reason is None:
            return None

        delay = _get_retry_delay(attempt)
        logger.info(
            "[ImageDownloader] 图片暂未就绪，{} 秒后重试: attempt={} reason={} url={}",
            f"{delay:.1f}",
            attempt,
            outcome.retry_reason,
            _safe_url_label(current_url),
        )
        await asyncio.sleep(delay)
        attempt += 1

    return None


async def _download_with_semaphore(
    session: aiohttp.ClientSession,
    url: str,
    policy: ImageDownloadPolicy,
    budget: _DownloadBudget,
    semaphore: asyncio.Semaphore,
) -> str | None:
    async with semaphore:
        return await _download_single_image(session, url, policy, budget)


async def download_images_as_base64_aligned(
    urls: list[str],
    policy: ImageDownloadPolicy | None = None,
) -> list[str | None]:
    """按输入位置返回下载结果，并对整批图片应用共享资源预算。"""
    if not urls:
        return []

    active_policy = policy or ImageDownloadPolicy()
    selected_urls = urls[: active_policy.max_images]
    results: list[str | None] = [None] * len(urls)
    budget = _DownloadBudget(active_policy.max_total_bytes)
    semaphore = asyncio.Semaphore(active_policy.concurrency)
    timeout = aiohttp.ClientTimeout(
        total=None,
        connect=active_policy.connect_timeout_seconds,
        sock_connect=active_policy.connect_timeout_seconds,
        sock_read=active_policy.read_timeout_seconds,
    )
    connector = aiohttp.TCPConnector(
        resolver=_PublicAddressResolver(),
        use_dns_cache=False,
    )

    tasks: list[asyncio.Task[str | None]] = []
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        tasks = [
            asyncio.create_task(
                _download_with_semaphore(
                    session,
                    url,
                    active_policy,
                    budget,
                    semaphore,
                )
            )
            for url in selected_urls
        ]
        selected_results: list[str | None] | None = None
        try:
            async with asyncio.timeout(active_policy.total_timeout_seconds):
                selected_results = await asyncio.gather(*tasks)
        except TimeoutError:
            logger.warning(
                "[ImageDownloader] 单条消息图片下载超过总时限: seconds={}",
                active_policy.total_timeout_seconds,
            )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        if selected_results is None:
            selected_results = [
                task.result()
                if task.done() and not task.cancelled() and task.exception() is None
                else None
                for task in tasks
            ]

    results[: len(selected_results)] = selected_results
    succeeded = sum(result is not None for result in results)
    if len(urls) > active_policy.max_images:
        logger.warning(
            "[ImageDownloader] 图片数量超过单条消息上限: total={} limit={}",
            len(urls),
            active_policy.max_images,
        )
    if succeeded < len(urls):
        logger.warning(
            "[ImageDownloader] {} / {} 张图片下载成功，响应体累计 {} bytes",
            succeeded,
            len(urls),
            budget.consumed_bytes,
        )
    return results


async def download_images_as_base64(
    urls: list[str],
    policy: ImageDownloadPolicy | None = None,
) -> list[str]:
    """下载图片列表，过滤失败结果；保留给独立调用方的兼容入口。"""
    aligned_results = await download_images_as_base64_aligned(urls, policy)
    return [result for result in aligned_results if result is not None]
