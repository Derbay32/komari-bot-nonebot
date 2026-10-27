"""群聊总结固定字体资产的初始化准备。"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import aiohttp
from nonebot import logger

from komari_bot.core.project_paths import DATA_DIR

FONT_PATH = DATA_DIR / "fonts" / "LXGWWenKaiGBScreen.ttf"
FONT_URL = (
    "https://github.com/lxgw/LxgwWenKai-Screen/releases/download/"
    "v1.521/LXGWWenKaiGBScreen.ttf"
)

_DOWNLOAD_CHUNK_SIZE = 64 * 1024
_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(
    total=120,
    connect=10,
    sock_connect=10,
    sock_read=30,
)
_TEMP_SUFFIX = ".tmp"


async def ensure_font_asset() -> None:
    """确保固定字体文件存在；缺失时从固定上游完整下载后原子落盘。"""
    if FONT_PATH.exists():
        return

    temp_path: Path | None = None
    try:
        FONT_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{FONT_PATH.name}.",
            suffix=_TEMP_SUFFIX,
            dir=FONT_PATH.parent,
        )
        temp_path = Path(temp_name)

        with os.fdopen(fd, "wb") as temp_file:
            async with (
                aiohttp.ClientSession(timeout=_DOWNLOAD_TIMEOUT) as session,
                session.get(FONT_URL) as response,
            ):
                response.raise_for_status()
                async for chunk in response.content.iter_chunked(
                    _DOWNLOAD_CHUNK_SIZE
                ):
                    temp_file.write(chunk)

        temp_path.replace(FONT_PATH)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error(
            "[GroupHistorySummary] 固定字体准备失败: error_type={}",
            type(exc).__name__,
        )
        raise
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
