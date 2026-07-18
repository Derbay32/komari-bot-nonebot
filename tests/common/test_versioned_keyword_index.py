"""数据库版本化关键词索引测试。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self

import pytest

from komari_bot.common.versioned_keyword_index import VersionedKeywordIndex

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@dataclass
class _SharedStore:
    version: int = 0
    entries: dict[str, set[int]] = field(default_factory=dict)


class _FakeConnection:
    def __init__(self, store: _SharedStore) -> None:
        self.store = store
        self.transaction_options: list[dict[str, object]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb

    def transaction(self, **kwargs: object) -> _FakeConnection:
        self.transaction_options.append(kwargs)
        return self

    async def fetchval(self, query: str, index_name: str) -> int:
        assert "komari_search_index_versions" in query
        assert index_name == "test_index"
        return self.store.version


class _FakePool:
    def __init__(self, store: _SharedStore) -> None:
        self.connection = _FakeConnection(store)

    def acquire(self) -> _FakeConnection:
        return self.connection


def _make_loader(
    store: _SharedStore,
) -> Callable[[object], Awaitable[dict[str, set[int]]]]:
    async def _loader(_conn: object) -> dict[str, set[int]]:
        return {keyword: set(ids) for keyword, ids in store.entries.items()}

    return _loader


@pytest.mark.asyncio
async def test_other_worker_refreshes_to_new_immutable_snapshot() -> None:
    store = _SharedStore(entries={"alpha": {1}})
    pool = _FakePool(store)
    first_worker = VersionedKeywordIndex("test_index", check_interval_seconds=0)
    second_worker = VersionedKeywordIndex("test_index", check_interval_seconds=0)
    loader = _make_loader(store)

    await first_worker.rebuild(pool, loader)
    await second_worker.rebuild(pool, loader)
    previous_snapshot = second_worker.snapshot

    store.entries = {"beta": {2}}
    store.version = 1
    assert await second_worker.ensure_fresh(pool, loader) is True

    assert previous_snapshot.entries == {"alpha": frozenset({1})}
    assert second_worker.entries == {"beta": frozenset({2})}
    assert first_worker.entries == {"alpha": frozenset({1})}
    with pytest.raises(TypeError):
        second_worker.entries["gamma"] = frozenset({3})  # type: ignore[index]


@pytest.mark.asyncio
async def test_concurrent_rebuild_is_single_flight_for_same_version() -> None:
    store = _SharedStore(entries={"alpha": {1}})
    pool = _FakePool(store)
    index = VersionedKeywordIndex("test_index")
    loader_started = asyncio.Event()
    release_loader = asyncio.Event()
    loader_calls = 0

    async def _loader(_conn: object) -> dict[str, set[int]]:
        nonlocal loader_calls
        loader_calls += 1
        loader_started.set()
        await release_loader.wait()
        return {"alpha": {1}}

    first_rebuild = asyncio.create_task(index.rebuild(pool, _loader))
    await loader_started.wait()
    second_rebuild = asyncio.create_task(index.rebuild(pool, _loader))
    release_loader.set()

    assert await first_rebuild is True
    assert await second_rebuild is False
    assert loader_calls == 1


@pytest.mark.asyncio
async def test_failed_rebuild_keeps_previous_snapshot() -> None:
    store = _SharedStore(entries={"alpha": {1}})
    pool = _FakePool(store)
    index = VersionedKeywordIndex("test_index")
    loader = _make_loader(store)
    await index.rebuild(pool, loader)

    store.version = 1

    async def _fail_loader(_conn: object) -> dict[str, set[int]]:
        raise RuntimeError("模拟加载失败")

    with pytest.raises(RuntimeError, match="模拟加载失败"):
        await index.rebuild(pool, _fail_loader)

    assert index.snapshot.version == 0
    assert index.entries == {"alpha": frozenset({1})}
    assert pool.connection.transaction_options[-1] == {
        "isolation": "repeatable_read",
        "readonly": True,
    }
