"""Komari Search 联网搜索测试（v2.0：Tavily / EXA 双提供者）。"""

from __future__ import annotations

import asyncio
import threading
import time
from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar

import pytest
from pydantic import ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable

from komari_bot.plugins.komari_search.config_schema import DynamicConfigSchema
from komari_bot.plugins.komari_search.types import SearchResponse, SearchResultItem

search_module = import_module("komari_bot.plugins.komari_search")
tavily_adapter_module = import_module(
    "komari_bot.plugins.komari_search.tavily_adapter",
)
exa_adapter_module = import_module(
    "komari_bot.plugins.komari_search.exa_adapter",
)


class _FakeConfigManager:
    """v2.0 配置的假管理器，支持运行时覆盖任意字段。"""

    def __init__(self, **overrides: object) -> None:
        defaults: dict[str, object] = {
            "plugin_enable": True,
            "user_whitelist": [],
            "group_whitelist": [],
            "search_provider": "tavily",
            "search_api_key": "token",
            "search_enabled": True,
            "max_results": 5,
            "result_content_limit": 300,
            "search_timeout_seconds": 12.0,
            "tavily_search_depth": "basic",
            "tavily_include_answer": True,
            "fetch_enabled": True,
            "fetch_max_urls": 3,
            "fetch_content_limit": 3000,
            "fetch_timeout_seconds": 15.0,
            "exa_search_type": "auto",
            "exa_fetch_format": "text",
            "circuit_breaker_failure_threshold": 3,
            "circuit_breaker_recovery_seconds": 30.0,
        }
        defaults.update(overrides)
        self.config = SimpleNamespace(**defaults)

    def get(self) -> SimpleNamespace:
        """返回当前配置快照。"""
        return self.config


class _FakeTavilyClient:
    """Tavily SDK 假客户端，记录调用参数并返回可重现的搜索/抓取结果。"""

    calls: ClassVar[list[dict[str, object]]] = []
    attempts: ClassVar[int] = 0
    raise_error: ClassVar[bool] = False
    error_message: ClassVar[str] = "Tavily 故障"

    def __init__(self, *, api_key: str) -> None:
        self.api_key = api_key

    def search(self, **kwargs: object) -> dict[str, object]:
        """模拟 TavilyClient.search()。"""
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

    def extract(self, **kwargs: Any) -> dict[str, object]:
        """模拟 TavilyClient.extract()。"""
        self.__class__.attempts += 1
        if self.__class__.raise_error:
            raise RuntimeError(self.__class__.error_message)
        self.__class__.calls.append(kwargs)
        urls = kwargs.get("urls", [])
        return {
            "results": [
                {
                    "url": url,
                    "title": "页面标题",
                    "raw_content": f"原始正文内容（{url}）",
                    "content": f"备用正文（{url}）",
                }
                for url in urls
            ]
        }

    def close(self) -> None:
        """TavilyClient.close() 空操作。"""


class _FakeExa:
    """EXA SDK 假客户端，记录调用参数并返回 SimpleNamespace 结果。"""

    calls: ClassVar[list[dict[str, object]]] = []
    attempts: ClassVar[int] = 0
    raise_error: ClassVar[bool] = False
    error_message: ClassVar[str] = "EXA 故障"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def search(self, query: str, **kwargs: object) -> SimpleNamespace:
        """模拟 Exa.search()。"""
        self.__class__.attempts += 1
        if self.__class__.raise_error:
            raise RuntimeError(self.__class__.error_message)
        self.__class__.calls.append({"query": query, **kwargs})
        return SimpleNamespace(
            results=[
                SimpleNamespace(
                    title="EXA标题",
                    url="https://exa.test",
                    text="EXA正文内容",
                    score=0.95,
                )
            ],
        )

    def get_contents(
        self, urls: list[str], **kwargs: object,
    ) -> SimpleNamespace:
        """模拟 Exa.get_contents()。"""
        self.__class__.attempts += 1
        if self.__class__.raise_error:
            raise RuntimeError(self.__class__.error_message)
        self.__class__.calls.append({"urls": urls, **kwargs})
        return SimpleNamespace(
            results=[
                SimpleNamespace(
                    url=url,
                    title="页面标题",
                    text=f"页面正文（{url}）",
                    highlights=[f"亮点{i}" for i in range(1, 4)],
                    summary="页面摘要",
                )
                for url in urls
            ],
        )


def _patch_search_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    **config_overrides: object,
) -> None:
    """统一注入搜索测试所需的假配置与假 SDK 客户端。"""
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


# ─── 配置 Schema 边界 ──────────────────────────────────────────────


