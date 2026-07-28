"""Komari Search 网页抓取测试（fetch_page）。"""

from __future__ import annotations

import asyncio
import hashlib
import time
from importlib import import_module
from typing import TYPE_CHECKING

import pytest

from komari_bot.plugins.komari_search.types import FetchResponse, FetchResultItem

if TYPE_CHECKING:
    from collections.abc import Callable

search_module = import_module("komari_bot.plugins.komari_search")
tavily_adapter_module = import_module(
    "komari_bot.plugins.komari_search.tavily_adapter",
)
exa_adapter_module = import_module(
    "komari_bot.plugins.komari_search.exa_adapter",
)

# 复用 search 测试中的 fake 类与 patching 辅助函数
from tests.komari_search.test_search_web import (
    _FakeConfigManager,
    _FakeExa,
    _FakeTavilyClient,
)


def _patch_fetch_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    **config_overrides: object,
) -> None:
    """统一注入抓取测试所需的假配置与假 SDK 客户端。"""
    _FakeTavilyClient.calls = []
    _FakeTavilyClient.attempts = 0
    _FakeTavilyClient.raise_error = False
    _FakeTavilyClient.error_message = "Tavily 故障"
    _FakeExa.calls = []
    _FakeExa.attempts = 0
    _FakeExa.raise_error = False
    _FakeExa.error_message = "EXA 故障"
    search_module._reset_search_runtime_for_tests()
    search_module._executor_state.close()
    monkeypatch.setattr(
        search_module,
        "config_manager",
        _FakeConfigManager(**config_overrides),
    )
    monkeypatch.setattr(tavily_adapter_module, "TavilyClient", _FakeTavilyClient)
    monkeypatch.setattr(exa_adapter_module, "Exa", _FakeExa)


# ─── 可用性检查 ────────────────────────────────────────────────────


def test_fetch_availability_disabled_when_fetch_enabled_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fetch_enabled=False 时 is_fetch_available 返回 False。"""
    _patch_fetch_dependencies(monkeypatch, fetch_enabled=False)
    assert search_module.is_fetch_available() is False


def test_fetch_availability_disabled_when_plugin_enable_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """plugin_enable=False 时 is_fetch_available 返回 False。"""
    _patch_fetch_dependencies(monkeypatch, plugin_enable=False)
    assert search_module.is_fetch_available() is False


def test_fetch_availability_missing_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """search_api_key 为空时 is_fetch_available 返回 False。"""
    _patch_fetch_dependencies(monkeypatch, search_api_key="")
    assert search_module.is_fetch_available() is False


def test_fetch_availability_superuser_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SUPERUSER 始终可用。"""
    _patch_fetch_dependencies(
        monkeypatch,
        user_whitelist=["9999"],
        group_whitelist=["9999"],
    )
    assert search_module.is_fetch_available(caller_is_superuser=True) is True


@pytest.mark.parametrize(
    (
        "user_whitelist",
        "group_whitelist",
        "caller_user_id",
        "caller_group_id",
        "expected",
    ),
    [
        ([], [], "1001", "2001", True),
        (["1001"], [], "1002", "2001", False),
        ([], ["2001"], "1001", "2002", False),
    ],
)
def test_fetch_availability_whitelist_matrix(
    monkeypatch: pytest.MonkeyPatch,
    user_whitelist: list[str],
    group_whitelist: list[str],
    caller_user_id: str,
    caller_group_id: str,
    *,
    expected: bool,
) -> None:
    """抓取可用性白名单矩阵（简化版）。"""
    _patch_fetch_dependencies(
        monkeypatch,
        user_whitelist=user_whitelist,
        group_whitelist=group_whitelist,
    )
    assert search_module.is_fetch_available(
        caller_user_id=caller_user_id,
        caller_group_id=caller_group_id,
    ) is expected


# ─── URL 校验 ──────────────────────────────────────────────────────

_INVALID_URLS_ERROR = "[抓取失败：INVALID_URLS]"


@pytest.mark.asyncio
async def test_fetch_page_url_exceeds_max(monkeypatch: pytest.MonkeyPatch) -> None:
    """去重后 URL 数量超过 fetch_max_urls 返回 INVALID_URLS。"""
    _patch_fetch_dependencies(monkeypatch, fetch_max_urls=2)
    result = await search_module.fetch_page(
        ["https://a.test", "https://b.test", "https://c.test"],
    )
    assert result == _INVALID_URLS_ERROR


@pytest.mark.asyncio
async def test_fetch_page_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """空列表返回 INVALID_URLS。"""
    _patch_fetch_dependencies(monkeypatch)
    result = await search_module.fetch_page([])
    assert result == _INVALID_URLS_ERROR


