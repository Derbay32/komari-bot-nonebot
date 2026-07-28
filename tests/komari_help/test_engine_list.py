"""HelpEngine 列表查询测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from komari_bot.plugins.komari_help.engine import HelpEngine


class _FakeListPool:
    def __init__(self) -> None:
        self.fetchval_calls: list[tuple[str, tuple[object, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[object, ...]]] = []

    def acquire(self) -> "_FakeListPool":
        return self

    async def __aenter__(self) -> "_FakeListPool":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb

    async def fetchval(self, query: str, *args: object) -> int:
        self.fetchval_calls.append((query, args))
        return 1

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        self.fetch_calls.append((query, args))
        timestamp = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)
        return [
            {
                "id": 1,
                "category": "command",
                "plugin_name": "character_binding",
                "keywords": ["绑定"],
                "title": "角色绑定",
                "content": "/bind set",
                "notes": None,
                "is_auto_generated": False,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        ]


def test_list_help_escapes_like_wildcards() -> None:
    engine = HelpEngine()
    pool = _FakeListPool()
    engine._pool = pool

    items, total = asyncio.run(
        engine.list_help(limit=10, offset=0, query=r"100%_x\tag")
    )

    count_query, count_args = pool.fetchval_calls[0]
    _data_query, data_args = pool.fetch_calls[0]

    assert total == 1
    assert items[0].title == "角色绑定"
    assert "title ILIKE $1 ESCAPE '\\'" in count_query
    assert "content ILIKE $1 ESCAPE '\\'" in count_query
    assert "COALESCE(plugin_name, '') ILIKE $1 ESCAPE '\\'" in count_query
    assert "keyword ILIKE $1 ESCAPE '\\'" in count_query
    assert count_args == (r"%100\%\_x\\tag%",)
    assert data_args == (r"%100\%\_x\\tag%", 10, 0)