def test_search_resilience_config_has_safe_bounds() -> None:
    """search_timeout_seconds / circuit_breaker 等字段应有合理默认与下限。"""
    config = DynamicConfigSchema()

    assert config.search_timeout_seconds == 12.0
    assert config.circuit_breaker_failure_threshold == 3
    assert config.circuit_breaker_recovery_seconds == 30.0
    with pytest.raises(ValidationError):
        DynamicConfigSchema(search_timeout_seconds=0.5)
    with pytest.raises(ValidationError):
        DynamicConfigSchema(circuit_breaker_failure_threshold=0)


# ─── 可用性检查 ────────────────────────────────────────────────────


def test_search_availability_enforces_switch_and_caller_whitelists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """plugin_enable=False 或白名单不匹配时 is_search_available 返回 False。"""
    _patch_search_dependencies(monkeypatch, plugin_enable=False)
    assert search_module.is_search_available(
        caller_user_id="1001",
        caller_group_id="2001",
    ) is False

    _patch_search_dependencies(
        monkeypatch,
        user_whitelist=["1001"],
        group_whitelist=["2001"],
    )
    assert search_module.is_search_available(
        caller_user_id="1001",
        caller_group_id="2001",
    ) is True
    assert search_module.is_search_available(
        caller_user_id="1002",
        caller_group_id="2001",
    ) is False
    assert search_module.is_search_available(
        caller_user_id="1001",
        caller_group_id="2002",
    ) is False
    assert search_module.is_search_available() is False
    assert search_module.is_search_available(caller_is_superuser=True) is True


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
        (["1001"], [], "1001", "2002", True),
        (["1001"], [], "1002", "2001", False),
        ([], ["2001"], "1002", "2001", True),
        ([], ["2001"], "1001", "2002", False),
        (["1001"], ["2001"], "1001", "2001", True),
        (["1001"], ["2001"], "1002", "2001", False),
        (["1001"], ["2001"], "1001", "2002", False),
    ],
)
def test_search_availability_whitelist_matrix(
    monkeypatch: pytest.MonkeyPatch,
    user_whitelist: list[str],
    group_whitelist: list[str],
    caller_user_id: str,
    caller_group_id: str,
    *,
    expected: bool,
) -> None:
    """白名单矩阵：混合 user/group 白名单的各种组合。"""
    _patch_search_dependencies(
        monkeypatch,
        user_whitelist=user_whitelist,
        group_whitelist=group_whitelist,
    )

    assert search_module.is_search_available(
        caller_user_id=caller_user_id,
        caller_group_id=caller_group_id,
    ) is expected


# ─── 权限拒绝 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_web_denies_missing_or_disallowed_caller_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺失或权限不匹配的调用上下文返回 PERMISSION_DENIED。"""
    _patch_search_dependencies(
        monkeypatch,
        user_whitelist=["1001"],
        group_whitelist=["2001"],
    )

    missing = await search_module.search_web("今日新闻")
    wrong_user = await search_module.search_web(
        "今日新闻",
        caller_user_id="1002",
        caller_group_id="2001",
    )
    wrong_group = await search_module.search_web(
        "今日新闻",
        caller_user_id="1001",
        caller_group_id="2002",
    )

    assert missing == "[搜索失败：PERMISSION_DENIED]"
    assert wrong_user == "[搜索失败：PERMISSION_DENIED]"
    assert wrong_group == "[搜索失败：PERMISSION_DENIED]"
    assert _FakeTavilyClient.attempts == 0


@pytest.mark.asyncio
async def test_search_web_allows_authenticated_context_and_superuser_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """白名单内用户与 SUPERUSER 均可正常搜索。"""
    _patch_search_dependencies(
        monkeypatch,
        user_whitelist=["1001"],
        group_whitelist=["2001"],
    )

    allowed = await search_module.search_web(
        "普通调用",
        caller_user_id="1001",
        caller_group_id="2001",
    )
    superuser = await search_module.search_web(
        "超级用户调用",
        caller_is_superuser=True,
    )

    assert "搜索结果" in allowed
    assert "搜索结果" in superuser
    assert _FakeTavilyClient.attempts == 2


# ─── 查询校验 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_web_rejects_long_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """超长查询（>200 字符）返回 INVALID_QUERY。"""
    _patch_search_dependencies(monkeypatch)

    result = await search_module.search_web("查" * 300)

    assert result == "[搜索失败：INVALID_QUERY]"
    assert _FakeTavilyClient.attempts == 0


# ─── 缓存 ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_web_uses_short_term_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """重复相同查询应命中短期缓存（首位空格不计入缓存键）。"""
    _patch_search_dependencies(monkeypatch)

    first = await search_module.search_web("今日新闻")
    second = await search_module.search_web(" 今日新闻 ")

    assert first == second
    assert len(_FakeTavilyClient.calls) == 1


