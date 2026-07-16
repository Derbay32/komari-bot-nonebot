"""Komari Search 联网搜索测试。"""

from __future__ import annotations

import asyncio
import threading
import time
from importlib import import_module
from types import SimpleNamespace
from typing import ClassVar

import pytest
from pydantic import ValidationError

from komari_bot.plugins.komari_search.config_schema import DynamicConfigSchema

search_module = import_module("komari_bot.plugins.komari_search")


class _FakeConfigManager:
    def __init__(self, **overrides: object) -> None:
        defaults: dict[str, object] = {
            "search_enabled": True,
            "tavily_api_key": "token",
            "search_depth": "basic",
            "max_results": 5,
            "include_answer": True,
            "result_content_limit": 300,
            "search_timeout_seconds": 12.0,
            "circuit_breaker_failure_threshold": 3,
            "circuit_breaker_recovery_seconds": 30.0,
        }
        defaults.update(overrides)
        self.config = SimpleNamespace(**defaults)

    def get(self) -> SimpleNamespace:
        return self.config


class _FakeTavilyClient:
    calls: ClassVar[list[dict[str, object]]] = []
    attempts: ClassVar[int] = 0
    raise_error: ClassVar[bool] = False
    error_message: ClassVar[str] = "Tavily 故障"

    def __init__(self, *, api_key: str) -> None:
        self.api_key = api_key

    def search(self, **kwargs: object) -> dict[str, object]:
        self.__class__.attempts += 1
        if self.__class__.raise_error:
            raise RuntimeError(self.__class__.error_message)
        self.__class__.calls.append(kwargs)
        return {
            "answer": f"摘要：{kwargs['query']}",
            "results": [
                {
                    "title": "标题",
                    "url": "https://example.test",
                    "content": "正文",
                }
            ],
        }

    def close(self) -> None:
        pass


def _patch_search_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    **config_overrides: object,
) -> None:
    _FakeTavilyClient.calls = []
    _FakeTavilyClient.attempts = 0
    _FakeTavilyClient.raise_error = False
    _FakeTavilyClient.error_message = "Tavily 故障"
    search_module._reset_search_runtime_for_tests()
    search_module._executor_state.close()
    monkeypatch.setattr(
        search_module,
        "config_manager",
        _FakeConfigManager(**config_overrides),
    )
    monkeypatch.setattr(search_module, "TavilyClient", _FakeTavilyClient)


def test_search_resilience_config_has_safe_bounds() -> None:
    config = DynamicConfigSchema()

    assert config.search_timeout_seconds == 12.0
    assert config.circuit_breaker_failure_threshold == 3
    assert config.circuit_breaker_recovery_seconds == 30.0
    with pytest.raises(ValidationError):
        DynamicConfigSchema(search_timeout_seconds=0.5)
    with pytest.raises(ValidationError):
        DynamicConfigSchema(circuit_breaker_failure_threshold=0)


