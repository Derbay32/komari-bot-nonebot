"""自定义提案仓库初始化与发布状态 SQL 测试。"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from komari_bot.plugins.komari_custom import proposal_repository as repository_module
from komari_bot.plugins.komari_custom.proposal_repository import ProposalRepository


class _FakeConnection:
    def __init__(self, *, schema_error: Exception | None = None) -> None:
        self.schema_error = schema_error
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[object, ...]]] = []

    async def execute(self, query: str, *args: object) -> str:
        self.execute_calls.append((query, args))
        if self.schema_error is not None:
            raise self.schema_error
        return "OK"

    async def fetchrow(self, query: str, *args: object) -> None:
        self.fetchrow_calls.append((query, args))


class _FakeAcquire:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _FakeConnection:
        return self.connection

    async def __aexit__(
        self,
        _exc_type: object,
        _exc: object,
        _tb: object,
    ) -> None:
        return None


class _FakePool:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection
        self.close_calls = 0

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self.connection)

    async def close(self) -> None:
        self.close_calls += 1


@pytest.mark.asyncio
async def test_initialize_is_concurrency_safe_and_close_releases_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_pools: list[_FakePool] = []

    async def _create_pool(*_args: object, **_kwargs: object) -> Any:
        await asyncio.sleep(0)
        pool = _FakePool(_FakeConnection())
        created_pools.append(pool)
        return pool

    monkeypatch.setattr(repository_module, "create_postgres_pool", _create_pool)
    repository = ProposalRepository()

    await asyncio.gather(repository.initialize(), repository.initialize())

    assert len(created_pools) == 1
    assert len(created_pools[0].connection.execute_calls) == 1
    assert repository._pool is created_pools[0]

    await repository.close()

    assert created_pools[0].close_calls == 1
    assert repository._pool is None


@pytest.mark.asyncio
async def test_initialize_closes_temporary_pool_when_schema_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _FakePool(_FakeConnection(schema_error=RuntimeError("模拟建表失败")))

    async def _create_pool(*_args: object, **_kwargs: object) -> Any:
        return pool

    monkeypatch.setattr(repository_module, "create_postgres_pool", _create_pool)
    repository = ProposalRepository()

    with pytest.raises(RuntimeError, match="模拟建表失败"):
        await repository.initialize()

    assert pool.close_calls == 1
    assert repository._pool is None


@pytest.mark.asyncio
async def test_claim_publication_uses_idempotency_key_and_lease_guard() -> None:
    connection = _FakeConnection()
    repository = ProposalRepository()
    repository._pool = cast("Any", _FakePool(connection))

    result = await repository.claim_publication(
        publication_key="stable-key",
        publication_token="claim-token",
        group_id=100,
        proposer_id=200,
        proposer_name="测试用户",
        title="标题",
        content="正文",
        required_votes=3,
        expire_hours=2,
        lease_seconds=300,
    )

    assert result is None
    query, args = connection.fetchrow_calls[0]
    assert "ON CONFLICT (publication_key) DO UPDATE" in query
    assert "status = 'failed'" in query
    assert "status = 'publishing'" in query
    assert "publication_started_at IS NULL" in query
    assert args[0:2] == ("stable-key", "claim-token")
    assert args[-1] == 300


@pytest.mark.asyncio
async def test_complete_publication_requires_current_claim_token() -> None:
    connection = _FakeConnection()
    repository = ProposalRepository()
    repository._pool = cast("Any", _FakePool(connection))

    result = await repository.complete_publication(9, 7788, "claim-token")

    assert result is None
    query, args = connection.fetchrow_calls[0]
    assert "status = 'voting'" in query
    assert "status = 'publishing'" in query
    assert "publication_token = $3" in query
    assert "RETURNING *" in query
    assert args == (9, 7788, "claim-token")
