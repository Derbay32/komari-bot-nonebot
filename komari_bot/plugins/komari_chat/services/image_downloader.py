"""受资源预算与 SSRF 边界保护的远程图片下载工具。"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import re
import socket
import time
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


@dataclass(frozen=True)
class ImageDownloadPolicy:
    """单条消息的图片下载资源预算。

    构造默认值仅供下载器纯调用方（无配置可读）直接构造策略使用；
    ``from_config`` 不依赖这些默认值（见下）。
    """

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
        """任务起点从 ``komari_chat`` 配置读取一次并冻结图片下载预算。

        TSK-194 / ADR-0010 协调式破坏升级：0015 之后生产 typed 配置必须
        携带全部 8 项预算字段且满足 chat 表跨字段 CHECK；任一字段缺失立即
        明确失败，绝不静默回退 Python 默认值、旧 ``komari_memory`` 别名或
        历史快照；类型非法用 ``TypeError`` 表示。本方法由调用方在任务起点
        读取一次，任务内不再重读。
        """
        field_names = (
            "vision_image_download_max_count",
            "vision_image_download_max_bytes",
            "vision_image_download_total_max_bytes",
            "vision_image_download_max_pixels",
            "vision_image_download_concurrency",
            "vision_image_download_connect_timeout_seconds",
            "vision_image_download_read_timeout_seconds",
            "vision_image_download_total_timeout_seconds",
        )
        missing = [
            name for name in field_names if getattr(config, name, None) is None
        ]
        if missing:
            msg = (
                "配置缺少图片下载预算字段"
                f"（{', '.join(sorted(missing))}），无法冻结下载策略"
            )
            raise RuntimeError(msg)

        raw = {name: getattr(config, name) for name in field_names}
        for name in (
            "vision_image_download_max_count",
            "vision_image_download_max_bytes",
            "vision_image_download_total_max_bytes",
            "vision_image_download_max_pixels",
            "vision_image_download_concurrency",
        ):
            value = raw[name]
            if not isinstance(value, int) or isinstance(value, bool):
                msg = (
                    f"配置的图片下载预算字段非法（{name}={value!r}），"
                    "无法冻结下载策略"
                )
                raise TypeError(msg)
        for name in (
            "vision_image_download_connect_timeout_seconds",
            "vision_image_download_read_timeout_seconds",
            "vision_image_download_total_timeout_seconds",
        ):
            value = raw[name]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                msg = (
                    f"配置的图片下载预算字段非法（{name}={value!r}），"
                    "无法冻结下载策略"
                )
                raise TypeError(msg)

        max_total_bytes = int(raw["vision_image_download_total_max_bytes"])
        return cls(
            max_images=raw["vision_image_download_max_count"],
            max_image_bytes=min(
                raw["vision_image_download_max_bytes"],
                max_total_bytes,
            ),
            max_total_bytes=max_total_bytes,
            max_pixels=raw["vision_image_download_max_pixels"],
            concurrency=raw["vision_image_download_concurrency"],
            connect_timeout_seconds=float(
                raw["vision_image_download_connect_timeout_seconds"]
            ),
            read_timeout_seconds=float(
                raw["vision_image_download_read_timeout_seconds"]
            ),
            total_timeout_seconds=float(
                raw["vision_image_download_total_timeout_seconds"]
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


class _TimeBudget:
    """任务级下载总时限：下载活动区间的并集耗时（TSK-195）。

    语义：

    - 首个在途下载启动时从冻结的剩余值派生共享绝对 deadline；并发在途
      下载共享同一 deadline，重叠墙钟时段只计一次；
    - 全部在途下载结束后冻结剩余值（``deadline - now``），下一批从剩余
      值继续；无下载活动的间隔（例如 LLM 轮次之间）不消耗预算也不重置；
    - 每个下载尝试（含信号量等待）从 ``acquire`` 到 ``release`` 都在同一
      批内参与总时限。
    """

    def __init__(self, total_seconds: float) -> None:
        self.total_seconds = total_seconds
        self._remaining = total_seconds
        self._deadline: float | None = None
        self._active = 0
        self._lock = asyncio.Lock()

    async def acquire(self) -> float | None:
        """下载尝试开始前调用；返回共享绝对 deadline，预算耗尽返回 None。"""
        async with self._lock:
            if self._deadline is None:
                if self._remaining <= 0:
                    return None
                self._deadline = time.monotonic() + self._remaining
            self._active += 1
            return self._deadline

    async def release(self) -> None:
        """下载尝试结束后调用（成功/失败/取消都释放一次）。"""
        async with self._lock:
            self._active -= 1
            if self._active <= 0:
                self._active = 0
                if self._deadline is not None:
                    self._remaining = max(0.0, self._deadline - time.monotonic())
                    self._deadline = None


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


#: 供任务级图片会话等外部模块复用的安全日志标签（不含 path/query/userinfo）。
safe_url_label = _safe_url_label


async def _validate_download_url(url: str) -> bool:
    """校验 URL 结构与字面地址；域名地址在实际建连时校验。"""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        # 只记录归一化异常类型，不记录 str(exc)：解析失败的 URL 内容不得
        # 进入普通日志（TSK-195 第二轮安全验收反馈）。
        logger.warning(
            "[ImageDownloader] 图片 URL 解析失败: error_type={}",
            type(exc).__name__,
        )
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
            # 只使用异常类型等安全码，绝不记录 str(exc)：aiohttp 异常正文
            # 可能内嵌完整 URL path/query（TSK-195 第二轮安全验收反馈）。
            error_code = type(exc).__name__
            if attempt < _DOWNLOAD_RETRY_ATTEMPTS:
                outcome = _DownloadOutcome(retry_reason=error_code)
            else:
                logger.warning(
                    "[ImageDownloader] 下载失败: error_type={} url={}",
                    error_code,
                    _safe_url_label(current_url),
                )
                outcome = _DownloadOutcome(should_abort=True)
        except Exception as exc:
            # 不捕获 traceback：栈帧局部变量 current_url 含完整路径/query；
            # 只记录归一化异常类型与安全来源标签。
            logger.warning(
                "[ImageDownloader] 下载未知错误: error_type={} url={}",
                type(exc).__name__,
                _safe_url_label(current_url),
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


class ImageDownloadSession:
    """单个任务的多图下载会话：跨多次调用共享字节账本、并发与累计总时限。

    - 同一任务的全部图片下载共享同一个 ``_DownloadBudget``（响应体总字节）、
      同一并发信号量以及累计下载总时限；字节账本/信号量/时限不会因为多次
      调用而重建（TSK-195）；
    - 总时限采用“下载活动区间的并集耗时”：首个在途下载启动共享 deadline，
      并发在途下载共享同一 deadline（重叠墙钟只计一次），全部在途下载结束
      后冻结剩余值，下一批从剩余值继续；无下载活动的等待（例如 LLM 轮次
      之间）不消耗也不重置预算；
    - ``download()`` 与 ``download_many()`` 走同一会话账本/时限路径，不存在
      能绕开累计时限的第二套批量实现；
    - 每个会话实例相互隔离，任务结束调用 ``close()`` 释放连接后丢弃。
    """

    __slots__ = (
        "_budget",
        "_semaphore",
        "_session",
        "_time_budget",
        "policy",
    )

    def __init__(self, policy: ImageDownloadPolicy) -> None:
        self.policy = policy
        self._budget = _DownloadBudget(policy.max_total_bytes)
        self._semaphore = asyncio.Semaphore(policy.concurrency)
        self._time_budget = _TimeBudget(policy.total_timeout_seconds)
        self._session: aiohttp.ClientSession | None = None

    @property
    def downloaded_bytes(self) -> int:
        """本任务已累计下载的响应体字节数。"""
        return self._budget.consumed_bytes

    def _ensure_session(self) -> aiohttp.ClientSession:
        """惰性建立带 SSRF/DNS 重绑定防护的 aiohttp 会话并跨调用复用。

        同步创建（无 await 间隙），并发下载尝试不可能重复建会话。
        """
        if self._session is None:
            timeout = aiohttp.ClientTimeout(
                total=None,
                connect=self.policy.connect_timeout_seconds,
                sock_connect=self.policy.connect_timeout_seconds,
                sock_read=self.policy.read_timeout_seconds,
            )
            connector = aiohttp.TCPConnector(
                resolver=_PublicAddressResolver(),
                use_dns_cache=False,
            )
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
            )
        return self._session

    async def close(self) -> None:
        """释放本任务持有的下载连接（任务结束调用一次，可重复调用）。"""
        session = self._session
        self._session = None
        if session is not None:
            await session.close()

    async def _download_attempt(self, url: str) -> str | None:
        """一次下载尝试：加入共享累计总时限并执行单图下载。

        信号量等待与下载本体一起落在该批共享 deadline 内；总时限耗尽时
        返回 ``None``，超时由 ``asyncio.timeout_at`` 取消底层下载协程。
        """
        deadline = await self._time_budget.acquire()
        if deadline is None:
            logger.warning(
                "[ImageDownloader] 任务级图片下载总时限已耗尽，"
                "本任务不再消耗下载预算"
            )
            return None
        try:
            session = self._ensure_session()
            async with asyncio.timeout_at(deadline):
                async with self._semaphore:
                    return await _download_single_image(
                        session,
                        url,
                        self.policy,
                        self._budget,
                    )
        except TimeoutError:
            logger.warning(
                "[ImageDownloader] 单图下载超过任务剩余总时限: "
                "seconds={} url={}",
                f"{max(0.0, deadline - time.monotonic()):.1f}",
                _safe_url_label(url),
            )
            return None
        finally:
            await self._time_budget.release()

    async def download(self, url: str) -> str | None:
        """按需下载单张图片，与其他并发下载共享字节账本与累计总时限。

        返回经 SSRF/重定向/真实 MIME/完整性/像素校验后的 base64 data URI；
        任一环节失败返回 ``None``。同一会话内的多次调用共享同一份字节账本
        与“下载活动区间并集耗时”累计总时限：并发重叠只计一次、空闲等待
        不重置预算。
        """
        return await self._download_attempt(url)

    async def download_many(self, urls: list[str]) -> list[str | None]:
        """并发批量下载并按输入位置返回结果；与 ``download`` 共享同一账本。

        每个索引的下载走与 ``download()`` 相同的 ``_download_attempt``
        路径（同一字节账本、并发信号量与累计总时限），不存在能绕开累计
        时限的第二套批量实现。超过 ``max_images`` 的输入位置保持 ``None``
        并记录警告。
        """
        if not urls:
            return []

        selected_urls = urls[: self.policy.max_images]
        results: list[str | None] = [None] * len(urls)
        tasks: list[asyncio.Task[str | None]] = [
            asyncio.create_task(self._download_attempt(url))
            for url in selected_urls
        ]
        try:
            selected_results = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        results[: len(selected_results)] = selected_results
        succeeded = sum(result is not None for result in results)
        if len(urls) > self.policy.max_images:
            logger.warning(
                "[ImageDownloader] 图片数量超过单条消息上限: total={} limit={}",
                len(urls),
                self.policy.max_images,
            )
        if succeeded < len(urls):
            logger.warning(
                "[ImageDownloader] {} / {} 张图片下载成功，响应体累计 {} bytes",
                succeeded,
                len(urls),
                self._budget.consumed_bytes,
            )
        return results


async def download_images_as_base64_aligned(
    urls: list[str],
    policy: ImageDownloadPolicy | None = None,
) -> list[str | None]:
    """按输入位置返回下载结果，并对整批图片应用共享资源预算。

    保留给原生批量路径与独立调用方的兼容入口：内部新建一个任务级
    ``ImageDownloadSession`` 并执行一次性批量下载；无论成功与否都会关闭
    会话持有的连接（可靠 close，不泄漏 aiohttp 会话）。
    """
    active_policy = policy or ImageDownloadPolicy()
    session_obj = ImageDownloadSession(active_policy)
    try:
        return await session_obj.download_many(urls)
    finally:
        await session_obj.close()


async def download_images_as_base64(
    urls: list[str],
    policy: ImageDownloadPolicy | None = None,
) -> list[str]:
    """下载图片列表，过滤失败结果；保留给独立调用方的兼容入口。"""
    aligned_results = await download_images_as_base64_aligned(urls, policy)
    return [result for result in aligned_results if result is not None]
