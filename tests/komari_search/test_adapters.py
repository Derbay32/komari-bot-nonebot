"""Komari Search 适配器与格式化器单元测试。"""

from __future__ import annotations

from importlib import import_module
from typing import Any

import pytest

from komari_bot.plugins.komari_search.config_schema import DynamicConfigSchema
from komari_bot.plugins.komari_search.formatter import (
    format_fetch_response,
    format_search_response,
)
from komari_bot.plugins.komari_search.types import (
    FetchResponse,
    FetchResultItem,
    SearchResponse,
    SearchResultItem,
)

tavily_adapter_module = import_module("komari_bot.plugins.komari_search.tavily_adapter")
exa_adapter_module = import_module("komari_bot.plugins.komari_search.exa_adapter")

TavilyAdapter = tavily_adapter_module.TavilyAdapter
ExaAdapter = exa_adapter_module.ExaAdapter


# ─── Tavily 适配器 fake ────────────────────────────────────────────


class _FakeTavilySDK:
    """模拟 TavilyClient，可控制 search/extract 返回值。"""

    def __init__(self, *, api_key: str) -> None:
        self.api_key = api_key

    def search(self, **kwargs: Any) -> dict[str, Any]:
        self._last_search_kwargs = kwargs  # type: ignore[attr-defined]
        return {
            "answer": "测试摘要",
            "results": [
                {
                    "title": "结果标题",
                    "url": "https://example.com/item",
                    "content": "结果正文",
                    "score": 0.85,
                }
            ],
        }

    def extract(self, **kwargs: Any) -> dict[str, Any]:
        self._last_extract_kwargs = kwargs  # type: ignore[attr-defined]
        return {
            "results": [
                {
                    "url": kwargs["urls"][0],
                    "title": "页面标题",
                    "raw_content": "原始正文内容",
                    "content": "备用正文",
                }
            ],
        }

    def close(self) -> None:
        pass


class _FakeTavilySDKNonDict:
    """search/extract 返回非 dict 类型，用于测试 TypeError。"""

    def __init__(self, *, api_key: str) -> None:
        self.api_key = api_key

    def search(self, **_kwargs: object) -> object:
        return ["not", "a", "dict"]

    def extract(self, **_kwargs: object) -> object:
        return 42

    def close(self) -> None:
        pass


class _FakeTavilySDKRawContentFallback:
    """raw_content 不存在时回退到 content。"""

    def __init__(self, *, api_key: str) -> None:
        self.api_key = api_key

    def extract(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "results": [
                {
                    "url": kwargs["urls"][0],
                    "title": "页",
                    "content": "只有备用正文",
                }
            ],
        }

    def close(self) -> None:
        pass


# ─── Tavily 适配器 search ──────────────────────────────────────────


def test_tavily_search_passes_correct_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 search 调用时透传 search_depth/max_results/include_answer/timeout。"""
    fake_cls = _FakeTavilySDK
    monkeypatch.setattr(tavily_adapter_module, "TavilyClient", fake_cls)
    config = DynamicConfigSchema(
        tavily_search_depth="advanced",
        max_results=3,
        tavily_include_answer=False,
        search_timeout_seconds=5.0,
    )
    adapter = TavilyAdapter()

    result = adapter.search(api_key="test-key", query="测试", config=config)

    assert isinstance(result, SearchResponse)
    assert len(result.items) == 1
    assert result.answer is None  # include_answer=False 时 answer 为 None


def test_tavily_search_normalizes_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 dict 响应正确归一化为 SearchResponse（answer / score 处理）。"""
    monkeypatch.setattr(tavily_adapter_module, "TavilyClient", _FakeTavilySDK)
    config = DynamicConfigSchema(tavily_include_answer=True)
    adapter = TavilyAdapter()

    result = adapter.search(api_key="k", query="q", config=config)

    assert isinstance(result, SearchResponse)
    assert result.answer == "测试摘要"
    assert len(result.items) == 1
    item = result.items[0]
    assert item.title == "结果标题"
    assert item.url == "https://example.com/item"
    assert item.content == "结果正文"
    assert item.score == 0.85