@pytest.mark.asyncio
async def test_fetch_page_non_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 list 类型返回 INVALID_URLS。"""
    _patch_fetch_dependencies(monkeypatch)
    result = await search_module.fetch_page("not_a_list")  # type: ignore[arg-type]
    assert result == _INVALID_URLS_ERROR


@pytest.mark.asyncio
async def test_fetch_page_invalid_url_no_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无 scheme 的非法 URL 返回 INVALID_URLS。"""
    _patch_fetch_dependencies(monkeypatch)
    result = await search_module.fetch_page(["notaurl"])
    assert result == _INVALID_URLS_ERROR


@pytest.mark.asyncio
async def test_fetch_page_invalid_url_ftp_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 http(s) scheme 的 URL 返回 INVALID_URLS。"""
    _patch_fetch_dependencies(monkeypatch)
    result = await search_module.fetch_page(["ftp://x.com"])
    assert result == _INVALID_URLS_ERROR


@pytest.mark.asyncio
async def test_fetch_page_url_dedup_before_limit_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重复 URL 去重后数量不超过限制时正常执行。"""
    _patch_fetch_dependencies(monkeypatch, fetch_max_urls=2)

    async def _ok_fetch(_request: Callable[[], object]) -> object:
        return FetchResponse(
            items=[FetchResultItem(url="https://a.test", title="T", content="C")],
        )

    monkeypatch.setattr(search_module, "_run_provider_request", _ok_fetch)
    result = await search_module.fetch_page(
        ["https://a.test", "https://a.test", "https://b.test"],
    )
    assert "页面" in result


# ─── Single-flight ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_page_singleflight_merges_concurrent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并发相同 URLs 只执行一次上游调用。"""
    _patch_fetch_dependencies(monkeypatch)
    requests = 0

    async def _slow_fetch(_request: Callable[[], object]) -> object:
        nonlocal requests
        requests += 1
        await asyncio.sleep(0.03)
        return FetchResponse(
            items=[FetchResultItem(url="https://a.test", title="T", content="C")],
        )

    monkeypatch.setattr(search_module, "_run_provider_request", _slow_fetch)

    results = await asyncio.gather(
        *(
            search_module.fetch_page(["https://a.test", "https://b.test"])
            for _ in range(8)
        ),
    )

    assert len(set(results)) == 1
    assert requests == 1


@pytest.mark.asyncio
async def test_fetch_page_serial_calls_no_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fetch 不缓存结果：串行两次相同 URL 执行两次上游调用。"""
    _patch_fetch_dependencies(monkeypatch)
    requests = 0

    async def _count_fetch(_request: Callable[[], object]) -> object:
        nonlocal requests
        requests += 1
        return FetchResponse(
            items=[FetchResultItem(url="https://a.test", title="T", content="C")],
        )

    monkeypatch.setattr(search_module, "_run_provider_request", _count_fetch)

    await search_module.fetch_page(["https://a.test"])
    await search_module.fetch_page(["https://a.test"])

    assert requests == 2


# ─── 熔断器独立 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_page_circuit_breaker_search_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """搜索熔断器 tripped 不影响抓取。"""
    _patch_fetch_dependencies(monkeypatch)
    search_module._search_circuit_breaker.open_until = time.monotonic() + 999

    async def _ok_fetch(_request: Callable[[], object]) -> object:
        return FetchResponse(
            items=[FetchResultItem(url="https://a.test", title="T", content="C")],
        )

    monkeypatch.setattr(search_module, "_run_provider_request", _ok_fetch)

    fetch_result = await search_module.fetch_page(["https://a.test"])
    assert "页面" in fetch_result

    search_result = await search_module.search_web("无关查询")
    assert "CIRCUIT_OPEN" in search_result


@pytest.mark.asyncio
async def test_fetch_page_circuit_breaker_fetch_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """抓取熔断器 tripped 不影响搜索。"""
    _patch_fetch_dependencies(
        monkeypatch,
        circuit_breaker_failure_threshold=1,
    )

    async def _error_fetch(_request: Callable[[], object]) -> object:
        raise RuntimeError("强制故障")

    monkeypatch.setattr(search_module, "_run_provider_request", _error_fetch)
    await search_module.fetch_page(["https://a.test"])

    async def _ok_search(_request: Callable[[], object]) -> object:
        from komari_bot.plugins.komari_search.types import (
            SearchResponse,
            SearchResultItem,
        )

        return SearchResponse(
            items=[
                SearchResultItem(title="T", url="https://a.test", content="C"),
            ],
            answer=None,
        )

    monkeypatch.setattr(search_module, "_run_provider_request", _ok_search)

    search_result = await search_module.search_web("正常查询")
    assert "搜索结果" in search_result

    fetch_result = await search_module.fetch_page(["https://a.test"])
    assert "CIRCUIT_OPEN" in fetch_result


# ─── 超时 ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_page_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_timeout_seconds 超时返回 TIMEOUT。"""
    _patch_fetch_dependencies(monkeypatch, fetch_timeout_seconds=0.01)

    async def _hang_request(_request: Callable[[], object]) -> object:
        await asyncio.sleep(1)
        return {}

    monkeypatch.setattr(search_module, "_run_provider_request", _hang_request)

    started_at = time.monotonic()
    result = await search_module.fetch_page(["https://a.test"])

    assert result == "[抓取失败：TIMEOUT]"
    assert time.monotonic() - started_at < 0.2


