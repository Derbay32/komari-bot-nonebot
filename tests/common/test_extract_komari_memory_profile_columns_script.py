"""画像列提取脚本测试。"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts/extract_komari_memory_profile_columns.py"


def _load_script_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "tests.scripts.extract_komari_memory_profile_columns_script",
        SCRIPT_PATH,
    )
    if spec is None or spec.loader is None:
        raise AssertionError

    module = importlib.util.module_from_spec(spec)
    original_module = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        if original_module is not None:
            sys.modules[spec.name] = original_module
        else:
            sys.modules.pop(spec.name, None)


class _FakeConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *args: Any) -> None:
        self.executed.append((query, args))


class _FakeAcquireContext:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConnection:
        return self._conn

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        del exc_type, exc, tb


class _FakePool:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    def acquire(self) -> _FakeAcquireContext:
        return _FakeAcquireContext(self._conn)


def test_migrate_profile_row_converts_updated_at_string_to_datetime() -> None:
    module = _load_script_module()
    conn = _FakeConnection()
    pool = _FakePool(conn)
    row = module.ProfileRow(
        user_id="10001",
        group_id="821560570",
        value='{"version":1}',
        profile_version=None,
        profile_payload_user_id=None,
        profile_display_name=None,
        profile_traits=None,
        profile_updated_at=None,
    )

    asyncio.run(
        module._migrate_profile_row(
            pool,
            row=row,
            normalized_profile={
                "version": 1,
                "display_name": "用户10001",
                "traits": {"喜欢的食物": {"value": "布丁"}},
                "updated_at": "2026-04-11T17:30:56.737014+00:00",
            },
            payload_user_id="10001",
        )
    )

    assert len(conn.executed) == 1
    _, args = conn.executed[0]
    updated_at_arg = args[8]
    assert isinstance(updated_at_arg, datetime)
    assert updated_at_arg == datetime(
        2026,
        4,
        11,
        17,
        30,
        56,
        737014,
        tzinfo=UTC,
    )


def test_normalize_profile_updated_at_supports_z_suffix() -> None:
    module = _load_script_module()

    result = module._normalize_profile_updated_at("2026-04-11T17:30:56Z")

    assert result == datetime(2026, 4, 11, 17, 30, 56, tzinfo=UTC)
