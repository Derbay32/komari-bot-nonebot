"""TSK-277 AC12：OneBot 旧 /bind 命令入口退役，保留静默证据监听并更新帮助。

旧 `bind` / `bind_set` / `bind_del` / `bind_list` 四个 on_command matcher 必须
物理退役；QQ 绑定向导由 `qq_commands.bind_qq`（on_message）承接，证据监听
`reply_evidence_matcher` 保持注册。帮助条目同步新语法。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.character_binding.test_reply_evidence import (
    _real_character_binding_package,
)
from tests.group_admission.entry_gate_census import MATCHER_ENTRY_CENSUS
from tests.group_admission.registry_isolation_support import (
    registry_isolation_context,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = PROJECT_ROOT / "komari_bot" / "plugins" / "character_binding"
LEGACY_SYMBOLS = ("bind", "bind_set", "bind_del", "bind_list")
EVIDENCE_MATCHER_ENTRY = (
    "matcher.character_binding.reply_evidence.reply_evidence_matcher"
)
QQ_HANDLER_ENTRY = "matcher.character_binding.qq_commands.bind_qq"


def _on_command_assignments() -> dict[str, str]:
    """AST 扫描 character_binding 目录里的 on_command matcher 赋值。"""
    result: dict[str, str] = {}
    for py_file in sorted(PLUGIN_DIR.glob("*.py")):
        tree = ast.parse(py_file.read_text("utf-8"), filename=str(py_file))
        aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "nonebot":
                aliases.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "on_command"
                )
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id in {"on_command", *aliases}
            ):
                result[node.targets[0].id] = py_file.name
    return result


def test_character_binding_has_no_onebot_bind_command_matchers() -> None:
    """AC12：旧四个 on_command 入口退役，禁止换 on_type 或别名逃逸。"""
    assignments = _on_command_assignments()
    assert assignments == {}, f"旧 OneBot 命令入口必须退役: {assignments}"


def test_census_retires_legacy_matchers_and_registers_qq_handler() -> None:
    """AC12：census 27→24、on_command 23→19，新增 QQ handler 明确登记。"""
    entry_ids = {row.entry_id for row in MATCHER_ENTRY_CENSUS}
    for symbol in LEGACY_SYMBOLS:
        legacy_entry = f"matcher.character_binding.commands.{symbol}"
        assert legacy_entry not in entry_ids, f"{legacy_entry} 必须从 census 移除"
    assert EVIDENCE_MATCHER_ENTRY in entry_ids, "静默证据监听必须保留"
    assert QQ_HANDLER_ENTRY in entry_ids, "QQ handler 必须显式登记"
    qq_row = next(row for row in MATCHER_ENTRY_CENSUS if row.entry_id == QQ_HANDLER_ENTRY)
    assert qq_row.factory == "on_message", "禁止换 on_type 逃扫描"
    assert qq_row.source_path.endswith("qq_commands.py")


def test_binding_help_metadata_documents_new_wizard_syntax() -> None:
    """AC12：帮助条目同步新 /bind 语法，不再宣传旧 set/del/list。"""
    with registry_isolation_context(), _real_character_binding_package() as package:
        metadata = getattr(package, "__plugin_meta__", None)
        assert metadata is not None
        usage = str(getattr(metadata, "usage", "") or "")
        for command in (
            "/bind rename",
            "/bind unbind",
            "/bind confirm",
            "/bind cancel",
        ):
            assert command in usage, f"帮助缺少 {command}: {usage!r}"
        for legacy in (".bind set", ".bind del", ".bind list"):
            assert legacy not in usage, f"帮助仍宣传旧语法 {legacy}: {usage!r}"


@pytest.mark.asyncio
async def test_real_package_keeps_silent_evidence_listener_only() -> None:
    """AC12：真实包导入只保留静默证据监听与 QQ handler，无旧命令 matcher。"""
    import nonebot.matcher as matcher_module

    with registry_isolation_context(), _real_character_binding_package():
        matchers = [
            matcher
            for matchers in matcher_module.matchers.values()
            for matcher in matchers
            if getattr(getattr(matcher, "_source", None), "module_name", "").startswith(
                "komari_bot.plugins.character_binding"
            )
        ]
        by_module = {
            getattr(getattr(matcher, "_source", None), "module_name", ""): matcher
            for matcher in matchers
        }
        assert (
            "komari_bot.plugins.character_binding.commands" not in by_module
        ), "旧 commands 模块不得再注册 matcher"
        evidence = by_module.get("komari_bot.plugins.character_binding.reply_evidence")
        assert evidence is not None
        assert evidence.type == "message"
        assert evidence.block is False
        qq_handler = by_module.get("komari_bot.plugins.character_binding.qq_commands")
        assert qq_handler is not None
        assert qq_handler.type == "message"
