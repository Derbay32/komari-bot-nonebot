"""角色绑定插件 - 提供跨插件的角色名管理功能。"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from nonebot import get_driver
from nonebot.plugin import PluginMetadata, require
from sqlmodel import SQLModel

# 依赖统一群聊准入插件
require("group_admission")

from . import reply_evidence as _reply_evidence  # noqa: F401
from .reply_evidence import (
    ReplyEvidence,
    ReplyEvidenceCollector,
    ReplyEvidenceSession,
    SessionCodeCollisionError,
    get_runtime_collectors,
    set_runtime_collectors,
)

if TYPE_CHECKING:
    from .manager import (
        BindingConflictError,
        BindingPersistenceError,
        CharacterBindingManager,
        CharacterNameValidationError,
    )

__plugin_meta__ = PluginMetadata(
    name="character_binding",
    description="提供跨插件的角色名绑定管理功能",
    usage="""
    .bind set <角色名> - 设置绑定
    .bind del - 删除绑定
    .bind list - 查看绑定列表
    """,
)


async def init_plugin() -> None:
    """插件启动时初始化管理器。"""
    manager = import_module(f"{__name__}.manager").get_manager()
    await manager.initialize()


async def close_plugin() -> None:
    """插件关闭时释放数据库连接池租约。"""
    manager = import_module(f"{__name__}.manager").get_manager()
    await manager.close()


def get_binding_manager() -> CharacterBindingManager:
    """获取角色绑定管理器实例。

    Returns:
        管理器实例
    """
    return import_module(f"{__name__}.manager").get_manager()


def get_character_name(
    *,
    group_id: str,
    user_id: str,
    fallback_nickname: str | None = None,
) -> str:
    """OneBot 群事件的最小群作用域桥接查询。"""
    return get_binding_manager().get_character_name(
        group_id=group_id,
        user_id=user_id,
        fallback_nickname=fallback_nickname,
    )


def get_qq_character_name(
    *,
    app_id: str,
    group_openid: str,
    member_openid: str,
    fallback_nickname: str | None = None,
) -> str | None:
    """QQ 官方群事件的 canonical 角色名查询。"""
    return get_binding_manager().get_qq_character_name(
        app_id=app_id,
        group_openid=group_openid,
        member_openid=member_openid,
        fallback_nickname=fallback_nickname,
    )


async def get_legacy_character_name(user_id: str) -> str | None:
    """读取旧全局角色名作为主动绑定流程的迁移候选。"""
    return await get_binding_manager().get_legacy_character_name(user_id)


def __getattr__(name: str) -> Any:
    """Resolve manager types lazily so evidence import stays side-effect narrow."""
    if name in {
        "BindingConflictError",
        "BindingPersistenceError",
        "CharacterBindingManager",
        "CharacterNameValidationError",
    }:
        return getattr(import_module(f"{__name__}.manager"), name)
    raise AttributeError(name)


__all__ = [
    "BindingConflictError",
    "BindingPersistenceError",
    "CharacterBindingManager",
    "CharacterNameValidationError",
    "ReplyEvidence",
    "ReplyEvidenceCollector",
    "ReplyEvidenceSession",
    "SessionCodeCollisionError",
    "get_binding_manager",
    "get_character_name",
    "get_legacy_character_name",
    "get_qq_character_name",
    "get_runtime_collectors",
    "set_runtime_collectors",
]

try:
    driver = get_driver()
except ValueError:
    driver = None

if driver is not None and "komari_character_bindings" not in SQLModel.metadata.tables:
    # 导入命令模块以注册命令处理器；commands 自己导入 manager。
    from . import commands  # noqa: F401

    driver.on_startup(init_plugin)
    driver.on_shutdown(close_plugin)
