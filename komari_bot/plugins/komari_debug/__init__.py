"""Komari Debug - SUPERUSER 调试命令插件。

所有子命令仅允许 SUPERUSER 使用，运行时校验权限。
"""

from __future__ import annotations

from nonebot.plugin import PluginMetadata, require

require("user_data")
require("character_binding")
require("komari_chat")
require("group_history_summary")
require("agent_run_logger")

__plugin_meta__ = PluginMetadata(
    name="komari_debug",
    description="SUPERUSER 调试命令插件（好感度/绑定/回复/总结诊断）",
    usage=(
        ".debug — 显示子命令帮助\n"
        ".debug favor get <用户ID>\n"
        ".debug favor set <用户ID> <0-400>\n"
        ".debug bind set <用户ID> <角色名>\n"
        ".debug bind del <用户ID>\n"
        ".debug bind list\n"
        ".debug reply <测试文本>（仅群聊）\n"
        ".debug summary <总结要求>（仅群聊）"
    ),
)

from . import commands  # noqa: F401