# ─── 上游错误 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_page_upstream_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """上游异常返回 UPSTREAM_ERROR。"""
    _patch_fetch_dependencies(monkeypatch)

    async def _error_fetch(_request: Callable[[], object]) -> object:
        raise RuntimeError("上游故障")

    monkeypatch.setattr(search_module, "_run_provider_request", _error_fetch)

    result = await search_module.fetch_page(["https://a.test"])
    assert result == "[抓取失败：UPSTREAM_ERROR]"


# ─── 权限拒绝 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_page_permission_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    """白名单不匹配返回 PERMISSION_DENIED。"""
    _patch_fetch_dependencies(
        monkeypatch,
        user_whitelist=["9999"],
        group_whitelist=["9999"],
    )

    result = await search_module.fetch_page(
        ["https://a.test"],
        caller_user_id="1001",
        caller_group_id="2001",
    )
    assert result == "[抓取失败：PERMISSION_DENIED]"


# ─── 内容截断 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_page_content_truncation_per_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单页正文超过 fetch_content_limit 时截断并附带 "…"。 """
    _patch_fetch_dependencies(monkeypatch, fetch_content_limit=50)

    async def _fat_fetch(_request: Callable[[], object]) -> object:
        return FetchResponse(
            items=[
                FetchResultItem(url="https://a.test", title="T", content="A" * 200),
            ],
        )

    monkeypatch.setattr(search_module, "_run_provider_request", _fat_fetch)

    result = await search_module.fetch_page(["https://a.test"])
    assert result.endswith("…")
    assert len("A" * 200) + 100 > len(result)  # 格式化后的总长度远小于原始 200


@pytest.mark.asyncio
async def test_fetch_page_content_truncation_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """多页抓取总量超过 _FETCH_TOTAL_CONTENT_LIMIT 时整体截断。"""
    _patch_fetch_dependencies(monkeypatch, fetch_content_limit=5000)
    monkeypatch.setattr(search_module, "_FETCH_TOTAL_CONTENT_LIMIT", 100)

    async def _multi_fetch(_request: Callable[[], object]) -> object:
        return FetchResponse(
            items=[
                FetchResultItem(url="https://a.test", title="TA", content="A" * 200),
                FetchResultItem(url="https://b.test", title="TB", content="B" * 200),
            ],
        )

    monkeypatch.setattr(search_module, "_run_provider_request", _multi_fetch)

    result = await search_module.fetch_page(["https://a.test", "https://b.test"])
    assert result.endswith("…")
    # 结果应小于或接近 total_limit，加上格式化前缀
    assert len(result) < 150


# ─── 日志脱敏 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_page_failure_log_sanitization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失败日志不泄露 URL 内容，只含 url_count 和 urls_sha256。"""
    _patch_fetch_dependencies(monkeypatch)
    urls_canary = ["https://secret-page.example/private"]

    async def _error_fetch(_request: Callable[[], object]) -> object:
        raise RuntimeError("故障")

    monkeypatch.setattr(search_module, "_run_provider_request", _error_fetch)
    logged_parts: list[str] = []

    def _capture_warning(message: object, *args: object, **_kwargs: object) -> None:
        logged_parts.append(f"{message!s} {args!r}")

    monkeypatch.setattr(search_module.logger, "warning", _capture_warning)

    result = await search_module.fetch_page(urls_canary)
    serialized_logs = " ".join(logged_parts)

    assert "UPSTREAM" in result
    assert urls_canary[0] not in result
    assert urls_canary[0] not in serialized_logs
    assert "url_count" in serialized_logs
    assert "urls_sha256" in serialized_logs
    assert (
        hashlib.sha256("\n".join(urls_canary).encode()).hexdigest()
        in serialized_logs
    )
