"""角色绑定插件 - 提供跨插件的角色名管理功能。"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from nonebot import get_driver, get_plugin_config, logger
from nonebot.plugin import PluginMetadata, require

# 依赖统一群聊准入插件
require("group_admission")
require("user_ban")

from . import reply_evidence as _reply_evidence  # noqa: F401
from .manager import (
    BindingConflictError,
    BindingPersistenceError,
    CharacterBindingManager,
    CharacterNameValidationError,
    GroupBindingRecord,
    character_name_key,
    get_manager,
)
from .qq_coordinator import QQBindingCoordinator
from .reply_evidence import (
    ReplyEvidence,
    ReplyEvidenceCollector,
    ReplyEvidenceSession,
    SessionCodeCollisionError,
    get_runtime_collectors,
    set_runtime_collectors,
)
from .transaction import BindingTransaction, GroupBindingGroup
from .wizard import BindingWizard, get_binding_wizard, set_binding_wizard

__plugin_meta__ = PluginMetadata(
    name="character_binding",
    description="提供跨插件的角色名绑定管理功能",
    usage="""
    /bind - 开始或继续本群绑定向导
    /bind rename - 修改本群角色名
    /bind unbind - 解除本群角色名绑定
    /bind name <会话码> <角色名> - 提交或修改角色名
    /bind reuse <会话码> - 沿用旧角色名
    /bind confirm <会话码> - 确认当前操作
    /bind cancel <会话码> - 取消当前操作
    """,
)


type CommandBanScope = Literal["command"]


@dataclass(slots=True)
class _QQPluginState:
    coordinator: QQBindingCoordinator | None = None


_qq_plugin_state = _QQPluginState()


async def _unavailable_message_fetcher(
    _message_id: int,
) -> Mapping[str, object]:
    raise RuntimeError("receiving OneBot bot is required for message evidence")  # noqa: TRY003


async def _resolve_qq_group(app_id: str, group_openid: str) -> int | None:
    """Resolve one canonical QQ group for admission via a lock-free committed read.

    该解析器会被持组锁的 confirm 重审调用；若另开 session 再取同一 advisory
    lock 会形成跨 session 自锁，因此这里只读取已提交 canonical 行。
    """
    from .database import _open_session

    session = _open_session()
    try:
        async with session.begin():
            group = await BindingTransaction(session).resolve_group(
                app_id=app_id,
                group_openid=group_openid,
                lock=False,
            )
            if group is None:
                return None
            try:
                group_id = int(group.group_id)
            except (TypeError, ValueError):
                raise RuntimeError("invalid canonical QQ group identity") from None  # noqa: TRY003
            if group_id <= 0:
                raise RuntimeError("invalid canonical QQ group identity")  # noqa: TRY003
            return group_id
    finally:
        await session.close()


async def _resolve_qq_member(
    app_id: str,
    group_openid: str,
    member_openid: str,
) -> int | None:
    """Resolve the current trusted numeric member identity, if bound.

    与 group resolver 相同：准入只读，不加组锁，只读取已提交 canonical 行。
    """
    from .database import _open_session

    session = _open_session()
    try:
        async with session.begin():
            member = await BindingTransaction(session).resolve_member(
                app_id=app_id,
                group_openid=group_openid,
                member_openid=member_openid,
                lock=False,
            )
            if member is None:
                return None
            try:
                member_qq = int(member.member_qq)
            except (TypeError, ValueError):
                raise RuntimeError("invalid canonical QQ member identity") from None  # noqa: TRY003
            if member_qq <= 0:
                raise RuntimeError("invalid canonical QQ member identity")  # noqa: TRY003
            return member_qq
    finally:
        await session.close()


async def _qq_ban_checker(member_qq: int, scope: str) -> bool:
    """Use only the evidence-backed numeric QQ identity for user bans."""
    if scope != "command":
        raise ValueError("QQ admission only supports command ban scope")  # noqa: TRY003
    from komari_bot.plugins.user_ban import (
        is_configured_superuser_id,
        is_user_banned,
    )

    command_scope: CommandBanScope = "command"
    user_id = str(member_qq)
    if is_configured_superuser_id(user_id):
        return False
    return await is_user_banned(user_id, command_scope)


def _configured_official_qq(raw_value: object) -> str | None:
    """Normalize one startup trusted QQ value without leaking its contents."""
    if isinstance(raw_value, bool):
        return None
    if isinstance(raw_value, int):
        return str(raw_value) if raw_value > 0 else None
    if not isinstance(raw_value, str):
        return None
    value = raw_value.strip()
    if not value or not value.isascii() or not value.isdigit():
        return None
    try:
        parsed = int(value)
    except (ValueError, OverflowError):
        return None
    return str(parsed) if parsed > 0 else None


def _configured_qq_collectors() -> tuple[ReplyEvidenceCollector, ...]:
    """Build one evidence collector per QQ app with a valid trusted QQ ID."""
    from nonebot.adapters.qq.config import Config as QQConfig

    config = get_plugin_config(QQConfig)
    raw_map = getattr(get_driver().config, "qq_official_bot_qq_by_app", {})
    official_by_app = raw_map if isinstance(raw_map, Mapping) else {}
    collectors: list[ReplyEvidenceCollector] = []
    for bot_info in config.qq_bots:
        official_text = _configured_official_qq(official_by_app.get(bot_info.id))
        if official_text is None:
            logger.warning(
                "[CharacterBinding] QQ evidence disabled: invalid trusted identity configuration"
            )
            continue
        collectors.append(
            ReplyEvidenceCollector(
                app_id=bot_info.id,
                official_bot_qq=official_text,
                message_fetcher=_unavailable_message_fetcher,
                clock=lambda: datetime.now(UTC),
            )
        )
    return tuple(collectors)


async def init_plugin() -> None:
    """初始化管理器后安装 QQ 准入、证据接线与绑定向导。"""
    manager = get_manager()
    await manager.initialize()
    if not manager._initialized:
        return
    previous_wizard = get_binding_wizard()
    if previous_wizard is not None:
        await previous_wizard.close()
        set_binding_wizard(None)
    if _qq_plugin_state.coordinator is not None:
        await _qq_plugin_state.coordinator.close()
    coordinator = QQBindingCoordinator(
        collectors=_configured_qq_collectors(),
        group_resolver=_resolve_qq_group,
        clock=lambda: datetime.now(UTC),
        member_resolver=_resolve_qq_member,
        ban_checker=_qq_ban_checker,
    )
    _qq_plugin_state.coordinator = coordinator
    await coordinator.start()
    from .database import _open_session

    set_binding_wizard(
        BindingWizard(
            coordinator=coordinator,
            session_factory=_open_session,
            clock=lambda: datetime.now(UTC),
            manager=manager,
        )
    )


async def close_plugin() -> None:
    """先撤销向导与 QQ 准入接缝，再释放绑定数据库租约。"""
    wizard = get_binding_wizard()
    if wizard is not None:
        await wizard.close()
    set_binding_wizard(None)
    if _qq_plugin_state.coordinator is not None:
        await _qq_plugin_state.coordinator.close()
        _qq_plugin_state.coordinator = None
    await get_manager().close()


def get_binding_manager() -> CharacterBindingManager:
    """获取角色绑定管理器实例。

    Returns:
        管理器实例
    """
    return get_manager()


def get_character_name(
    *,
    group_id: str,
    user_id: str,
    fallback_nickname: str | None = None,
) -> str:
    """OneBot 群事件的最小群作用域桥接查询。"""
    return get_manager().get_character_name(
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
    return get_manager().get_qq_character_name(
        app_id=app_id,
        group_openid=group_openid,
        member_openid=member_openid,
        fallback_nickname=fallback_nickname,
    )


async def get_legacy_character_name(user_id: str) -> str | None:
    """读取旧全局角色名作为主动绑定流程的迁移候选。"""
    return await get_manager().get_legacy_character_name(user_id)


__all__ = [
    "BindingConflictError",
    "BindingPersistenceError",
    "BindingTransaction",
    "CharacterBindingManager",
    "CharacterNameValidationError",
    "GroupBindingGroup",
    "GroupBindingRecord",
    "QQBindingCoordinator",
    "ReplyEvidence",
    "ReplyEvidenceCollector",
    "ReplyEvidenceSession",
    "SessionCodeCollisionError",
    "character_name_key",
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

if driver is not None:
    # 导入 QQ handler 以注册唯一 matcher（必须在 manager 之后导入以避免循环导入）
    from . import qq_commands  # noqa: F401

    driver.on_startup(init_plugin)
    driver.on_shutdown(close_plugin)
