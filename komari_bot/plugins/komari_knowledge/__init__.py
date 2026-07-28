"""
Komari Knowledge 常识库插件。

提供 Bot 人物设定和世界知识的混合检索能力，管理 REST API 由 komari_management 统一挂载。
"""

from __future__ import annotations

from nonebot import get_driver, logger
from nonebot.plugin import PluginMetadata, require

from komari_bot.common.database_config import get_shared_database_config

from .api import register_knowledge_api
from .config_schema import DynamicConfigSchema
from .engine import (
    UNSET,
    SearchResult,
    get_engine,
    initialize_engine,
)
from .models import KnowledgeCategory, KnowledgeEntry, KnowledgeListResponse

# 依赖其他插件
config_manager_plugin = require("config_manager")
require("embedding_provider")

# 获取配置管理器
config_manager = config_manager_plugin.get_config_manager(
    "komari_knowledge", DynamicConfigSchema
)

__plugin_meta__ = PluginMetadata(
    name="komari-knowledge",
    description="小鞠常识库 - 提供人物设定和世界知识的混合检索与管理接口",
    usage="""
    在其他插件中引用：
    knowledge_plugin = require("komari-knowledge")
    results = await knowledge_plugin.search_knowledge("查询文本")
    """,
    config=DynamicConfigSchema,
)

__all__ = [
    "UNSET",
    "SearchResult",
    "add_knowledge",
    "delete_knowledge",
    "get_all_knowledge",
    "get_engine",
    "get_knowledge",
    "list_knowledge",
    "register_knowledge_api",
    "search_by_keyword",
    "search_knowledge",
    "update_knowledge",
]


driver = get_driver()


@driver.on_startup
async def on_startup() -> None:
    """Bot 启动时初始化常识库引擎。"""
    config = config_manager.get()

    if not config.plugin_enable:
        logger.info("[Komari Knowledge] 插件未启用，跳过初始化")
        return

    db_config = get_shared_database_config()
    if not db_config.pg_user or not db_config.pg_password:
        logger.warning(
            "[Komari Knowledge] 数据库用户名或密码未配置，跳过初始化。"
            "请在 database_config 中设置 pg_user 和 pg_password"
        )
        return

    try:
        await initialize_engine()
        logger.info("[Komari Knowledge] 插件启动完成")
    except Exception as e:
        logger.error(f"[Komari Knowledge] 初始化失败: {e}")


@driver.on_shutdown
async def on_shutdown() -> None:
    """Bot 关闭时清理资源。"""
    engine = get_engine()
    if engine:
        await engine.close()
        logger.info("[Komari Knowledge] 插件已关闭")


async def search_knowledge(
    query: str,
    limit: int | None = None,
    query_embedding: list[float] | None = None,
) -> list[SearchResult]:
    """
    检索相关知识。

    这是供其他插件调用的主要接口。
    """
    engine = get_engine()
    if engine is None:
        logger.warning("[Komari Knowledge] 引擎未初始化")
        return []

    config = config_manager.get()
    if not config.plugin_enable:
        return []

    return await engine.search(query, limit, query_vec=query_embedding)


async def search_by_keyword(keyword: str) -> list[SearchResult]:
    """通过关键词精确查询知识。"""
    engine = get_engine()
    if engine is None:
        logger.warning("[Komari Knowledge] 引擎未初始化")
        return []

    config = config_manager.get()
    if not config.plugin_enable:
        return []

    return await engine.search_by_keyword(keyword)


async def add_knowledge(
    content: str,
    keywords: list[str],
    category: KnowledgeCategory = "general",
    notes: str | None = None,
) -> int:
    """添加知识到数据库。"""
    engine = get_engine()
    if engine is None:
        raise RuntimeError("常识库引擎未初始化")

    return await engine.add_knowledge(content, keywords, category, notes)


async def get_knowledge(kid: int) -> KnowledgeEntry | None:
    """按 ID 获取单条知识。"""
    engine = get_engine()
    if engine is None:
        raise RuntimeError("常识库引擎未初始化")
    return await engine.get_knowledge(kid)


async def get_all_knowledge() -> list[dict]:
    """获取所有知识。"""
    engine = get_engine()
    if engine is None:
        raise RuntimeError("常识库引擎未初始化")

    return await engine.get_all_knowledge()


async def list_knowledge(
    *,
    limit: int,
    offset: int,
    query: str | None = None,
    category: KnowledgeCategory | None = None,
) -> KnowledgeListResponse:
    """分页获取知识列表。"""
    engine = get_engine()
    if engine is None:
        raise RuntimeError("常识库引擎未初始化")

    items, total = await engine.list_knowledge(
        limit=limit,
        offset=offset,
        query=query,
        category=category,
    )
    return KnowledgeListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


async def delete_knowledge(kid: int) -> bool:
    """删除知识。"""
    engine = get_engine()
    if engine is None:
        raise RuntimeError("常识库引擎未初始化")

    return await engine.delete_knowledge(kid)


async def update_knowledge(
    kid: int,
    *,
    content: str | object = UNSET,
    keywords: list[str] | object = UNSET,
    category: KnowledgeCategory | object = UNSET,
    notes: str | None | object = UNSET,
) -> bool:
    """更新知识。"""
    engine = get_engine()
    if engine is None:
        raise RuntimeError("常识库引擎未初始化")

    return await engine.update_knowledge(
        kid,
        content=content,
        keywords=keywords,
        category=category,
        notes=notes,
    )
