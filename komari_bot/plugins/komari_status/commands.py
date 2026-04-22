"""Komari Status 命令处理器。"""

from __future__ import annotations

from nonebot import on_command

from .api_client import StatusQueryError, get_status_client
from .renderer import render_status

status_cmd = on_command("status", priority=10, block=True)


@status_cmd.handle()
async def handle_status() -> None:
    """处理 .status 命令。"""
    from . import config_manager

    config = config_manager.get()
    if not config.plugin_enable:
        await status_cmd.finish("状态查询插件当前未启用")

    try:
        data = await get_status_client().fetch_status()
    except StatusQueryError as exc:
        await status_cmd.finish(str(exc))

    await status_cmd.finish(render_status(data))
