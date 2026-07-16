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


def test_client_settings_repr_never_exposes_token() -> None:
    settings = _settings("super-secret-token")

    assert "super-secret-token" not in repr(settings)
