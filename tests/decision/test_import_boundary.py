"""全仓库跨插件引用边界终验测试（ticket #33）。

验收目标（ADR-0006）：
- 任何插件不得 import komari_decision 的内部子模块（services / repositories /
  handlers）；唯一豁免是 komari_management 对配置 Schema 的 import（注册管理
  资源的既定惯例）；
- 任何生产代码不得从 ``komari_bot.plugins.komari_decision`` 顶层或其内部
  子模块 import / 属性使用已退役的 decision ``get_runtime_state``。该守卫是
  AST/source-aware 的：其他插件暴露的同名符号（例如 group_admission 的顶层
  公开接口）不是违规，字符串/注释提及也不是违规。
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KOMARI_BOT_DIR = PROJECT_ROOT / "komari_bot"
PLUGINS_DIR = KOMARI_BOT_DIR / "plugins"

FORBIDDEN_IMPORT_MARKERS = (
    "komari_decision.services",
    "komari_decision.repositories",
    "komari_decision.handlers",
)

DECISION_MODULE = "komari_bot.plugins.komari_decision"
RETIRED_SYMBOL = "get_runtime_state"


def _iter_plugin_sources() -> list[tuple[str, Path]]:
    return [
        (plugin_dir.name, module_file)
        for plugin_dir in sorted(PLUGINS_DIR.iterdir())
        if plugin_dir.is_dir()
        and plugin_dir.name != "komari_decision"
        and (plugin_dir / "__init__.py").exists()
        for module_file in sorted(plugin_dir.rglob("*.py"))
    ]


def test_no_plugin_imports_decision_internal_submodules() -> None:
    offenders = [
        f"{plugin}/{module_file.name}: {marker}"
        for plugin, module_file in _iter_plugin_sources()
        for marker in FORBIDDEN_IMPORT_MARKERS
        if marker in module_file.read_text(encoding="utf-8")
    ]
    assert not offenders, f"存在指向判定插件内部子模块的 import: {offenders}"


# ---------------------------------------------------------------------------
# 已退役 decision get_runtime_state 的 AST-aware 守卫
# ---------------------------------------------------------------------------


def _is_decision_module(target: str) -> bool:
    return target == DECISION_MODULE or target.startswith(f"{DECISION_MODULE}.")


def _resolve_relative_module(
    package: str, level: int, module: str | None
) -> str | None:
    """把 relative import 解析为绝对模块名；无法解析时返回 None。"""
    parts = package.split(".")
    stripped = len(parts) - (level - 1)
    if stripped < 1:
        return None
    base = parts[:stripped]
    return ".".join([*base, module]) if module else ".".join(base)


def _dotted_name(node: ast.expr) -> str | None:
    """重建 Name/Attribute 链的完整点分名；其他表达式返回 None。"""
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def find_retired_decision_symbol_offenders(
    source: str, *, package: str | None = None
) -> list[str]:
    """静态检测源码中 import / 属性使用已退役 decision 符号的位置。

    纯函数（无文件系统副作用，测试可直接以字符串 fixture 驱动）。只有指向
    ``komari_bot.plugins.komari_decision``（顶层包或其任何内部子模块）的
    ``from ... import get_runtime_state`` 以及绑定名上的
    ``...get_runtime_state`` 属性使用才算违规；其他模块的同名符号、
    字符串字面量、注释与 docstring 均不触发。

    传入 ``package``（源码所属包的绝对点分名）时，relative import 也会被
    解析后再判定，因此 decision 包内部的相对残留 import 同样会被抓。
    """
    tree = ast.parse(source)

    decision_bindings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            decision_bindings.update(
                alias.asname or alias.name
                for alias in node.names
                if _is_decision_module(alias.name)
            )

    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            target = node.module if node.level == 0 else None
            if node.level > 0 and package is not None:
                target = _resolve_relative_module(package, node.level, node.module)
            if target and _is_decision_module(target):
                offenders.extend(
                    f"line {node.lineno}: from {target} import {alias.name}"
                    for alias in node.names
                    if alias.name == RETIRED_SYMBOL
                )
        elif isinstance(node, ast.Attribute) and node.attr == RETIRED_SYMBOL:
            dotted = _dotted_name(node.value)
            if dotted is not None and any(
                dotted == binding or dotted.startswith(f"{binding}.")
                for binding in decision_bindings
            ):
                offenders.append(f"line {node.lineno}: {dotted}.{RETIRED_SYMBOL}")
    return offenders


def _package_of(module_file: Path) -> str:
    relative = module_file.relative_to(PROJECT_ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def test_no_production_code_imports_retired_decision_get_runtime_state() -> None:
    """退役符号守卫本体：生产代码对 decision ``get_runtime_state`` 零引用。"""
    offenders = [
        f"{module_file.relative_to(PROJECT_ROOT)}: {offender}"
        for module_file in sorted(KOMARI_BOT_DIR.rglob("*.py"))
        for offender in find_retired_decision_symbol_offenders(
            module_file.read_text(encoding="utf-8"),
            package=_package_of(module_file),
        )
    ]
    assert not offenders, (
        f"存在对已退役 decision get_runtime_state 的 import/使用: {offenders}"
    )


# ---------------------------------------------------------------------------
# 守卫 helper 自身的正/负向单测（字符串 fixture，不触碰生产包）
# ---------------------------------------------------------------------------


def test_helper_flags_direct_import_from_decision_top_level() -> None:
    source = "from komari_bot.plugins.komari_decision import get_runtime_state\n"
    assert find_retired_decision_symbol_offenders(source), (
        "从 decision 顶层 import 退役符号必须被抓"
    )


def test_helper_flags_relative_residual_import_inside_decision() -> None:
    source = "from . import get_runtime_state\n"
    offenders = find_retired_decision_symbol_offenders(
        source, package="komari_bot.plugins.komari_decision"
    )
    assert offenders, "decision 包内部的相对残留 import 必须被抓"
    assert find_retired_decision_symbol_offenders(
        source, package="komari_bot.plugins.group_admission"
    ) == [], "非 decision 包的同名相对 import 不是违规"


def test_helper_flags_aliased_and_internal_imports_from_decision() -> None:
    aliased = (
        "from komari_bot.plugins.komari_decision import get_runtime_state as grs\n"
    )
    internal = (
        "from komari_bot.plugins.komari_decision.services.runtime import "
        "get_runtime_state\n"
    )
    assert find_retired_decision_symbol_offenders(aliased)
    assert find_retired_decision_symbol_offenders(internal)


def test_helper_flags_attribute_use_on_bound_decision_module() -> None:
    source = (
        "import komari_bot.plugins.komari_decision as decision\n"
        "decision.get_runtime_state()\n"
    )
    assert find_retired_decision_symbol_offenders(source)


def test_helper_ignores_same_named_symbol_from_other_plugins() -> None:
    source = "from komari_bot.plugins.group_admission import get_runtime_state\n"
    assert find_retired_decision_symbol_offenders(source) == [], (
        "其他插件顶层同名接口不是违规，守卫不得误杀"
    )


def test_helper_ignores_other_symbols_from_decision() -> None:
    source = (
        "from komari_bot.plugins.komari_decision import DecisionRuntimeState\n"
    )
    assert find_retired_decision_symbol_offenders(source) == []


def test_helper_ignores_string_literal_and_comment_mentions() -> None:
    source = (
        '"""docstring 提及 get_runtime_state 不算违规。"""\n'
        "# 注释提及 komari_bot.plugins.komari_decision.get_runtime_state\n"
        'name = "get_runtime_state"\n'
    )
    assert find_retired_decision_symbol_offenders(source) == []
