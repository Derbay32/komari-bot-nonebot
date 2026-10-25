"""Komari Memory - Hindsight shared long-term memory caller."""

from __future__ import annotations

import asyncio

from nonebot import get_driver, logger
from nonebot.plugin import PluginMetadata

from .config_schema import KomariMemoryConfigSchema
from .services.config_interface import get_config_async
from .services.hindsight_retain import HindsightRetainRequest
from .services.hindsight_service import (
    HindsightMemoryService,
    HindsightRecallSession,
    HindsightUnavailableError,
)

__plugin_meta__ = PluginMetadata(
    name="小鞠记忆",
    description="Hindsight 共享长期记忆调用侧",
    usage="自动运行，无需命令",
)

__all__ = [
    "HindsightMemoryService",
    "HindsightRecallSession",
    "HindsightRetainRequest",
    "HindsightUnavailableError",
    "KomariMemoryConfigSchema",
    "get_hindsight_memory",
]


class PluginState:
    """Mutable process-local state owned by this plugin lifecycle."""

    def __init__(self) -> None:
        self.service: HindsightMemoryService | None = None


_state = PluginState()


def get_hindsight_memory() -> HindsightMemoryService | None:
    """Return the stable runtime caller, or ``None`` outside the lifecycle."""
    return _state.service


driver = get_driver()


@driver.on_startup
async def startup() -> None:
    """Build the caller and attempt one isolated bounded probe."""
    service = HindsightMemoryService(config_getter=get_config_async)
    _state.service = service
    try:
        await service.startup_probe()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Configuration or transport failure never aborts unrelated plugins.
        logger.error(
            "[KomariMemory] Hindsight 启动探测异常: error_type={}",
            type(exc).__name__,
        )


@driver.on_shutdown
async def shutdown() -> None:
    """Close only the caller and its owned session clients."""
    service = _state.service
    _state.service = None
    if service is None:
        return
    try:
        await service.close()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error(
            "[KomariMemory] Hindsight 关闭异常: error_type={}",
            type(exc).__name__,
        )
