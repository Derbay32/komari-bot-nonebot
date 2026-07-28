"""Komari Help 插件元数据扫描器。"""

from __future__ import annotations

import asyncio
import re
import uuid
from contextlib import suppress
from types import ModuleType
from typing import TYPE_CHECKING, Any

from nonebot import logger
from nonebot.plugin import get_loaded_plugins

from .engine import get_disabled_auto_help_plugins

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .engine import HelpEngine
    from .models import HelpCategory


SCAN_LEASE_SECONDS = 90
SCAN_LEASE_HEARTBEAT_SECONDS = 30


class HelpScanAlreadyRunningError(RuntimeError):
    """另一个 worker 正持有帮助扫描租约。"""

    def __init__(self) -> None:
        super().__init__("另一个 worker 正在扫描帮助元数据")


class HelpScanLeaseLostError(RuntimeError):
    """当前帮助扫描任务已经失去租约。"""

    def __init__(self) -> None:
        super().__init__("帮助扫描期间租约已丢失，已停止继续写入")


def _get_plugin_meta(plugin: Any) -> Any | None:
    metadata = getattr(plugin, "metadata", None)
    if metadata is not None:
        return metadata
    module = getattr(plugin, "module", None)
    if isinstance(module, ModuleType):
        return getattr(module, "__plugin_meta__", None)
    return None


def _extract_keywords(*texts: str) -> list[str]:
    keywords: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for token in re.findall(r"[\w\-/]{2,}|[\u4e00-\u9fff]{2,}", text):
            normalized = token.strip().lower()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            keywords.append(token.strip())
    return keywords


def _iter_usage_lines(usage: str | None, description: str | None) -> Iterable[str]:
    base = usage or description or ""
    for line in base.splitlines():
        cleaned = line.strip()
        if cleaned:
            yield cleaned


def _guess_category(usage: str | None) -> HelpCategory:
    if usage and "/" in usage:
        return "command"
    return "feature"


async def scan_and_sync(engine: HelpEngine) -> int:
    """扫描所有已加载插件并同步自动生成帮助条目。"""
    owner_token = uuid.uuid4().hex
    acquired = await engine.acquire_scan_lease(
        owner_token,
        lease_seconds=SCAN_LEASE_SECONDS,
    )
    if not acquired:
        raise HelpScanAlreadyRunningError

    lease_lost = asyncio.Event()
    heartbeat_task = asyncio.create_task(
        _maintain_scan_lease(engine, owner_token, lease_lost),
        name="komari-help-scan-lease-heartbeat",
    )
    try:
        updated_count = 0
        disabled_plugins = get_disabled_auto_help_plugins()
        deleted_count = await engine.delete_auto_generated_help_by_plugins(
            disabled_plugins,
            rebuild_index=False,
        )

        for plugin in get_loaded_plugins():
            _raise_if_lease_lost(lease_lost)
            metadata = _get_plugin_meta(plugin)
            if metadata is None:
                continue

            plugin_name = getattr(plugin, "name", None)
            if not isinstance(plugin_name, str) or not plugin_name.strip():
                continue
            if plugin_name in disabled_plugins:
                continue

            title = str(getattr(metadata, "name", plugin_name)).strip() or plugin_name
            description = str(getattr(metadata, "description", "")).strip()
            usage = str(getattr(metadata, "usage", "")).strip()
            content = "\n".join(_iter_usage_lines(usage, description))
            if not content:
                continue

            changed = await engine.sync_auto_generated_help(
                plugin_name=plugin_name,
                title=title,
                content=content,
                keywords=_extract_keywords(plugin_name, title, description),
                category=_guess_category(usage),
                notes="自动扫描生成",
                rebuild_index=False,
            )
            _raise_if_lease_lost(lease_lost)
            if changed:
                updated_count += 1

        if updated_count > 0 or deleted_count > 0:
            await engine.rebuild_keyword_index()
        _raise_if_lease_lost(lease_lost)
        return updated_count
    finally:
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
        try:
            await engine.release_scan_lease(owner_token)
        except Exception:
            logger.exception("[Komari Help] 释放帮助扫描租约失败")


def _raise_if_lease_lost(lease_lost: asyncio.Event) -> None:
    if lease_lost.is_set():
        raise HelpScanLeaseLostError


async def _maintain_scan_lease(
    engine: HelpEngine,
    owner_token: str,
    lease_lost: asyncio.Event,
) -> None:
    try:
        while True:
            await asyncio.sleep(SCAN_LEASE_HEARTBEAT_SECONDS)
            renewed = await engine.renew_scan_lease(
                owner_token,
                lease_seconds=SCAN_LEASE_SECONDS,
            )
            if not renewed:
                lease_lost.set()
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("[Komari Help] 帮助扫描租约续租失败")
        lease_lost.set()
