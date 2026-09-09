"""TSK-280 控制面约束守卫（不 import 未实现业务模块，当前即绿）。

本文件证明「缺失业务模块」RED 不是夹具/依赖问题：既有的 TSK-276 共享锁、
BindingTransaction、轮盘 storage 与管理鉴权/审计 seam 在无服务、无新模块时
全部可导入。守卫还固定 TSK-280 的负面约束：不新增 debug 修复入口、不新增
matcher。
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.character_binding.tsk280_support import (
    REPAIR_API_PREFIX,
    reset_shared_orm_engine,
)
from tests.group_admission.entry_gate_census import MATCHER_ENTRY_CENSUS

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEBUG_COMMANDS = (
    PROJECT_ROOT / "komari_bot" / "plugins" / "komari_debug" / "commands.py"
)
BINDING_MATCHER_ENTRIES = {
    "matcher.character_binding.reply_evidence.reply_evidence_matcher",
    "matcher.character_binding.qq_commands.bind_qq",
}


def _debug_bind_subcommands() -> set[str]:
    """AST 扫描 ``on_command(("debug", "bind", ...))`` 注册的第三段。"""
    tree = ast.parse(DEBUG_COMMANDS.read_text(encoding="utf-8"))
    result: set[str] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "on_command"
        ):
            continue
        args = node.value.args
        if len(args) != 1 or not isinstance(args[0], ast.Tuple):
            continue
        constants = [
            part for part in args[0].elts if isinstance(part, ast.Constant)
        ]
        if len(constants) != 3 or not all(
            isinstance(part.value, str) for part in constants
        ):
            continue
        values = [part.value for part in constants if isinstance(part.value, str)]
        if values[:2] == ["debug", "bind"]:
            result.add(values[2])
    return result


def test_debug_bind_subcommands_are_not_extended() -> None:
    """TSK-280 不新增 debug 修复入口：``.debug bind`` 保持 set/del/list 闭集。"""
    assert _debug_bind_subcommands() == {"set", "del", "list"}


def test_character_binding_matchers_stay_at_frozen_census() -> None:
    """修复控制面是 REST，不新增 matcher：census 中 character_binding 仍两项。"""
    binding_entries = {
        row.entry_id
        for row in MATCHER_ENTRY_CENSUS
        if row.entry_id.startswith("matcher.character_binding.")
    }
    assert binding_entries == BINDING_MATCHER_ENTRIES


async def test_shared_seams_are_importable_without_repair_module() -> None:
    """TSK-280 复用的既有 seam 可导入，证明 RED 来自缺失业务模块。"""
    from komari_bot.db.group_transaction_locks import lock_group_scope
    from komari_bot.management.management_api import create_bearer_auth_dependency
    from komari_bot.management.management_audit import management_audit_span
    from komari_bot.plugins.character_binding import BindingTransaction
    from komari_bot.plugins.komari_roulette import (
        PostgresRouletteStorage,
        RouletteCommandService,
    )

    assert callable(lock_group_scope)
    assert BindingTransaction is not None
    assert PostgresRouletteStorage is not None
    assert RouletteCommandService is not None
    assert callable(create_bearer_auth_dependency)
    assert management_audit_span is not None
    assert REPAIR_API_PREFIX == "/api/v2/character-bindings/repair"
    await reset_shared_orm_engine()