def test_tavily_search_raises_typeerror_on_non_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tavily search 返回非 dict 时抛出 TypeError。"""
    monkeypatch.setattr(tavily_adapter_module, "TavilyClient", _FakeTavilySDKNonDict)
    config = DynamicConfigSchema()
    adapter = TavilyAdapter()

    with pytest.raises(TypeError, match="非 dict"):
        adapter.search(api_key="k", query="q", config=config)


# ─── Tavily 适配器 fetch ───────────────────────────────────────────


def test_tavily_fetch_passes_correct_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 extract 调用时透传 urls 和 fetch_timeout_seconds。"""
    monkeypatch.setattr(tavily_adapter_module, "TavilyClient", _FakeTavilySDK)
    config = DynamicConfigSchema(fetch_timeout_seconds=10.0)
    adapter = TavilyAdapter()

    result = adapter.fetch(
        api_key="test-key",
        urls=["https://example.com/page"],
        config=config,
    )

    assert isinstance(result, FetchResponse)
    assert len(result.items) == 1
    assert result.items[0].content == "原始正文内容"


def test_tavily_fetch_prefers_raw_content_over_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 raw_content 优先于 content，不存在时回退到 content。"""
    monkeypatch.setattr(
        tavily_adapter_module, "TavilyClient", _FakeTavilySDKRawContentFallback,
    )
    config = DynamicConfigSchema()
    adapter = TavilyAdapter()

    result = adapter.fetch(
        api_key="k",
        urls=["https://example.com/page"],
        config=config,
    )

    assert result.items[0].content == "只有备用正文"


def test_tavily_fetch_raises_typeerror_on_non_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tavily extract 返回非 dict 时抛出 TypeError。"""
    monkeypatch.setattr(tavily_adapter_module, "TavilyClient", _FakeTavilySDKNonDict)
    config = DynamicConfigSchema()
    adapter = TavilyAdapter()

    with pytest.raises(TypeError, match="非 dict"):
        adapter.fetch(api_key="k", urls=["https://x.test"], config=config)


# ─── EXA 适配器 fake ──────────────────────────────────────────────


class _FakeExaSDK:
    """模拟 Exa SDK，可控制 search/get_contents 返回值。"""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def search(self, query: str, **kwargs: object) -> object:
        self._last_search_args = (query, kwargs)  # type: ignore[attr-defined]
        result = type(
            "ExaResult", (), {
                "title": "EXA标题",
                "url": "https://exa.test/1",
                "text": "EXA正文",
                "score": 0.92,
            },
        )()
        return type("ExaResponse", (), {"results": [result]})()

    def get_contents(self, urls: list[str], **kwargs: object) -> object:
        self._last_contents_args = (urls, kwargs)  # type: ignore[attr-defined]
        result = type(
            "ExaContentResult", (), {
                "url": urls[0],
                "title": "页面",
                "text": "正文内容",
                "highlights": ["亮点一", "亮点二"],
                "summary": "页面摘要内容",
            },
        )()
        return type("ExaResponse", (), {"results": [result]})()


# ─── EXA 适配器 search ─────────────────────────────────────────────


def test_exa_search_passes_correct_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 Exa.search() 调用时透传 num_results/type/contents。"""
    monkeypatch.setattr(exa_adapter_module, "Exa", _FakeExaSDK)
    config = DynamicConfigSchema(
        max_results=4,
        exa_search_type="neural",
    )
    adapter = ExaAdapter()

    result = adapter.search(api_key="test-key", query="测试查询", config=config)

    assert isinstance(result, SearchResponse)
    assert result.answer is None  # EXA 无 answer
    assert len(result.items) == 1


def test_exa_search_normalizes_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 EXA 的属性读取正确归一化为 SearchResponse。"""
    monkeypatch.setattr(exa_adapter_module, "Exa", _FakeExaSDK)
    config = DynamicConfigSchema()
    adapter = ExaAdapter()

    result = adapter.search(api_key="k", query="q", config=config)

    assert isinstance(result, SearchResponse)
    assert result.answer is None
    item = result.items[0]
    assert item.title == "EXA标题"
    assert item.url == "https://exa.test/1"
    assert item.content == "EXA正文"
    assert item.score == 0.92


# ─── EXA 适配器 fetch ──────────────────────────────────────────────


def test_exa_fetch_format_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """exa_fetch_format="text" 时只传 text=True。"""
    monkeypatch.setattr(exa_adapter_module, "Exa", _FakeExaSDK)
    config = DynamicConfigSchema(exa_fetch_format="text")
    adapter = ExaAdapter()

    result = adapter.fetch(
        api_key="k", urls=["https://exa.test/1"], config=config,
    )

    assert isinstance(result, FetchResponse)
    # fetch_format="text" 应读取 .text 属性
    assert "正文内容" in result.items[0].content


