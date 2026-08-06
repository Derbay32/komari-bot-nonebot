"""角色名绑定 JSON 迁移脚本测试。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts/migrate_character_binding_to_pg.py"


def _load_script_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "tests.scripts.migrate_character_binding_to_pg",
        SCRIPT_PATH,
    )
    if spec is None or spec.loader is None:
        raise AssertionError

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeConnection:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []
        self.closed = False

    async def execute(self, query: str, *args: object) -> str:
        self.execute_calls.append((query, args))
        return "OK"

    async def close(self) -> None:
        self.closed = True


def test_migration_upserts_valid_entries_and_skips_invalid_ones(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    module = _load_script_module()
    bindings_path = tmp_path / "bindings.json"
    bindings_path.write_text(
        json.dumps(
            {
                "42": "泉此方",
                "10086": "角色\n名",
                "114514": None,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    connection = _FakeConnection()
    connect_kwargs: dict[str, object] = {}

    async def _fake_connect(**kwargs: object) -> _FakeConnection:
        connect_kwargs.update(kwargs)
        return connection

    monkeypatch.setenv(
        "SQLALCHEMY_DATABASE_URL",
        "postgresql+asyncpg://user:pass@localhost:5432/komari_bot",
    )
    monkeypatch.setattr(module.asyncpg, "connect", _fake_connect)

    stats = asyncio.run(module.migrate_bindings(bindings_path=bindings_path))

    assert stats == {"written": 1, "skipped": 2}
    assert connection.closed is True
    assert connect_kwargs["dsn"] == (
        "postgresql+asyncpg://user:pass@localhost:5432/komari_bot"
    )
    assert any(
        "CREATE TABLE IF NOT EXISTS komari_character_bindings" in query
        for query, _args in connection.execute_calls
    )
    assert ("42", "泉此方") in [args for _query, args in connection.execute_calls]


def test_migration_raises_when_dsn_unconfigured(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    module = _load_script_module()
    bindings_path = tmp_path / "bindings.json"
    bindings_path.write_text('{"42": "泉此方"}', encoding="utf-8")
    monkeypatch.delenv("SQLALCHEMY_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="SQLALCHEMY_DATABASE_URL"):
        asyncio.run(module.migrate_bindings(bindings_path=bindings_path))