@pytest.mark.asyncio
async def test_search_web_rejects_long_query(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_search_dependencies(monkeypatch)

    result = await search_module.search_web("查" * 300)

    assert result == "[搜索失败：INVALID_QUERY]"
    assert _FakeTavilyClient.attempts == 0


@pytest.mark.asyncio
async def test_search_web_uses_short_term_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_search_dependencies(monkeypatch)

    first = await search_module.search_web("今日新闻")
    second = await search_module.search_web(" 今日新闻 ")

    assert first == second
    assert len(_FakeTavilyClient.calls) == 1


@pytest.mark.asyncio
async def test_search_web_cache_key_includes_result_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_search_dependencies(monkeypatch, max_results=1)
    await search_module.search_web("今日新闻")

    monkeypatch.setattr(search_module, "config_manager", _FakeConfigManager(max_results=2))
    await search_module.search_web("今日新闻")

    monkeypatch.setattr(
        search_module,
        "config_manager",
        _FakeConfigManager(search_depth="advanced", max_results=2),
    )
    await search_module.search_web("今日新闻")

    assert len(_FakeTavilyClient.calls) == 3


@pytest.mark.asyncio
async def test_search_web_limits_tavily_call_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_search_dependencies(monkeypatch)
    active = 0
    max_active = 0
    lock = threading.Lock()
    original_search = _FakeTavilyClient.search

    def _slow_search(
        client: _FakeTavilyClient,
        **kwargs: object,
    ) -> dict[str, object]:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.02)
            return original_search(client, **kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(_FakeTavilyClient, "search", _slow_search)
    monkeypatch.setattr(search_module, "_TAVILY_SEARCH_SEMAPHORE", asyncio.Semaphore(2))

    await asyncio.gather(
        *(search_module.search_web(f"查询{index}") for index in range(4))
    )

    assert max_active <= 2


@pytest.mark.asyncio
async def test_search_web_does_not_cache_tavily_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_search_dependencies(monkeypatch)
    _FakeTavilyClient.raise_error = True

    first = await search_module.search_web("今日新闻")
    _FakeTavilyClient.raise_error = False
    second = await search_module.search_web("今日新闻")

    assert first == "[搜索失败：UPSTREAM_ERROR]"
    assert "搜索结果" in second
    assert len(_FakeTavilyClient.calls) == 1


@pytest.mark.asyncio
async def test_search_web_singleflight_merges_concurrent_identical_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_search_dependencies(monkeypatch)
    original_search = _FakeTavilyClient.search

    def _slow_search(
        client: _FakeTavilyClient,
        **kwargs: object,
    ) -> dict[str, object]:
        time.sleep(0.03)
        return original_search(client, **kwargs)

    monkeypatch.setattr(_FakeTavilyClient, "search", _slow_search)

    results = await asyncio.gather(
        *(search_module.search_web("同一个查询") for _ in range(8))
    )

    assert len(set(results)) == 1
    assert _FakeTavilyClient.attempts == 1


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_shared_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_search_dependencies(monkeypatch)
    started = asyncio.Event()
    release = asyncio.Event()
    requests = 0

    async def _controlled_request(**_kwargs: object) -> object:
        nonlocal requests
        requests += 1
        started.set()
        await release.wait()
        return {"answer": "共享结果", "results": []}

    monkeypatch.setattr(search_module, "_run_tavily_request", _controlled_request)
    cancelled_waiter = asyncio.create_task(search_module.search_web("共享查询"))
    surviving_waiter = asyncio.create_task(search_module.search_web("共享查询"))
    await started.wait()

    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter
    release.set()
    result = await surviving_waiter

    assert "共享结果" in result
    assert requests == 1


@pytest.mark.asyncio
async def test_search_web_applies_business_deadline_and_stable_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_search_dependencies(monkeypatch, search_timeout_seconds=0.01)
    received_timeout: list[float] = []

    async def _hang_request(**kwargs: object) -> object:
        timeout_seconds = kwargs["timeout_seconds"]
        assert isinstance(timeout_seconds, (int, float))
        received_timeout.append(float(timeout_seconds))
        await asyncio.sleep(1)
        return {}

    monkeypatch.setattr(search_module, "_run_tavily_request", _hang_request)

    started_at = time.monotonic()
    result = await search_module.search_web("超时查询")

    assert result == "[搜索失败：TIMEOUT]"
    assert time.monotonic() - started_at < 0.2
    assert received_timeout == [0.01]


@pytest.mark.asyncio
async def test_search_web_passes_same_timeout_to_tavily_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_search_dependencies(monkeypatch, search_timeout_seconds=7.5)

    await search_module.search_web("检查超时")

    assert _FakeTavilyClient.calls[0]["timeout"] == 7.5


@pytest.mark.asyncio
async def test_search_web_circuit_breaker_blocks_until_half_open_probe_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_search_dependencies(
        monkeypatch,
        circuit_breaker_failure_threshold=2,
        circuit_breaker_recovery_seconds=30.0,
    )
    _FakeTavilyClient.raise_error = True

    first = await search_module.search_web("故障一")
    second = await search_module.search_web("故障二")
    blocked = await search_module.search_web("不会发往上游")

    assert first == second == "[搜索失败：UPSTREAM_ERROR]"
    assert blocked == "[搜索失败：CIRCUIT_OPEN]"
    assert _FakeTavilyClient.attempts == 2

    _FakeTavilyClient.raise_error = False
    search_module._circuit_breaker.open_until = time.monotonic() - 1
    recovered = await search_module.search_web("恢复探测")

    assert "搜索结果" in recovered
    assert search_module._circuit_breaker.consecutive_failures == 0


@pytest.mark.asyncio
async def test_search_failure_never_logs_or_returns_raw_query_and_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_search_dependencies(monkeypatch)
    query_canary = "CANARY_PRIVATE_QUERY_7f2d"
    error_canary = "CANARY_UPSTREAM_BODY_8a4c"
    _FakeTavilyClient.raise_error = True
    _FakeTavilyClient.error_message = error_canary
    logged_parts: list[str] = []

    def _capture_warning(message: object, *args: object, **_kwargs: object) -> None:
        logged_parts.append(f"{message!s} {args!r}")

    monkeypatch.setattr(search_module.logger, "warning", _capture_warning)

    result = await search_module.search_web(
        query_canary,
        request_trace_id="trace-search-1\nforged",
    )
    serialized_logs = " ".join(logged_parts)

    assert result == "[搜索失败：UPSTREAM_ERROR]"
    assert query_canary not in result
    assert error_canary not in result
    assert query_canary not in serialized_logs
    assert error_canary not in serialized_logs
    assert "trace-search-1forged" in serialized_logs
    assert "query_sha256" in serialized_logs