def test_exa_fetch_format_highlights(monkeypatch: pytest.MonkeyPatch) -> None:
    """exa_fetch_format="highlights" 时只传 highlights=True，列表 join 成文本。"""
    monkeypatch.setattr(exa_adapter_module, "Exa", _FakeExaSDK)
    config = DynamicConfigSchema(exa_fetch_format="highlights")
    adapter = ExaAdapter()

    result = adapter.fetch(
        api_key="k", urls=["https://exa.test/1"], config=config,
    )

    assert isinstance(result, FetchResponse)
    assert "亮点一" in result.items[0].content
    assert "亮点二" in result.items[0].content


def test_exa_fetch_format_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    """exa_fetch_format="summary" 时只传 summary=True，读取 .summary 属性。"""
    monkeypatch.setattr(exa_adapter_module, "Exa", _FakeExaSDK)
    config = DynamicConfigSchema(exa_fetch_format="summary")
    adapter = ExaAdapter()

    result = adapter.fetch(
        api_key="k", urls=["https://exa.test/1"], config=config,
    )

    assert isinstance(result, FetchResponse)
    assert result.items[0].content == "页面摘要内容"


# ─── Formatter: format_search_response ─────────────────────────────


def test_format_search_response_with_answer() -> None:
    """有 answer 时，摘要前置输出。"""
    response = SearchResponse(
        items=[
            SearchResultItem(title="T", url="https://x.test", content="C"),
        ],
        answer="这是 AI 生成的答案摘要",
    )

    result = format_search_response(response, result_content_limit=300)

    assert "搜索摘要" in result
    assert "这是 AI 生成的答案摘要" in result
    assert "搜索结果" in result


def test_format_search_response_item_truncation() -> None:
    """条目正文超过 result_content_limit 时截断。"""
    response = SearchResponse(
        items=[
            SearchResultItem(title="T", url="https://x.test", content="A" * 200),
        ],
        answer=None,
    )

    result = format_search_response(response, result_content_limit=50)

    assert result.endswith("…")
    assert "摘要：" in result


def test_format_search_response_empty() -> None:
    """无条目、无 answer 时返回空结果提示。"""
    response = SearchResponse(items=[], answer=None)

    result = format_search_response(response, result_content_limit=300)

    assert result == "[搜索完成，但未找到相关结果]"


def test_format_search_response_no_answer_but_has_items() -> None:
    """无 answer 有条目时不输出摘要行。"""
    response = SearchResponse(
        items=[
            SearchResultItem(title="T", url="https://x.test", content="C"),
        ],
        answer=None,
    )

    result = format_search_response(response, result_content_limit=300)

    assert "搜索摘要" not in result
    assert "搜索结果" in result


# ─── Formatter: format_fetch_response ──────────────────────────────


def test_format_fetch_response_item_truncation() -> None:
    """单个页面正文超 content_limit 时截断。"""
    response = FetchResponse(
        items=[
            FetchResultItem(url="https://a.test", title="T", content="A" * 500),
        ],
    )

    result = format_fetch_response(response, content_limit=50, total_limit=10000)

    assert result.endswith("…")
    assert "正文：" in result


def test_format_fetch_response_total_truncation() -> None:
    """多页面总长度超 total_limit 时整体截断。"""
    response = FetchResponse(
        items=[
            FetchResultItem(url="https://a.test", title="TA", content="XA"),
            FetchResultItem(url="https://b.test", title="TB", content="XB"),
        ],
    )

    result = format_fetch_response(response, content_limit=10000, total_limit=30)

    assert result.endswith("…")
    assert len(result) < 34  # rstrip + "…" 后不超过 total_limit + 1


def test_format_fetch_response_empty() -> None:
    """无条目时返回空抓取提示。"""
    response = FetchResponse(items=[])

    result = format_fetch_response(response, content_limit=300, total_limit=10000)

    assert result == "[抓取完成，但未获取到页面内容]"


def test_format_fetch_response_untitled_item() -> None:
    """title 为空时显示 "无标题"。"""
    response = FetchResponse(
        items=[
            FetchResultItem(url="https://a.test", title="", content="C"),
        ],
    )

    result = format_fetch_response(response, content_limit=300, total_limit=10000)

    assert "无标题" in result
