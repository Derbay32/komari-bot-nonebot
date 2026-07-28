"""Komari Search 联网搜索测试。"""

from __future__ import annotations

import asyncio
from importlib import import_module
from types import SimpleNamespace
from typing import ClassVar

import pytest

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
        }
        defaults.update(overrides)
        self.config = SimpleNamespace(**defaults)

    def get(self) -> SimpleNamespace:
        return self.config


class _FakeTavilyClient:
    calls: ClassVar[list[dict[str, object]]] = []
    raise_error: ClassVar[bool] = False

    def __init__(self, *, api_key: str) -> None:
        self.api_key = api_key

    def search(self, **kwargs: object) -> dict[str, object]:
        if self.__class__.raise_error:
            msg = "Tavily 故障"
            raise RuntimeError(msg)
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


def _patch_search_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    **config_overrides: object,
) -> None:
    _FakeTavilyClient.calls = []
    _FakeTavilyClient.raise_error = False
    search_module._search_cache.clear()
    monkeypatch.setattr(
        search_module,
        "config_manager",
        _FakeConfigManager(**config_overrides),
    )
    monkeypatch.setattr(search_module, "TavilyClient", _FakeTavilyClient)


@pytest.mark.asyncio
async def test_search_web_truncates_long_query(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_search_dependencies(monkeypatch)

    await search_module.search_web("查" * 300)

    assert len(str(_FakeTavilyClient.calls[0]["query"])) == 200


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

    async def fake_to_thread(func: object, **kwargs: object) -> object:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return func(**kwargs)  # type: ignore[misc]

    monkeypatch.setattr(search_module.asyncio, "to_thread", fake_to_thread)
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

    assert "搜索服务异常" in first
    assert "搜索结果" in second
    assert len(_FakeTavilyClient.calls) == 1
