"""角色名绑定 JSON 迁移脚本测试。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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

    async def _fake_connect(**_kwargs: object) -> _FakeConnection:
        return connection

    monkeypatch.setattr(module.asyncpg, "connect", _fake_connect)
    monkeypatch.setattr(
        module,
        "load_database_config_from_env",
        lambda: SimpleNamespace(
            pg_host="localhost",
            pg_port=5432,
            pg_database="komari_bot",
            pg_user="user",
            pg_password="password",
        ),
    )

    stats = asyncio.run(module.migrate_bindings(bindings_path=bindings_path))

    assert stats == {"written": 1, "skipped": 2}
    assert connection.closed is True
    assert any(
        "CREATE TABLE IF NOT EXISTS komari_character_bindings" in query
        for query, _args in connection.execute_calls
    )
    assert ("42", "泉此方") in [args for _query, args in connection.execute_calls]


def test_migration_uses_explicit_database_config(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    module = _load_script_module()
    bindings_path = tmp_path / "bindings.json"
    bindings_path.write_text('{"42": "泉此方"}', encoding="utf-8")
    config_path = tmp_path / "database.json"
    config_path.write_text("{}", encoding="utf-8")
    connection = _FakeConnection()
    explicit_config = SimpleNamespace(
        pg_host="db",
        pg_port=5432,
        pg_database="komari",
        pg_user="user",
        pg_password="password",
    )

    async def _fake_connect(**_kwargs: object) -> _FakeConnection:
        return connection

    monkeypatch.setattr(module.asyncpg, "connect", _fake_connect)
    monkeypatch.setattr(
        module,
        "load_database_config_from_file",
        lambda path: explicit_config if path == config_path else None,
    )
    monkeypatch.setattr(
        module,
        "load_database_config_from_env",
        lambda: (_ for _ in ()).throw(AssertionError),
    )

    stats = asyncio.run(
        module.migrate_bindings(
            bindings_path=bindings_path,
            database_config_path=config_path,
        )
    )

    assert stats == {"written": 1, "skipped": 0}
