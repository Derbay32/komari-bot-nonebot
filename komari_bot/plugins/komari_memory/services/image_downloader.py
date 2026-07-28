"""图片下载工具 — 将远程图片 URL 转为 base64 data URI。

QQ 的多媒体图片 URL（multimedia.nt.qq.com.cn）带有临时鉴权参数，
Gemini API 代理无法直接下载。此模块在 bot 侧完成下载并转为 base64，
确保 LLM 能看到图片内容。
"""

from __future__ import annotations

import base64
from logging import getLogger

import aiohttp

logger = getLogger(__name__)

# 限制：单张图片最大 10 MB
_MAX_IMAGE_SIZE = 10 * 1024 * 1024
# 下载超时 10 秒
_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=10)


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
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                logger.warning(
                    "[ImageDownloader] 下载失败: HTTP %d, url=%s",
                    resp.status,
                    url[:100],
                )
                return None

            # 预检大小（通过 Content-Length）
            content_length = resp.content_length
            if content_length and content_length > _MAX_IMAGE_SIZE:
                logger.warning(
                    "[ImageDownloader] 图片过大: %d bytes, url=%s",
                    content_length,
                    url[:100],
                )
                return None

            # 读取（带大小限制），读多 1 byte 用于检测超限
            data = await resp.content.read(_MAX_IMAGE_SIZE + 1)
            if len(data) > _MAX_IMAGE_SIZE:
                logger.warning(
                    "[ImageDownloader] 图片过大 (读取时): %d bytes, url=%s",
                    len(data),
                    url[:100],
                )
                return None

            mime_type = _guess_mime_type(resp.content_type, url)
            b64 = base64.b64encode(data).decode("ascii")

    except (TimeoutError, aiohttp.ClientError) as e:
        logger.warning("[ImageDownloader] 下载失败: %s, url=%s", e, url[:100])
        return None
    except Exception:
        logger.warning(
            "[ImageDownloader] 下载未知错误: url=%s",
            url[:100],
            exc_info=True,
        )
        return None

    return f"data:{mime_type};base64,{b64}"


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
                    "[ImageDownloader] 图片下载成功: %d bytes base64",
                    len(data_uri),
                )
                results.append(data_uri)

    if len(results) < len(urls):
        logger.warning(
            "[ImageDownloader] %d / %d 张图片下载成功",
            len(results),
            len(urls),
        )

    return results
