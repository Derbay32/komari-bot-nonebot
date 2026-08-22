"""TSK-225 依赖边界与无网络验收（AC-8 / AC-9）。

AC-8：低层 LLM / Search / OneBot / Embedding Adapter 目录不得 import
``komari_bot.plugins.group_admission``，也不得引用 ``adjudicate``（策略
无感、不猜群）；准入只由持有归属的编排 module 放置。
AC-9：本票新增聊天准入测试文件不得 import 真实网络 / 存储客户端。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.group_admission_acceptance

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KOMARI = PROJECT_ROOT / "komari_bot"
TESTS = PROJECT_ROOT / "tests" / "group_admission"

ADAPTER_DIRS = (
    "llm",
    "plugins/llm_provider",
    "plugins/embedding_provider",
    "plugins/komari_search",
    "onebot",
)

def _adapter_modules() -> list[Path]:
    files = []
    for rel in ADAPTER_DIRS:
        d = KOMARI / rel
        if not d.is_dir():
            continue
        files.extend(sorted(d.rglob("*.py")))
    return files

def _abs_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                continue
            if node.module:
                out.append(node.module)
    return out

def _abs_import_targets(path: Path) -> list[str]:
    return [
        t
        for t in _abs_imports(path)
        if t == "komari_bot.plugins.group_admission"
        or t.startswith("komari_bot.plugins.group_admission.")
    ]


def _has_ident(path: Path, ident: str) -> bool:
    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == ident:
            return True
    return False


def test_adapters_do_not_import_or_call_admission() -> None:
    offenders = []
    for p in _adapter_modules():
        offenders.extend(
            f"{p.name}: import {t}" for t in _abs_import_targets(p)
        )
        if _has_ident(p, "adjudicate"):
            offenders.append(f"{p.name}: 引用 adjudicate")
    assert offenders == [], (
        "低层 Adapter 不得 import 或调用准入: {offenders}"
    )

CHAT_ADMISSION_FILES = (
    "test_chat_effect_admission.py",
    "test_chat_effect_manifest.py",
    "chat_admission_support.py",
    "chat_effect_census.py",
)

FORBIDDEN_CLIENT_ROOTS = {
    "aiohttp", "httpx", "requests", "redis",
    "aioredis", "asyncpg", "sqlalchemy",
    "sqlmodel", "nonebot_plugin_orm",
}

def test_chat_admission_tests_do_not_open_network_or_storage() -> None:
    offenders = []
    for name in CHAT_ADMISSION_FILES:
        p = TESTS / name
        for t in _abs_imports(p):
            root = t.split(".")[0]
            if root in FORBIDDEN_CLIENT_ROOTS:
                offenders.append(f"{name}: import {t}")
    assert offenders == [], f"无真实外呼保证被破坏: {offenders}"
