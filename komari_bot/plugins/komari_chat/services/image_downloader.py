"""图片下载工具 — 将远程图片 URL 转为 base64 data URI。

QQ 的多媒体图片 URL（multimedia.nt.qq.com.cn）带有临时鉴权参数，
Gemini API 代理无法直接下载。此模块在 bot 侧完成下载并转为 base64，
确保 LLM 能看到图片内容。
"""

from __future__ import annotations

import asyncio
import base64

import aiohttp
from nonebot import logger

# 限制：单张图片最大 10 MB
_MAX_IMAGE_SIZE = 10 * 1024 * 1024
# 下载超时 10 秒
_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=10)
_READ_CHUNK_SIZE = 64 * 1024
_DOWNLOAD_RETRY_ATTEMPTS = 3
_DOWNLOAD_RETRY_BASE_DELAY = 0.2
_DOWNLOAD_RETRY_MAX_DELAY = 1.0
_RETRYABLE_STATUS_CODES = frozenset({404, 408, 425, 429, 500, 502, 503, 504})


def _guess_mime_type(content_type: str | None, url: str) -> str:
    """根据 Content-Type 或 URL 推断 MIME 类型。"""
    if content_type:
        # 取 Content-Type 主类型，例如 "image/jpeg; charset=utf-8" → "image/jpeg"
        mime = content_type.split(";")[0].strip().lower()
        if mime.startswith("image/"):
            return mime

    # 降级：从 URL 后缀推断
    lower_url = url.split("?")[0].lower()
    if lower_url.endswith(".png"):
        return "image/png"
    if lower_url.endswith(".gif"):
        return "image/gif"
    if lower_url.endswith(".webp"):
        return "image/webp"

    # 默认 JPEG
    return "image/jpeg"


def _get_retry_delay(attempt: int) -> float:
    """根据尝试次数计算下一次重试延迟。"""
    return min(
        _DOWNLOAD_RETRY_BASE_DELAY * (2 ** (attempt - 1)),
        _DOWNLOAD_RETRY_MAX_DELAY,
    )


async def _read_image_bytes(
    resp: aiohttp.ClientResponse,
    url: str,
) -> bytes | None:
    """读取完整图片响应体，并在读取过程中持续检查大小限制。"""
    content_length = resp.content_length
    if content_length and content_length > _MAX_IMAGE_SIZE:
        logger.warning(
            "[ImageDownloader] 图片过大: {} bytes, url={}",
            content_length,
            url[:100],
        )
        return None

    if content_length is not None:
        data = await resp.read()
        if len(data) > _MAX_IMAGE_SIZE:
            logger.warning(
                "[ImageDownloader] 图片过大 (读取时): {} bytes, url={}",
                len(data),
                url[:100],
            )
            return None
        return data

    buffer = bytearray()
    async for chunk in resp.content.iter_chunked(_READ_CHUNK_SIZE):
        buffer.extend(chunk)
        if len(buffer) > _MAX_IMAGE_SIZE:
            logger.warning(
                "[ImageDownloader] 图片过大 (分块读取时): {} bytes, url={}",
                len(buffer),
                url[:100],
            )
            return None

    return bytes(buffer)


async def _handle_download_response(
    resp: aiohttp.ClientResponse,
    url: str,
    attempt: int,
) -> tuple[str | None, str | None, bool]:
    """处理单次下载响应，返回重试原因、数据 URI 和是否应终止。"""
    if resp.status != 200:
        if resp.status in _RETRYABLE_STATUS_CODES and attempt < _DOWNLOAD_RETRY_ATTEMPTS:
            return f"HTTP {resp.status}", None, False

        logger.warning(
            "[ImageDownloader] 下载失败: HTTP {}, url={}",
            resp.status,
            url[:100],
        )
        return None, None, True

    data = await _read_image_bytes(resp, url)
    if data is None:
        return None, None, True

    if not data:
        if attempt < _DOWNLOAD_RETRY_ATTEMPTS:
            return "empty body", None, False

        logger.warning("[ImageDownloader] 图片内容为空: url={}", url[:100])
        return None, None, True

    mime_type = _guess_mime_type(resp.content_type, url)
    b64 = base64.b64encode(data).decode("ascii")
    return None, f"data:{mime_type};base64,{b64}", False


async def _download_single_image(
    session: aiohttp.ClientSession,
    url: str,
) -> str | None:
    """下载单张图片并转为 base64 data URI。

    Args:
        session: aiohttp 会话
        url: 图片 URL

    Returns:
        base64 data URI 字符串，失败时返回 None
    """
    for attempt in range(1, _DOWNLOAD_RETRY_ATTEMPTS + 1):
        retry_reason: str | None = None
        data_uri: str | None = None
        should_abort = False

        try:
            async with session.get(url) as resp:
                retry_reason, data_uri, should_abort = await _handle_download_response(
                    resp,
                    url,
                    attempt,
                )

        except (TimeoutError, aiohttp.ClientError) as e:
            if attempt < _DOWNLOAD_RETRY_ATTEMPTS:
                retry_reason = str(e)
            else:
                logger.warning("[ImageDownloader] 下载失败: {}, url={}", e, url[:100])
                should_abort = True
        except Exception:
            logger.warning(
                "[ImageDownloader] 下载未知错误: url={}",
                url[:100],
                exc_info=True,
            )
            should_abort = True

        if data_uri is not None:
            return data_uri

        if should_abort:
            break

        if retry_reason is None:
            break

        delay = _get_retry_delay(attempt)
        logger.info(
            "[ImageDownloader] 图片暂未就绪，{} 秒后重试: attempt={} reason={} url={}",
            f"{delay:.1f}",
            attempt,
            retry_reason,
            url[:100],
        )
        await asyncio.sleep(delay)

    return None


async def download_images_as_base64(urls: list[str]) -> list[str]:
    """将图片 URL 列表转为 base64 data URI 列表。

    下载失败的图片会被跳过（记录 warning 日志）。

    Args:
        urls: 图片 URL 列表

    Returns:
        成功转换的 base64 data URI 列表（可能比输入列表短）
    """
    if not urls:
        return []

    results: list[str] = []
    async with aiohttp.ClientSession(timeout=_DOWNLOAD_TIMEOUT) as session:
        for url in urls:
            data_uri = await _download_single_image(session, url)
            if data_uri:
                logger.info(
                    "[ImageDownloader] 图片下载成功: {} bytes base64",
                    len(data_uri),
                )
                results.append(data_uri)

    if len(results) < len(urls):
        logger.warning(
            "[ImageDownloader] {} / {} 张图片下载成功",
            len(results),
            len(urls),
        )

    return results
