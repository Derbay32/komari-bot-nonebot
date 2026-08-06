"""角色名绑定 ORM 存储与内存快照测试。

使用实现了 ``CharacterBindingDB`` 公共接口（initialize/load_all/upsert/
delete/close + 未初始化拒绝语义）的内存替身替换真实数据库，验证管理器的
快照发布、写入失败包装与降级契约；SQL 级行为由真实库集成测试
（``test_database_integration.py``）覆盖。
"""

from __future__ import annotations

import asyncio

import pytest

from komari_bot.plugins.character_binding import manager as manager_module
from komari_bot.plugins.character_binding.manager import (
    MAX_CHARACTER_NAME_LENGTH,
    BindingPersistenceError,
    CharacterBindingManager,
    CharacterNameValidationError,
)


class _FakeCharacterBindingDB:
    """CharacterBindingDB 内存替身：忠实建模 ready 状态与失败开关。

    - 未初始化（含初始化失败）时读写抛出 RuntimeError，与真实 ORM 仓储
      的 ``_require_ready`` 语义一致；
    - ``calls`` 记录全部公共方法调用，供断言调用参数与零 DDL 契约。
    """

    def __init__(self, rows: dict[str, str] | None = None) -> None:
        self.bindings = dict(rows or {})
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.fail_initialize = False
        self.fail_writes = False
        self.initialize_calls = 0
        self.closed = False
        self._ready = False

    async def initialize(self) -> None:
        self.initialize_calls += 1
        self.calls.append(("initialize", ()))
        if self.fail_initialize:
            self._ready = False
            msg = "模拟 PostgreSQL 不可用"
            raise RuntimeError(msg)
        self._ready = True

    def _require_ready(self) -> None:
        if not self._ready:
            msg = "character_binding 数据库尚未初始化"
            raise RuntimeError(msg)

    async def load_all(self) -> dict[str, str]:
        self._require_ready()
        self.calls.append(("load_all", ()))
        return dict(self.bindings)

    async def upsert(self, user_id: str, character_name: str) -> None:
        self._require_ready()
        self.calls.append(("upsert", (user_id, character_name)))
        if self.fail_writes:
            msg = "模拟数据库写入失败"
            raise RuntimeError(msg)
        self.bindings[user_id] = character_name

    async def delete(self, user_id: str) -> bool:
        self._require_ready()
        self.calls.append(("delete", (user_id,)))
        if self.fail_writes:
            msg = "模拟数据库写入失败"
            raise RuntimeError(msg)
        return self.bindings.pop(user_id, None) is not None

    async def close(self) -> None:
        self.closed = True
        self.calls.append(("close", ()))


def _install_fake_database(
    monkeypatch: pytest.MonkeyPatch,
    database: _FakeCharacterBindingDB,
) -> _FakeCharacterBindingDB:
    def _create_database() -> _FakeCharacterBindingDB:
        return database

    monkeypatch.setattr(manager_module, "CharacterBindingDB", _create_database)
    return database


@pytest.mark.asyncio
async def test_initialize_creates_only_connectivity_check_and_loads_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _install_fake_database(
        monkeypatch,
        _FakeCharacterBindingDB(
            rows={"42": "泉此方", "10086": "柊镜"},
        ),
    )
    manager = CharacterBindingManager()

    await asyncio.gather(*(manager.initialize() for _ in range(10)))

    assert manager.list_bindings() == {"42": "泉此方", "10086": "柊镜"}
    assert database.initialize_calls == 1
    ddl_keywords = (
        "CREATE TABLE",
        "CREATE INDEX",
        "ALTER TABLE",
        "pg_advisory_xact_lock",
    )
    assert all(
        not any(keyword in repr(call) for keyword in ddl_keywords)
        for call in database.calls
    )

    await manager.close()
    assert database.closed is True


@pytest.mark.asyncio
async def test_upsert_updates_snapshot_after_database_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _install_fake_database(monkeypatch, _FakeCharacterBindingDB())
    manager = CharacterBindingManager()
    await manager.initialize()

    await manager.set_character_name("42", "泉此方")

    assert manager.list_bindings() == {"42": "泉此方"}
    assert ("upsert", ("42", "泉此方")) in database.calls


@pytest.mark.asyncio
async def test_delete_result_controls_snapshot_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database = _install_fake_database(
        monkeypatch,
        _FakeCharacterBindingDB(rows={"42": "泉此方"}),
    )
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
    database = _install_fake_database(
        monkeypatch,
        _FakeCharacterBindingDB(rows={"42": "泉此方"}),
    )
    manager = CharacterBindingManager()
    await manager.initialize()
    database.fail_writes = True

    with pytest.raises(BindingPersistenceError, match="角色绑定保存失败"):
        await manager.set_character_name("10086", "柊镜")
    with pytest.raises(BindingPersistenceError, match="角色绑定保存失败"):
        await manager.remove_character_name("42")

    assert manager.list_bindings() == {"42": "泉此方"}


@pytest.mark.asyncio
async def test_postgres_unavailable_degrades_to_empty_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _install_fake_database(monkeypatch, _FakeCharacterBindingDB())
    database.fail_initialize = True
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
async def test_close_clears_singleton_before_closing_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _install_fake_database(monkeypatch, _FakeCharacterBindingDB())
    manager_module.state.manager = None
    manager = manager_module.get_manager()
    await manager.initialize()

    await manager.close()

    assert manager_module.state.manager is None
    assert manager.list_bindings() == {}
    assert database.closed is True
