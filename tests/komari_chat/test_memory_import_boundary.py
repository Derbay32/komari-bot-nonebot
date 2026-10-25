"""ADR-0006 边界合规验收测试（KOMARIBOT-12）。

komari_chat 不得深 import komari_memory 的内部子模块（config_schema /
core.retry / services 等）；跨插件引用一律走 komari_memory 顶层包暴露面
（``__all__``），所需符号必须在顶层可导入。AGENTS.md 跨插件 import 边界
条款须明文记载采用的方案，消除「豁免条款与 config_interface 实践」张力。
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KOMARI_CHAT_PLUGIN_DIR = PROJECT_ROOT / "komari_bot" / "plugins" / "komari_chat"
KOMARI_MEMORY_INIT = (
    PROJECT_ROOT / "komari_bot" / "plugins" / "komari_memory" / "__init__.py"
)
AGENTS_MD = PROJECT_ROOT / "AGENTS.md"

MEMORY_PACKAGE = "komari_bot.plugins.komari_memory"
CONTEXT_PACKAGE = "komari_bot.plugins.komari_context"
CONTEXT_INIT = (
    PROJECT_ROOT / "komari_bot" / "plugins" / "komari_context" / "__init__.py"
)

#: komari_chat 消费、且必须由 komari_memory 顶层暴露面提供的符号。
REQUIRED_MEMORY_SYMBOLS = ("MemoryService",)
REQUIRED_CONTEXT_SYMBOLS = (
    "KomariContextConfigSchema",
    "MessageSchema",
    "RecentContextStore",
    "get_context_config",
    "get_context_store",
)


def _iter_chat_modules() -> list[Path]:
    return sorted(KOMARI_CHAT_PLUGIN_DIR.rglob("*.py"))


def _all_symbols(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    all_names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.List | ast.Tuple)
            and any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in node.targets
            )
        ):
            all_names.update(
                element.value
                for element in node.value.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            )
    return all_names


def _deep_import_offenders(package: str) -> list[str]:
    """收集 komari_chat 中指向指定插件内部子模块的 import。

    覆盖 ``from ... import`` 与 ``import ...`` 两种形态；位于
    TYPE_CHECKING 块内的深 import 同样计为违规。
    """
    offenders: list[str] = []
    for path in _iter_chat_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith(package + "."):
                    offenders.append(f"{path.name}:{node.lineno}: from {module}")
            elif isinstance(node, ast.Import):
                offenders.extend(
                    f"{path.name}:{node.lineno}: import {alias.name}"
                    for alias in node.names
                    if alias.name.startswith(package + ".")
                )
    return offenders


def test_komari_chat_has_no_deep_imports_into_komari_memory_or_context() -> None:
    """komari_chat 不得深 import 两个资源插件的内部子模块。"""
    assert _deep_import_offenders(MEMORY_PACKAGE) == []
    assert _deep_import_offenders(CONTEXT_PACKAGE) == []


def test_komari_memory_top_level_all_exposes_only_required_symbols() -> None:
    """旧共享符号已移出 memory；仅当前聊天依赖仍由顶层暴露。"""
    all_names = _all_symbols(KOMARI_MEMORY_INIT)

    missing = [name for name in REQUIRED_MEMORY_SYMBOLS if name not in all_names]
    assert missing == [], f"komari_memory 顶层 __all__ 缺少符号: {missing}"
    assert not {"MessageSchema", "RedisManager", "retry_async"} & all_names


def test_komari_context_top_level_all_exposes_shared_owner_symbols() -> None:
    all_names = _all_symbols(CONTEXT_INIT)

    missing = [name for name in REQUIRED_CONTEXT_SYMBOLS if name not in all_names]
    assert missing == [], f"komari_context 顶层 __all__ 缺少符号: {missing}"


def test_komari_chat_imports_shared_symbols_from_top_level_packages() -> None:
    """聊天对 memory/context 的符号引用必须走各自顶层包。"""
    memory_imports: set[str] = set()
    context_imports: set[str] = set()
    for path in _iter_chat_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module == MEMORY_PACKAGE:
                memory_imports.update(alias.name for alias in node.names)
            elif node.module == CONTEXT_PACKAGE:
                context_imports.update(alias.name for alias in node.names)

    assert set(REQUIRED_MEMORY_SYMBOLS) <= memory_imports
    assert {
        "MessageSchema",
        "RecentContextStore",
        "get_context_config",
    } <= context_imports


def _agents_md_boundary_paragraph() -> str:
    """截取 AGENTS.md 中跨插件 import 边界条款所在段落。"""
    text = AGENTS_MD.read_text(encoding="utf-8")
    start = text.find("跨插件 import 边界")
    assert start != -1, "AGENTS.md 缺少跨插件 import 边界条款"
    end = text.find("\n\n", start)
    return text[start : end if end != -1 else start + 2000]


def test_agents_md_documents_the_adopted_boundary_scheme() -> None:
    """边界条款明文记载方案：顶层暴露面覆盖 komari_chat → komari_memory。

    管理插件豁免条款保留；同时条款必须点名 komari_chat / komari_context
    的顶层暴露面实践，使聊天配置读取不再依赖管理插件豁免。
    """
    paragraph = _agents_md_boundary_paragraph()
    assert "豁免" in paragraph, "管理插件豁免条款必须保留"
    assert "komari_memory" in paragraph, (
        "边界条款必须明文记载 komari_memory 顶层暴露面方案"
    )
