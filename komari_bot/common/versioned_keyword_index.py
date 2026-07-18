"""基于 PostgreSQL 版本戳的不可变关键词索引快照。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

type MutableKeywordIndex = dict[str, set[int]]
type KeywordIndexEntries = Mapping[str, frozenset[int]]
type KeywordIndexLoader = Callable[[Any], Awaitable[MutableKeywordIndex]]

_INDEX_VERSION_QUERY = """
    SELECT version
    FROM komari_search_index_versions
    WHERE index_name = $1
"""
_EMPTY_ENTRIES: KeywordIndexEntries = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class KeywordIndexSnapshot:
    """可由并发读者安全共享的关键词索引快照。"""

    version: int
    entries: KeywordIndexEntries


class VersionedKeywordIndex:
    """通过版本轮询单飞重建关键词索引。"""

    def __init__(
        self,
        index_name: str,
        *,
        check_interval_seconds: float = 1.0,
    ) -> None:
        self._index_name = index_name
        self._check_interval_seconds = check_interval_seconds
        self._snapshot = KeywordIndexSnapshot(version=-1, entries=_EMPTY_ENTRIES)
        self._loaded = False
        self._last_version_check_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def snapshot(self) -> KeywordIndexSnapshot:
        return self._snapshot

    @property
    def entries(self) -> KeywordIndexEntries:
        return self._snapshot.entries

    async def rebuild(self, pool: Any, loader: KeywordIndexLoader) -> bool:
        """立刻检查版本，并在变化时单飞重建。"""
        async with self._lock:
            return await self._refresh_locked(pool, loader)

    async def ensure_fresh(self, pool: Any, loader: KeywordIndexLoader) -> bool:
        """达到轮询间隔后检查跨 worker 版本变化。"""
        if not self._loaded or not self._check_due(monotonic()):
            return False

        async with self._lock:
            if not self._check_due(monotonic()):
                return False
            return await self._refresh_locked(pool, loader)

    async def reset(self) -> None:
        """等待在途重建完成后清空快照。"""
        async with self._lock:
            self._snapshot = KeywordIndexSnapshot(version=-1, entries=_EMPTY_ENTRIES)
            self._loaded = False
            self._last_version_check_at = 0.0

    def _check_due(self, now: float) -> bool:
        return now - self._last_version_check_at >= self._check_interval_seconds

    async def _refresh_locked(
        self,
        pool: Any,
        loader: KeywordIndexLoader,
    ) -> bool:
        async with (
            pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            raw_version = await conn.fetchval(
                _INDEX_VERSION_QUERY,
                self._index_name,
            )
            version = int(raw_version or 0)
            if self._loaded and version == self._snapshot.version:
                self._last_version_check_at = monotonic()
                return False
            mutable_entries = await loader(conn)

        frozen_entries: KeywordIndexEntries = MappingProxyType(
            {
                keyword: frozenset(entry_ids)
                for keyword, entry_ids in mutable_entries.items()
                if entry_ids
            }
        )
        self._snapshot = KeywordIndexSnapshot(
            version=version,
            entries=frozen_entries,
        )
        self._loaded = True
        self._last_version_check_at = monotonic()
        return True
