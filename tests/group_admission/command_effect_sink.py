"""TSK-227 命令效果 sink census（测试真源，非测试模块）。

``group_admission.effect.command.*`` 效果行的 **sink census**：把每个被收发
敛的短生命周期命令 matcher 显式映射到它受治理的业务效果接缝（sink）。与
``entry_gate_census.py`` 的 matcher census 互补，便于对账。

确定性枚举：新增被收敛命令 matcher 必须一并更新本文件，否则验收测试失败。
"""

from __future__ import annotations

from dataclasses import dataclass

_COMMAND_ANCHOR = (
    "tests/group_admission/test_command_admission.py"
    "::test_census_maps_each_command_matcher_to_effect"
)


@dataclass(frozen=True, slots=True)
class CommandSinkRow:
    """一条命令 matcher 的效果 sink 登记行。"""

    entry_id: str
    source_path: str
    source_symbol: str
    effect_id: str
    owner_module: str
    sink_kind: str
    work_category: str
    acceptance_anchor: str


_REPLY = ("group_message_reply", "transient_interaction")
_WRITE_GROUP = ("persistent_group_write", "persistent_group_work")

#: (owner, module_stem, source_symbol, (sink_kind, work_category))
_RAW: tuple[tuple[str, str, str, tuple[str, str]], ...] = (
    ("komari_help", "commands", "help_cmd", _REPLY),
    ("komari_help", "commands", "help_list_cmd", _REPLY),
    ("komari_help", "commands", "help_refresh_cmd", _REPLY),
    ("sr", "__init__", "sr", _REPLY),
    ("sr", "__init__", "sr_custom", _WRITE_GROUP),
    ("sr", "__init__", "sr_manage", _WRITE_GROUP),
    ("character_binding", "commands", "bind", _REPLY),
    ("character_binding", "commands", "bind_set", _WRITE_GROUP),
    ("character_binding", "commands", "bind_del", _WRITE_GROUP),
    ("character_binding", "commands", "bind_list", _REPLY),
    ("user_ban", "commands", "ban_matcher", _WRITE_GROUP),
    ("user_ban", "commands", "unban_matcher", _WRITE_GROUP),
)

_COMMAND_EFFECT_SINK_CENSUS: tuple[CommandSinkRow, ...] = tuple(
    CommandSinkRow(
        entry_id=f"matcher.{owner}.{module_stem}.{symbol}",
        source_path=f"komari_bot/plugins/{owner}/{module_stem}.py",
        source_symbol=symbol,
        effect_id=f"group_admission.effect.command.{owner}.{symbol}",
        owner_module=f"komari_bot.plugins.{owner}",
        sink_kind=sink_kind,
        work_category=work_category,
        acceptance_anchor=_COMMAND_ANCHOR,
    )
    for owner, module_stem, symbol, (sink_kind, work_category) in _RAW
)


#: 公开 census 名：供命令效果 manifest / 验收测试单一真源引用。
COMMAND_EFFECT_SINK_CENSUS: tuple[CommandSinkRow, ...] = (
    _COMMAND_EFFECT_SINK_CENSUS
)
