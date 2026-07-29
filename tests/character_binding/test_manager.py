"""角色名绑定 PostgreSQL 存储与内存快照测试。"""

from __future__ import annotations

import asyncio

import pytest

from komari_bot.plugins.character_binding import database as database_module
from komari_bot.plugins.character_binding import manager as manager_module
from komari_bot.plugins.character_binding.manager import (
    MAX_CHARACTER_NAME_LENGTH,
    BindingPersistenceError,
    CharacterBindingManager,
    CharacterNameValidationError,
)


class _FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class _FakeConnection:
    def __init__(
        self,
        *,
        rows: list[dict[str, str]] | None = None,
        delete_results: list[str] | None = None,
    ) -> None:
        self.rows = list(rows or [])
        self.delete_results = list(delete_results or [])
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.fail_writes = False

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()

    async def fetchval(self, query: str, *args: object) -> None:
        self.calls.append((query, args))

    async def fetch(
        self,
        query: str,
        *args: object,
    ) -> list[dict[str, str]]:
        self.calls.append((query, args))
        return list(self.rows)

    async def execute(self, query: str, *args: object) -> str:
        self.calls.append((query, args))
        if self.fail_writes and (
            "INSERT INTO komari_character_bindings" in query
            or "DELETE FROM komari_character_bindings" in query
        ):
            msg = "模拟数据库写入失败"
            raise RuntimeError(msg)
        if "DELETE FROM komari_character_bindings" in query:
            return self.delete_results.pop(0) if self.delete_results else "DELETE 0"
        return "OK"


class _FakeAcquire:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _FakeConnection:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        return None


class _FakePool:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection
        self.closed = False

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self.connection)

    async def close(self) -> None:
        self.closed = True


def _install_fake_pool(
    monkeypatch: pytest.MonkeyPatch,
    connection: _FakeConnection,
) -> _FakePool:
    pool = _FakePool(connection)

    async def _create_pool(_config: object) -> _FakePool:
        return pool

    monkeypatch.setattr(database_module, "create_postgres_pool", _create_pool)
    monkeypatch.setattr(
        database_module,
        "get_shared_database_config",
        lambda: object(),
    )
    return pool


@pytest.mark.asyncio
async def test_initialize_creates_table_and_loads_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(
        rows=[
            {"user_id": "42", "character_name": "泉此方"},
            {"user_id": "10086", "character_name": "柊镜"},
        ]
    )
    pool = _install_fake_pool(monkeypatch, connection)
    manager = CharacterBindingManager()

    await asyncio.gather(*(manager.initialize() for _ in range(10)))

    assert manager.list_bindings() == {"42": "泉此方", "10086": "柊镜"}
    assert (
        sum(
            "CREATE TABLE IF NOT EXISTS komari_character_bindings" in query
            for query, _args in connection.calls
        )
        == 1
    )
    assert any("pg_advisory_xact_lock" in query for query, _args in connection.calls)

    await manager.close()
    assert pool.closed is True


@pytest.mark.asyncio
async def test_upsert_updates_snapshot_after_database_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection()
    _install_fake_pool(monkeypatch, connection)
    manager = CharacterBindingManager()
    await manager.initialize()

    await manager.set_character_name("42", "泉此方")

    assert manager.list_bindings() == {"42": "泉此方"}
    assert any(
        "ON CONFLICT (user_id) DO UPDATE" in query and args == ("42", "泉此方")
        for query, args in connection.calls
    )


@pytest.mark.asyncio
async def test_delete_result_controls_snapshot_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(
        rows=[{"user_id": "42", "character_name": "泉此方"}],
        delete_results=["DELETE 0", "DELETE 1"],
    )
    _install_fake_pool(monkeypatch, connection)
    manager = CharacterBindingManager()
    await manager.initialize()

    assert await manager.remove_character_name("404") is False
    assert manager.list_bindings() == {"42": "泉此方"}

    assert await manager.remove_character_name("42") is True
    assert manager.list_bindings() == {}


@pytest.mark.asyncio
async def test_database_failure_raises_and_keeps_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(
        rows=[{"user_id": "42", "character_name": "泉此方"}],
    )
    _install_fake_pool(monkeypatch, connection)
    manager = CharacterBindingManager()
    await manager.initialize()
    connection.fail_writes = True

    with pytest.raises(BindingPersistenceError, match="角色绑定保存失败"):
        await manager.set_character_name("10086", "柊镜")
    with pytest.raises(BindingPersistenceError, match="角色绑定保存失败"):
        await manager.remove_character_name("42")

    assert manager.list_bindings() == {"42": "泉此方"}


@pytest.mark.asyncio
async def test_postgres_unavailable_degrades_to_empty_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fail_create_pool(_config: object) -> None:
        msg = "模拟 PostgreSQL 不可用"
        raise RuntimeError(msg)

    monkeypatch.setattr(database_module, "create_postgres_pool", _fail_create_pool)
    monkeypatch.setattr(
        database_module,
        "get_shared_database_config",
        lambda: object(),
    )
    manager = CharacterBindingManager()

    await manager.initialize()

    assert manager.list_bindings() == {}
    assert manager.get_character_name("42", "昵称") == "昵称"
    with pytest.raises(BindingPersistenceError, match="角色绑定保存失败"):
        await manager.set_character_name("42", "泉此方")


def test_character_name_fallback_chain() -> None:
    manager = CharacterBindingManager()
    manager._bindings = {"42": "泉此方"}

    assert manager.get_character_name("42", "昵称") == "泉此方"
    assert manager.get_character_name("10086", "柊镜") == "柊镜"
    assert manager.get_character_name("114514") == "114514"
    assert manager.has_binding("42") is True
    assert manager.has_binding("10086") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("character_name", ["角色\n名", "角色\t名", "角色\u2028名"])
async def test_rejects_line_breaks_and_control_characters(
    character_name: str,
) -> None:
    manager = CharacterBindingManager()

    with pytest.raises(CharacterNameValidationError, match="换行或控制字符"):
        await manager.set_character_name("42", character_name)

    assert manager.list_bindings() == {}


@pytest.mark.asyncio
async def test_rejects_name_over_unicode_length_limit() -> None:
    manager = CharacterBindingManager()

    with pytest.raises(CharacterNameValidationError, match="不能超过"):
        await manager.set_character_name(
            "42",
            "鞠" * (MAX_CHARACTER_NAME_LENGTH + 1),
        )

    assert manager.list_bindings() == {}


@pytest.mark.asyncio
async def test_close_clears_singleton_before_closing_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection()
    pool = _install_fake_pool(monkeypatch, connection)
    manager_module.state.manager = None
    manager = manager_module.get_manager()
    await manager.initialize()

    await manager.close()

    assert manager_module.state.manager is None
    assert manager.list_bindings() == {}
    assert pool.closed is True
