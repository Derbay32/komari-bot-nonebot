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

#: komari_chat 消费、且必须由 komari_memory 顶层暴露面提供的符号。
REQUIRED_TOP_LEVEL_SYMBOLS = (
    "KomariMemoryConfigSchema",
    "MemoryService",
    "MessageSchema",
    "RedisManager",
    "retry_async",
)


def _iter_chat_modules() -> list[Path]:
    return sorted(KOMARI_CHAT_PLUGIN_DIR.rglob("*.py"))


def _deep_import_offenders() -> list[str]:
    """收集 komari_chat 中指向 komari_memory 内部子模块的 import。

    覆盖 ``from ... import`` 与 ``import ...`` 两种形态；位于
    TYPE_CHECKING 块内的深 import 同样计为违规。
    """
    offenders: list[str] = []
    for path in _iter_chat_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith(MEMORY_PACKAGE + "."):
                    offenders.append(f"{path.name}:{node.lineno}: from {module}")
            elif isinstance(node, ast.Import):
                offenders.extend(
                    f"{path.name}:{node.lineno}: import {alias.name}"
                    for alias in node.names
                    if alias.name.startswith(MEMORY_PACKAGE + ".")
                )
    return offenders


def test_komari_chat_has_no_deep_imports_into_komari_memory() -> None:
    """komari_chat 全插件不存在指向 komari_memory 内部子模块的 import。"""
    assert _deep_import_offenders() == []


def test_komari_memory_top_level_all_exposes_required_symbols() -> None:
    """komari_chat 所需符号全部列入 komari_memory 顶层 ``__all__``。"""
    tree = ast.parse(KOMARI_MEMORY_INIT.read_text(encoding="utf-8"))
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
                if isinstance(element, ast.Constant)
                and isinstance(element.value, str)
            )

    missing = [name for name in REQUIRED_TOP_LEVEL_SYMBOLS if name not in all_names]
    assert missing == [], f"komari_memory 顶层 __all__ 缺少符号: {missing}"


def test_komari_chat_imports_memory_symbols_from_top_level_only() -> None:
    """komari_chat 对所需符号的引用必须来自 komari_memory 顶层包。"""
    top_level_imports: set[str] = set()
    for path in _iter_chat_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == MEMORY_PACKAGE
            ):
                top_level_imports.update(alias.name for alias in node.names)

    used = set(REQUIRED_TOP_LEVEL_SYMBOLS)
    missing = sorted(name for name in used if name not in top_level_imports)
    assert missing == [], (
        f"以下符号未见从 komari_memory 顶层包 import: {missing}"
    )


def _agents_md_boundary_paragraph() -> str:
    """截取 AGENTS.md 中跨插件 import 边界条款所在段落。"""
    text = AGENTS_MD.read_text(encoding="utf-8")
    start = text.find("跨插件 import 边界")
    assert start != -1, "AGENTS.md 缺少跨插件 import 边界条款"
    end = text.find("\n\n", start)
    return text[start : end if end != -1 else start + 2000]


def test_agents_md_documents_the_adopted_boundary_scheme() -> None:
    """边界条款明文记载方案：顶层暴露面覆盖 komari_chat → komari_memory。

    管理插件豁免条款保留；同时条款必须点名 komari_memory 的顶层暴露面
    实践（配置 Schema 与共享工具符号），使 komari_chat 的配置读取不再
    依赖管理插件豁免。
    """
    paragraph = _agents_md_boundary_paragraph()
    assert "豁免" in paragraph, "管理插件豁免条款必须保留"
    assert "komari_memory" in paragraph, (
        "边界条款必须明文记载 komari_memory 顶层暴露面方案"
    )
