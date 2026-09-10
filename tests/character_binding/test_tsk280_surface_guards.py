"""TSK-280 控制面约束守卫。

本文件证明「缺失业务模块」RED 不是夹具/依赖问题：既有的 TSK-276 共享锁、
BindingTransaction、轮盘 storage 与管理鉴权/审计 seam 在无服务、无新模块时
全部可导入。守卫固定 TSK-280 的负面约束：不新增 debug 修复入口、不新增
matcher、修复服务不得用 importlib 拼接隐藏对轮盘的依赖、生命周期归管理
装配所有（后两条对当前生产为正确 RED）。
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
MANAGEMENT_ROOT = PROJECT_ROOT / "komari_bot" / "plugins" / "komari_management"
BINDING_ROOT = PROJECT_ROOT / "komari_bot" / "plugins" / "character_binding"
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


def _module_all_exports(init_path: Path) -> set[str]:
    """AST 解析插件 ``__init__.__all__`` 字符串字面量集合。"""
    tree = ast.parse(init_path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in node.targets
        ):
            continue
        if isinstance(node.value, ast.List):
            return {
                element.value
                for element in node.value.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            }
    return set()


def test_management_lifecycle_cross_plugin_imports_are_top_level_only() -> None:
    """管理装配绑定修复生命周期只允许跨插件顶层公开 ``__all__`` 引用。

    ``komari_bot.plugins.<plugin>.<submodule>`` 直属深 import 与未被目标插件
    顶层 ``__all__`` 暴露的符号引用都必须为 0：管理装配经既有顶层暴露面取
    ``BindingRepairService`` / ``set_binding_repair_service`` / 轮盘公共 seam。
    """
    lifecycle = MANAGEMENT_ROOT / "binding_repair_lifecycle.py"
    tree = ast.parse(lifecycle.read_text(encoding="utf-8"))
    plugin_prefix = "komari_bot.plugins."
    plugin_root = PROJECT_ROOT / "komari_bot" / "plugins"
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if not node.module.startswith(plugin_prefix):
                continue
            parts = node.module.split(".")
            if len(parts) != 3:
                violations.append(f"{node.module}: 跨插件深 import 子模块")
                continue
            exported = _module_all_exports(plugin_root / parts[2] / "__init__.py")
            missing = sorted(
                alias.name for alias in node.names if alias.name not in exported
            )
            if missing:
                violations.append(f"{node.module}: 顶层 __all__ 未暴露 {missing}")
        elif isinstance(node, ast.Import):
            violations.extend(
                f"{alias.name}: 跨插件深 import 模块"
                for alias in node.names
                if alias.name.startswith(plugin_prefix)
                and len(alias.name.split(".")) != 3
            )
    assert not violations, "\n".join(violations)
    # 生命周期依赖的修复服务符号必须是 character_binding 顶层公开面成员。
    binding_exports = _module_all_exports(BINDING_ROOT / "__init__.py")
    assert "BindingRepairService" in binding_exports
    assert "set_binding_repair_service" in binding_exports


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


def test_repair_module_does_not_evade_roulette_dependency_with_dynamic_import() -> None:
    """修复服务不得用 importlib 字符串拼接隐藏 import 绕架构：
    对局状态必须经构造注入的 game_state_reader（真实 276 公共 seam）。"""
    repair_path = (
        PROJECT_ROOT / "komari_bot" / "plugins" / "character_binding" / "repair.py"
    )
    tree = ast.parse(repair_path.read_text(encoding="utf-8"))
    dynamic_imports: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        is_dynamic_import = (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
        ) or (isinstance(node.func, ast.Name) and node.func.id == "__import__")
        if is_dynamic_import:
            dynamic_imports.append(ast.unparse(node))
    assert not dynamic_imports, "\n".join(dynamic_imports)


def test_repair_service_lifecycle_is_owned_by_management_assembly() -> None:
    """修复服务生命周期由管理装配负责（创建注入真实 game_state_reader 并关闭）；
    character_binding 旧生命周期不得持有这反向依赖。"""
    binding_init = (
        PROJECT_ROOT / "komari_bot" / "plugins" / "character_binding" / "__init__.py"
    ).read_text(encoding="utf-8")
    management_root = PROJECT_ROOT / "komari_bot" / "plugins" / "komari_management"
    management_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(management_root.rglob("*.py"))
    )

    # 绑定插件生命周期不再创建/关闭修复服务。
    assert "BindingRepairService(" not in binding_init
    assert "set_binding_repair_service" not in binding_init
    # 管理装配创建修复服务并注入真实 game_state_reader。
    assert "BindingRepairService(" in management_source
    assert "game_state_reader=" in management_source
    # 管理装配负责关闭（set_binding_repair_service(None) 或 service.close()）。
    assert "set_binding_repair_service(" in management_source
