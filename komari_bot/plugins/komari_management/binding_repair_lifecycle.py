"""TSK-280 角色绑定修复服务的管理装配生命周期。

创建与关闭都由管理 composition root 负责：它注入真实游戏存储的顶层公共
seam 作为对局状态读取器，绑定插件自身不持有这层反向依赖。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _open_repair_session() -> AsyncSession:
    """打开 nonebot-plugin-orm 托管的共享会话。"""
    from nonebot import require

    require("nonebot_plugin_orm")
    from nonebot_plugin_orm import get_session

    return get_session(expire_on_commit=False)


async def _read_game_state(
    session: AsyncSession,
    *,
    app_id: str,
    group_openid: str,
    for_update: bool = False,
) -> object | None:
    """真实游戏存储顶层公共 seam：读取作用域当前对局快照。"""
    from komari_bot.plugins.komari_roulette import (
        GroupRef,
        PostgresRouletteStorage,
    )

    return await PostgresRouletteStorage(session).load_current(
        GroupRef(app_id=app_id, group_openid=group_openid),
        for_update=for_update,
    )


def start_binding_repair_service() -> None:
    """构造并安装修复服务（注入真实对局状态读取器与绑定管理器）。"""
    from nonebot import require

    require("komari_roulette")
    from komari_bot.plugins.character_binding import get_binding_manager
    from komari_bot.plugins.character_binding.repair import (
        BindingRepairService,
        set_binding_repair_service,
    )

    service = BindingRepairService(
        session_factory=_open_repair_session,
        clock=lambda: datetime.now(tz=UTC),
        game_state_reader=_read_game_state,
        manager=get_binding_manager(),
    )
    set_binding_repair_service(service)


async def stop_binding_repair_service() -> None:
    """关闭并移除修复服务；``close`` 立即生效且不等待在途任务。"""
    from komari_bot.plugins.character_binding.repair import (
        get_binding_repair_service,
        set_binding_repair_service,
    )

    service = get_binding_repair_service()
    set_binding_repair_service(None)
    if service is not None:
        await service.close()


__all__ = ["start_binding_repair_service", "stop_binding_repair_service"]