@pytest.mark.asyncio
async def test_search_web_cache_key_includes_result_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不同 max_results / tavily_search_depth 产生不同缓存键。"""
    _patch_search_dependencies(monkeypatch, max_results=1)
    await search_module.search_web("今日新闻")

    monkeypatch.setattr(
        search_module, "config_manager",
        _FakeConfigManager(max_results=2),
    )
    await search_module.search_web("今日新闻")

    monkeypatch.setattr(
        search_module,
        "config_manager",
        _FakeConfigManager(tavily_search_depth="advanced", max_results=2),
    )
    await search_module.search_web("今日新闻")

    assert len(_FakeTavilyClient.calls) == 3


@pytest.mark.asyncio
async def test_search_web_cache_key_includes_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一 query 在 tavily 下缓存后，切到 exa 应产生新上游调用。"""
    _patch_search_dependencies(monkeypatch)
    await search_module.search_web("今日新闻")
    assert _FakeTavilyClient.attempts == 1

    monkeypatch.setattr(
        search_module,
        "config_manager",
        _FakeConfigManager(search_provider="exa"),
    )
    await search_module.search_web("今日新闻")
    assert _FakeExa.attempts == 1


# ─── 并发控制 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_web_limits_tavily_call_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并发搜索受 _SEARCH_SEMAPHORE 限制。"""
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
    monkeypatch.setattr(
        search_module, "_SEARCH_SEMAPHORE", asyncio.Semaphore(2),
    )

    await asyncio.gather(
        *(search_module.search_web(f"查询{index}") for index in range(4))
    )

    assert max_active <= 2


@pytest.mark.asyncio
async def test_search_web_does_not_cache_tavily_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上游异常不缓存，重试可成功。"""
    _patch_search_dependencies(monkeypatch)
    _FakeTavilyClient.raise_error = True

    first = await search_module.search_web("今日新闻")
    _FakeTavilyClient.raise_error = False
    second = await search_module.search_web("今日新闻")

    assert first == "[搜索失败：UPSTREAM_ERROR]"
    assert "搜索结果" in second
    assert len(_FakeTavilyClient.calls) == 1


# ─── Single-flight 去重 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_web_singleflight_merges_concurrent_identical_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并发同一查询共享一次上游调用。"""
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
    """被取消的等待者不影响共享搜索任务的执行。"""
    _patch_search_dependencies(monkeypatch)
    started = asyncio.Event()
    release = asyncio.Event()
    requests = 0

    async def _controlled_request(_request: Callable[[], object]) -> object:
        nonlocal requests
        requests += 1
        started.set()
        await release.wait()
        return SearchResponse(
            items=[
                SearchResultItem(
                    title="共享结果",
                    url="https://x.test",
                    content="共享结果",
                )
            ],
            answer=None,
        )

    monkeypatch.setattr(search_module, "_run_provider_request", _controlled_request)
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


# ─── 超时 ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_web_applies_business_deadline_and_stable_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """search_timeout_seconds=0.01 时，hang 的请求应在业务截止时间内返回 TIMEOUT。"""
    _patch_search_dependencies(monkeypatch, search_timeout_seconds=0.01)

    async def _hang_request(_request: Callable[[], object]) -> object:
        await asyncio.sleep(1)
        return {}

    monkeypatch.setattr(search_module, "_run_provider_request", _hang_request)

    started_at = time.monotonic()
    result = await search_module.search_web("超时查询")

    assert result == "[搜索失败：TIMEOUT]"
    assert time.monotonic() - started_at < 0.2


@pytest.mark.asyncio
async def test_search_web_passes_same_timeout_to_tavily_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """search_timeout_seconds 应透传至 Tavily SDK。"""
    _patch_search_dependencies(monkeypatch, search_timeout_seconds=7.5)

    await search_module.search_web("检查超时")

    assert _FakeTavilyClient.calls[0]["timeout"] == 7.5


# ─── 熔断 ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_web_circuit_breaker_blocks_until_half_open_probe_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连续失败达到阈值后熔断，手动回退 open_until 后半开探测成功则恢复。"""
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
    search_module._search_circuit_breaker.open_until = time.monotonic() - 1
    recovered = await search_module.search_web("恢复探测")

    assert "搜索结果" in recovered
    assert search_module._search_circuit_breaker.consecutive_failures == 0


# ─── 日志脱敏 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_failure_never_logs_or_returns_raw_query_and_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失败日志不泄露原始查询/异常正文，只含 query_sha256 指纹。"""
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


# ─── 提供者参数化 ──────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name", ["tavily", "exa"])
async def test_search_web_works_with_provider(
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
) -> None:
    """tavily 和 exa 两个提供者均可通过 search_web 正常搜索。"""
    _patch_search_dependencies(monkeypatch, search_provider=provider_name)

    result = await search_module.search_web("测试查询")

    assert "搜索结果" in result
    if provider_name == "tavily":
        assert _FakeTavilyClient.attempts == 1
    else:
        assert _FakeExa.attempts == 1
