"""LLM 客户端连接复用与热切换测试。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from komari_bot.plugins.llm_provider.client_pool import ClientSettings, LLMClientPool


class _FakeManagedClient:
    def __init__(self, *, healthy: bool = True) -> None:
        self.healthy = healthy
        self.healthcheck_models: list[str | None] = []
        self.closed = False

    async def generate_text(self, **_kwargs: Any) -> str:
        return "ok"

    async def generate_text_with_messages(self, **_kwargs: Any) -> str:
        return "ok"

    async def test_connection(self, model: str | None = None) -> bool:
        self.healthcheck_models.append(model)
        return self.healthy

    async def close(self) -> None:
        self.closed = True


class _BlockingHealthClient(_FakeManagedClient):
    def __init__(
        self,
        *,
        healthcheck_started: asyncio.Event,
        healthcheck_release: asyncio.Event,
    ) -> None:
        super().__init__()
        self._healthcheck_started = healthcheck_started
        self._healthcheck_release = healthcheck_release

    async def test_connection(self, model: str | None = None) -> bool:
        self.healthcheck_models.append(model)
        self._healthcheck_started.set()
        await self._healthcheck_release.wait()
        return self.healthy


def _settings(token: str, *, model: str = "chat") -> ClientSettings:
    return ClientSettings(
        api_token=token,
        base_url="https://llm.example.com/v1",
        timeout_seconds=30.0,
        healthcheck_model=model,
    )


def test_pool_reuses_client_and_closes_it_on_shutdown() -> None:
    current_settings = _settings("token-a")
    clients: list[_FakeManagedClient] = []

    def _factory(_value: ClientSettings) -> _FakeManagedClient:
        client = _FakeManagedClient()
        clients.append(client)
        return client

    pool = LLMClientPool(
        settings_getter=lambda: current_settings,
        client_factory=_factory,
    )

    async def _run() -> None:
        async with pool.acquire() as first:
            pass
        async with pool.acquire() as second:
            assert second is first
        assert clients[0].healthcheck_models == []
        assert clients[0].closed is False
        await pool.close()

    asyncio.run(_run())

    assert len(clients) == 1
    assert clients[0].closed is True


def test_pool_healthchecks_new_config_and_drains_old_lease() -> None:
    state = {"settings": _settings("token-a", model="model-a")}
    clients: list[_FakeManagedClient] = []

    def _factory(_value: ClientSettings) -> _FakeManagedClient:
        client = _FakeManagedClient()
        clients.append(client)
        return client

    pool = LLMClientPool(
        settings_getter=lambda: state["settings"],
        client_factory=_factory,
    )

    async def _run() -> None:
        async with pool.acquire() as old_client:
            state["settings"] = _settings("token-b", model="model-b")
            async with pool.acquire() as new_client:
                assert new_client is not old_client
                assert clients[1].healthcheck_models == ["model-b"]
                assert clients[0].closed is False
            assert clients[0].closed is False
        assert clients[0].closed is True
        assert clients[1].closed is False
        await pool.close()

    asyncio.run(_run())

    assert clients[1].closed is True


def test_pool_rejects_unhealthy_replacement_without_closing_old_client() -> None:
    state = {"settings": _settings("token-a")}
    clients: list[_FakeManagedClient] = []

    def _factory(_value: ClientSettings) -> _FakeManagedClient:
        client = _FakeManagedClient(healthy=len(clients) == 0)
        clients.append(client)
        return client

    pool = LLMClientPool(
        settings_getter=lambda: state["settings"],
        client_factory=_factory,
    )

    async def _run() -> None:
        async with pool.acquire() as old_client:
            assert old_client is clients[0]
        state["settings"] = _settings("token-b")
        with pytest.raises(RuntimeError, match="健康检查失败"):
            async with pool.acquire():
                pass
        assert clients[0].closed is False
        assert clients[1].closed is True

        state["settings"] = _settings("token-a")
        async with pool.acquire() as preserved_client:
            assert preserved_client is clients[0]
        await pool.close()

    asyncio.run(_run())


def test_pool_healthcheck_does_not_block_old_lease_release_and_is_single_flight(
) -> None:
    state = {"settings": _settings("token-a")}
    clients: list[_FakeManagedClient] = []

    async def _run() -> None:
        healthcheck_started = asyncio.Event()
        healthcheck_release = asyncio.Event()

        def _factory(_value: ClientSettings) -> _FakeManagedClient:
            if clients:
                client = _BlockingHealthClient(
                    healthcheck_started=healthcheck_started,
                    healthcheck_release=healthcheck_release,
                )
            else:
                client = _FakeManagedClient()
            clients.append(client)
            return client

        pool = LLMClientPool(
            settings_getter=lambda: state["settings"],
            client_factory=_factory,
        )
        old_lease = pool.acquire()
        old_client = await old_lease.__aenter__()
        state["settings"] = _settings("token-b", model="model-b")

        async def _use_new_client() -> Any:
            async with pool.acquire() as client:
                return client

        requests = [asyncio.create_task(_use_new_client()) for _ in range(5)]
        await asyncio.wait_for(healthcheck_started.wait(), timeout=1)

        await asyncio.wait_for(
            old_lease.__aexit__(None, None, None),
            timeout=0.2,
        )
        assert old_client is clients[0]
        assert clients[0].closed is False
        assert len(clients) == 2
        assert all(not request.done() for request in requests)

        healthcheck_release.set()
        new_clients = await asyncio.gather(*requests)
        assert all(client is clients[1] for client in new_clients)
        assert clients[1].healthcheck_models == ["model-b"]
        assert clients[0].closed is True
        await pool.close()

    asyncio.run(_run())

    assert clients[1].closed is True


def test_pool_close_cancels_inflight_healthcheck_and_closes_candidates() -> None:
    state = {"settings": _settings("token-a")}
    clients: list[_FakeManagedClient] = []

    async def _run() -> None:
        healthcheck_started = asyncio.Event()
        healthcheck_release = asyncio.Event()

        def _factory(_value: ClientSettings) -> _FakeManagedClient:
            if clients:
                client = _BlockingHealthClient(
                    healthcheck_started=healthcheck_started,
                    healthcheck_release=healthcheck_release,
                )
            else:
                client = _FakeManagedClient()
            clients.append(client)
            return client

        pool = LLMClientPool(
            settings_getter=lambda: state["settings"],
            client_factory=_factory,
        )
        async with pool.acquire():
            pass
        state["settings"] = _settings("token-b")

        async def _request() -> None:
            async with pool.acquire():
                pass

        request = asyncio.create_task(_request())
        await asyncio.wait_for(healthcheck_started.wait(), timeout=1)
        await asyncio.wait_for(pool.close(), timeout=1)

        with pytest.raises(RuntimeError, match="正在关闭"):
            await request

    asyncio.run(_run())

    assert len(clients) == 2
    assert all(client.closed for client in clients)


def test_pool_recovers_after_client_factory_failure() -> None:
    current_settings = _settings("token-a")
    should_fail = True
    clients: list[_FakeManagedClient] = []

    def _factory(_value: ClientSettings) -> _FakeManagedClient:
        nonlocal should_fail
        if should_fail:
            should_fail = False
            raise RuntimeError("构造失败")
        client = _FakeManagedClient()
        clients.append(client)
        return client

    pool = LLMClientPool(
        settings_getter=lambda: current_settings,
        client_factory=_factory,
    )

    async def _run() -> None:
        with pytest.raises(RuntimeError, match="构造失败"):
            async with pool.acquire():
                pass
        async with pool.acquire() as client:
            assert client is clients[0]
        await pool.close()

    asyncio.run(_run())

    assert clients[0].closed is True


def test_client_settings_repr_never_exposes_token() -> None:
    settings = _settings("super-secret-token")

    assert "super-secret-token" not in repr(settings)
