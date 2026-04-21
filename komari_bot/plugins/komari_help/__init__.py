"""Komari Help 帮助文档查询插件。"""

from __future__ import annotations

import importlib

from nonebot import get_driver, logger
from nonebot.plugin import PluginMetadata, require

from komari_bot.common.database_config import get_shared_database_config

from .api import register_help_api
from .config_schema import DynamicConfigSchema
from .engine import UNSET, HelpEngine, get_engine, initialize_engine
from .models import HelpCategory, HelpEntry, HelpListResponse, HelpSearchResult
from .scanner import scan_and_sync

config_manager_plugin = require("config_manager")
try:
    require("embedding_provider")
except RuntimeError as exc:
    logger.warning("[Komari Help] embedding_provider 依赖未就绪: %s", exc)

try:
    driver = get_driver()
except ValueError:
    driver = None
else:
    importlib.import_module("komari_bot.plugins.komari_help.commands")

config_manager = config_manager_plugin.get_config_manager(
    "komari_help", DynamicConfigSchema
)

__plugin_meta__ = PluginMetadata(
    name="komari-help",
    description="帮助文档查询 - 通过自然语言查询 bot 使用帮助",
    usage="/help [查询内容] — 查询帮助\n/help list — 列出所有帮助\n/help refresh — 重新扫描插件信息",
    config=DynamicConfigSchema,
)

__all__ = [
    "UNSET",
    "HelpCategory",
    "HelpEngine",
    "HelpEntry",
    "HelpListResponse",
    "HelpSearchResult",
    "add_help",
    "delete_help",
    "get_engine",
    "get_help",
    "list_help",
    "register_help_api",
    "search_by_keyword",
    "search_help",
    "update_help",
]

if driver is not None:

    @driver.on_startup
    async def on_startup() -> None:
        config = config_manager.get()
        if not config.plugin_enable:
            logger.info("[Komari Help] 插件未启用，跳过初始化")
            return

        db_config = get_shared_database_config()
        if not db_config.pg_user or not db_config.pg_password:
            logger.warning(
                "[Komari Help] 数据库用户名或密码未配置，跳过初始化。请在 database_config 中设置 pg_user 和 pg_password"
            )
            return

        try:
            engine = await initialize_engine()
            if config.auto_scan_on_startup:
                updated_count = await scan_and_sync(engine)
                logger.info(
                    f"[Komari Help] 启动扫描完成，同步 {updated_count} 条帮助条目"
                )
            logger.info("[Komari Help] 插件启动完成")
        except Exception as exc:
            logger.error(f"[Komari Help] 初始化失败: {exc}")

    @driver.on_shutdown
    async def on_shutdown() -> None:
        engine = get_engine()
        if engine is not None:
            await engine.close()
            logger.info("[Komari Help] 插件已关闭")


async def search_help(
    query: str,
    limit: int | None = None,
    query_embedding: list[float] | None = None,
) -> list[HelpSearchResult]:
    engine = get_engine()
    if engine is None:
        logger.warning("[Komari Help] 引擎未初始化")
        return []
    config = config_manager.get()
    if not config.plugin_enable:
        return []
    return await engine.search(query, limit, query_vec=query_embedding)


async def search_by_keyword(keyword: str) -> list[HelpSearchResult]:
    engine = get_engine()
    if engine is None:
        logger.warning("[Komari Help] 引擎未初始化")
        return []
    config = config_manager.get()
    if not config.plugin_enable:
        return []
    return await engine.search_by_keyword(keyword)


async def add_help(
    title: str,
    content: str,
    keywords: list[str],
    category: HelpCategory = "other",
    plugin_name: str | None = None,
    notes: str | None = None,
) -> int:
    engine = get_engine()
    if engine is None:
        raise RuntimeError("帮助引擎未初始化")
    return await engine.add_help(
        title=title,
        content=content,
        keywords=keywords,
        category=category,
        plugin_name=plugin_name,
        notes=notes,
    )


async def get_help(hid: int) -> HelpEntry | None:
    engine = get_engine()
    if engine is None:
        raise RuntimeError("帮助引擎未初始化")
    return await engine.get_help(hid)


async def list_help(
    *,
    limit: int,
    offset: int,
    query: str | None = None,
    category: HelpCategory | None = None,
) -> HelpListResponse:
    engine = get_engine()
    if engine is None:
        raise RuntimeError("帮助引擎未初始化")
    items, total = await engine.list_help(
        limit=limit,
        offset=offset,
        query=query,
        category=category,
    )
    return HelpListResponse(items=items, total=total, limit=limit, offset=offset)


async def delete_help(hid: int) -> bool:
    engine = get_engine()
    if engine is None:
        raise RuntimeError("帮助引擎未初始化")
    return await engine.delete_help(hid)


async def update_help(
    hid: int,
    *,
    title: str | object = UNSET,
    content: str | object = UNSET,
    keywords: list[str] | object = UNSET,
    category: HelpCategory | object = UNSET,
    plugin_name: str | None | object = UNSET,
    notes: str | None | object = UNSET,
) -> bool:
    engine = get_engine()
    if engine is None:
        raise RuntimeError("帮助引擎未初始化")
    return await engine.update_help(
        hid,
        title=title,
        content=content,
        keywords=keywords,
        category=category,
        plugin_name=plugin_name,
        notes=notes,
    )
