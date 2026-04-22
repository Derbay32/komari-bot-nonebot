"""Komari Help 插件元数据扫描器。"""

from __future__ import annotations

import re
from types import ModuleType
from typing import TYPE_CHECKING, Any

from nonebot.plugin import get_loaded_plugins

from .engine import get_disabled_auto_help_plugins

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .engine import HelpEngine
    from .models import HelpCategory


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
    updated_count = 0
    disabled_plugins = get_disabled_auto_help_plugins()

    for plugin in get_loaded_plugins():
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
        if changed:
            updated_count += 1

    if updated_count > 0:
        await engine._build_keyword_index()

    return updated_count
