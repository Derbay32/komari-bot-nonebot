"""全仓库跨插件引用边界终验测试（ticket #33）。

验收目标（ADR-0006）：
- 任何插件不得 import komari_decision 的内部子模块（services / repositories /
  handlers）；唯一豁免是 komari_management 对配置 Schema 的 import（注册管理
  资源的既定惯例）；
- get_runtime_state 在判定插件之外零残留引用。
"""

from __future__ import annotations

from pathlib import Path

PLUGINS_DIR = (
    Path(__file__).resolve().parents[2] / "komari_bot" / "plugins"
)

FORBIDDEN_IMPORT_MARKERS = (
    "komari_decision.services",
    "komari_decision.repositories",
    "komari_decision.handlers",
)

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


def test_no_residual_get_runtime_state_reference_outside_decision() -> None:
    offenders = [
        f"{plugin}/{module_file.name}"
        for plugin, module_file in _iter_plugin_sources()
        if RETIRED_SYMBOL in module_file.read_text(encoding="utf-8")
    ]
    assert not offenders, f"判定插件之外仍存在 get_runtime_state 引用: {offenders}"
