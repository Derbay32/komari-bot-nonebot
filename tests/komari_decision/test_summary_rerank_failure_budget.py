"""群总结 rerank fallback 与固定窗口失败预算验收测试（KOMARIBOT-24）。"""

from __future__ import annotations

from types import SimpleNamespace

import aiohttp
import pytest
from komari_bot.plugins.komari_decision.services.summary_rerank_failure_budget import (
    RerankFailureBudgetUnavailableError,
    SummaryRerankFailureBudget,
)

import komari_bot.plugins.komari_decision as decision_plugin
from komari_bot.decision import (
    SummaryRequestClassificationResult,
    SummaryRequestUnavailableReason,
)
from komari_bot.plugins import embedding_provider
from komari_bot.plugins.embedding_provider import (
    RemoteResponseDecodeError,
    RemoteServiceFailureKind,
    RemoteServiceRequestError,
    RerankConfigurationError,
    RerankResponseValidationError,
)
from komari_bot.plugins.embedding_provider.request_safety import request_with_retry
from tests.komari_decision.test_summary_request_classification import (
    _config,
    _EmbeddingProvider,
    _FailureBudget,
    _Runtime,
    _snapshot,
    _wire,
)


class _FakeRedis:
    def __init__(self) -> None:
        self.now_seconds = 0
        self.values: dict[str, tuple[int, int]] = {}
        self.expire_calls: dict[str, int] = {}

    async def execute_command(self, *args: object) -> object:
        command = str(args[0]).upper()
        if command == "EVAL":
            script = str(args[1])
            assert script.startswith("-- summary_rerank_failure_budget_v1")
            assert "redis.call('INCR', KEYS[1])" in script
            assert "if count == 1 then" in script
            assert "redis.call('EXPIRE', KEYS[1], ARGV[1])" in script
            assert args[2] == 1
            key = str(args[3])
            window_seconds = int(str(args[4]))
            current = self.values.get(key)
            if current is not None and current[1] <= self.now_seconds:
                self.values.pop(key, None)
                current = None
            count = 1 if current is None else current[0] + 1
            if current is None:
                expires_at = self.now_seconds + window_seconds
                self.expire_calls[key] = self.expire_calls.get(key, 0) + 1
            else:
                expires_at = current[1]
            self.values[key] = (count, expires_at)
            return count
        if command == "DEL":
            key = str(args[1])
            return int(self.values.pop(key, None) is not None)
        msg = f"未实现的 Redis 命令：{command}"
        raise AssertionError(msg)


class _BrokenFailureBudget:
    def __init__(self) -> None:
        self.record_calls = 0
        self.clear_calls = 0

    async def record_failure(
        self,
        provider_fingerprint: str,
        window_seconds: int,
    ) -> int:
        del provider_fingerprint, window_seconds
        self.record_calls += 1
        raise RerankFailureBudgetUnavailableError

    async def clear(self, provider_fingerprint: str) -> None:
        del provider_fingerprint
        self.clear_calls += 1
        raise RerankFailureBudgetUnavailableError


class _RequestConfig:
    request_connect_timeout_seconds = 1.0
    request_read_timeout_seconds = 1.0
    request_total_timeout_seconds = 1.0
    request_retry_attempts = 1
    request_retry_backoff_seconds = 0.0
    response_max_bytes = 1024


def _eligible_network_error() -> RemoteServiceRequestError:
    return RemoteServiceRequestError(
        "rerank_api 请求失败",
        status=None,
        failure_kind=RemoteServiceFailureKind.NETWORK,
    )


@pytest.mark.asyncio
async def test_failure_budget_uses_fixed_ttl_and_safe_fingerprint_keys() -> None:
    redis = _FakeRedis()
    budget = SummaryRerankFailureBudget(redis)
    first_fingerprint = "a" * 16
    second_fingerprint = "b" * 16

    assert await budget.record_failure(first_fingerprint, 60) == 1
    first_key = next(iter(redis.values))
    first_expiry = redis.values[first_key][1]
    assert first_key.endswith(first_fingerprint)
    assert "https://" not in first_key
    assert "api-key" not in first_key

    redis.now_seconds = 20
    assert await budget.record_failure(first_fingerprint, 60) == 2
    assert redis.values[first_key][1] == first_expiry
    assert redis.expire_calls[first_key] == 1

    assert await budget.record_failure(second_fingerprint, 60) == 1
    assert len(redis.values) == 2

    redis.now_seconds = 61
    assert await budget.record_failure(first_fingerprint, 60) == 1
    assert redis.values[first_key][1] == 121
    assert redis.expire_calls[first_key] == 2

    await budget.clear(first_fingerprint)
    assert first_key not in redis.values


def test_rerank_provider_fingerprint_isolated_by_endpoint_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = SimpleNamespace(
        rerank_api_url="https://rerank-a.example/v1",
        rerank_model="model-a",
        rerank_api_key="api-key-must-not-affect-fingerprint",
    )
    monkeypatch.setattr(
        embedding_provider.state,
        "rerank_service",
        SimpleNamespace(config=config),
    )

    first = embedding_provider.get_rerank_provider_fingerprint()
    config.rerank_api_key = "rotated-api-key"
    assert embedding_provider.get_rerank_provider_fingerprint() == first

    config.rerank_api_url = "https://rerank-b.example/v1"
    second = embedding_provider.get_rerank_provider_fingerprint()
    config.rerank_api_url = "https://rerank-a.example/v1"
    config.rerank_model = "model-b"
    third = embedding_provider.get_rerank_provider_fingerprint()

    assert len({first, second, third}) == 3
    assert all(len(value) == 16 for value in (first, second, third))
    assert "api-key" not in first
    assert "rerank-a" not in first


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_error, expected_kind, expected_status",
    [
        (
            aiohttp.ClientConnectionError("offline"),
            RemoteServiceFailureKind.NETWORK,
            None,
        ),
        (TimeoutError("timeout"), RemoteServiceFailureKind.TIMEOUT, None),
        (
            RemoteResponseDecodeError("响应不是合法 JSON"),
            RemoteServiceFailureKind.RESPONSE_INVALID,
            None,
        ),
        (
            aiohttp.ClientResponseError(
                request_info=SimpleNamespace(real_url="https://provider.invalid"),
                history=(),
                status=401,
                message="unauthorized",
            ),
            RemoteServiceFailureKind.HTTP_STATUS,
            401,
        ),
    ],
)
async def test_remote_request_error_preserves_safe_failure_metadata(
    source_error: Exception,
    expected_kind: RemoteServiceFailureKind,
    expected_status: int | None,
) -> None:
    async def _operation() -> object:
        raise source_error

    with pytest.raises(RemoteServiceRequestError) as raised:
        await request_with_retry(
            _operation,
            service_name="rerank_api",
            request_hash="safe-hash",
            config=_RequestConfig(),
        )

    assert raised.value.failure_kind is expected_kind
    assert raised.value.status == expected_status
    assert "provider.invalid" not in str(raised.value)


@pytest.mark.asyncio
async def test_third_failure_escalates_and_success_clears_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = _FailureBudget()
    provider = _EmbeddingProvider(
        rerank_error=_eligible_network_error(),
        rerank_provider_fingerprint="a" * 16,
    )
    config = _config(
        summary_rerank_fallback_enabled=True,
        summary_rerank_failure_threshold=3,
        summary_rerank_failure_window_seconds=60,
    )
    _wire(
        monkeypatch,
        config=config,
        runtime=_Runtime(_snapshot()),
        provider=provider,
        failure_budget=budget,
    )

    for _ in range(2):
        result = await decision_plugin.classify_summary_request(
            "请总结今天的群聊"
        )
        assert result == SummaryRequestClassificationResult.matched()

    exhausted = await decision_plugin.classify_summary_request(
        "请总结今天的群聊"
    )
    assert exhausted == SummaryRequestClassificationResult.unavailable(
        SummaryRequestUnavailableReason.RERANK_FAILURE_BUDGET_EXHAUSTED
    )

    still_exhausted = await decision_plugin.classify_summary_request(
        "请总结今天的群聊"
    )
    assert still_exhausted.reason is (
        SummaryRequestUnavailableReason.RERANK_FAILURE_BUDGET_EXHAUSTED
    )
    assert len(provider.rerank_calls) == 4

    provider.rerank_error = None
    provider.rerank_scores = [0.9, 0.1]
    recovered = await decision_plugin.classify_summary_request(
        "请总结今天的群聊"
    )
    assert recovered == SummaryRequestClassificationResult.matched()
    assert budget.clear_calls == ["a" * 16]

    provider.rerank_error = _eligible_network_error()
    after_recovery = await decision_plugin.classify_summary_request(
        "请总结今天的群聊"
    )
    assert after_recovery == SummaryRequestClassificationResult.matched()
    assert budget.counts["a" * 16] == 1


@pytest.mark.asyncio
async def test_failure_budget_isolated_by_provider_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = _FailureBudget()
    provider = _EmbeddingProvider(
        rerank_error=_eligible_network_error(),
        rerank_provider_fingerprint="a" * 16,
    )
    _wire(
        monkeypatch,
        config=_config(
            summary_rerank_fallback_enabled=True,
            summary_rerank_failure_threshold=3,
        ),
        runtime=_Runtime(_snapshot()),
        provider=provider,
        failure_budget=budget,
    )

    for _ in range(2):
        assert (
            await decision_plugin.classify_summary_request("总结一下群聊内容")
        ).status.value == "matched"

    provider.rerank_provider_fingerprint = "b" * 16
    isolated = await decision_plugin.classify_summary_request("总结一下群聊内容")
    assert isolated.status.value == "matched"

    provider.rerank_provider_fingerprint = "a" * 16
    exhausted = await decision_plugin.classify_summary_request("总结一下群聊内容")
    assert exhausted.reason is (
        SummaryRequestUnavailableReason.RERANK_FAILURE_BUDGET_EXHAUSTED
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        RemoteServiceRequestError(
            "401",
            status=401,
            failure_kind=RemoteServiceFailureKind.HTTP_STATUS,
        ),
        RemoteServiceRequestError(
            "403",
            status=403,
            failure_kind=RemoteServiceFailureKind.HTTP_STATUS,
        ),
        RemoteServiceRequestError(
            "400",
            status=400,
            failure_kind=RemoteServiceFailureKind.HTTP_STATUS,
        ),
        RemoteServiceRequestError(
            "425",
            status=425,
            failure_kind=RemoteServiceFailureKind.HTTP_STATUS,
        ),
        RerankConfigurationError("缺少 rerank URL"),
    ],
)
async def test_non_degradable_failures_do_not_consume_budget(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> None:
    budget = _FailureBudget()
    _wire(
        monkeypatch,
        config=_config(summary_rerank_fallback_enabled=True),
        runtime=_Runtime(_snapshot()),
        provider=_EmbeddingProvider(rerank_error=error),
        failure_budget=budget,
    )

    result = await decision_plugin.classify_summary_request("请总结群聊内容")

    assert result == SummaryRequestClassificationResult.unavailable(
        SummaryRequestUnavailableReason.RERANK_UNAVAILABLE
    )
    assert budget.record_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        _eligible_network_error(),
        RemoteServiceRequestError(
            "timeout",
            status=None,
            failure_kind=RemoteServiceFailureKind.TIMEOUT,
        ),
        RemoteServiceRequestError(
            "429",
            status=429,
            failure_kind=RemoteServiceFailureKind.HTTP_STATUS,
        ),
        RemoteServiceRequestError(
            "503",
            status=503,
            failure_kind=RemoteServiceFailureKind.HTTP_STATUS,
        ),
        RerankResponseValidationError("响应格式错误"),
        RemoteResponseDecodeError("响应不是合法 JSON"),
    ],
)
async def test_only_degradable_failures_enter_budget_and_fallback(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> None:
    budget = _FailureBudget()
    _wire(
        monkeypatch,
        config=_config(summary_rerank_fallback_enabled=True),
        runtime=_Runtime(_snapshot()),
        provider=_EmbeddingProvider(rerank_error=error),
        failure_budget=budget,
    )

    result = await decision_plugin.classify_summary_request("请总结群聊内容")

    assert result == SummaryRequestClassificationResult.matched()
    assert len(budget.record_calls) == 1


@pytest.mark.asyncio
async def test_fallback_disabled_still_counts_without_escalating_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = _FailureBudget()
    _wire(
        monkeypatch,
        config=_config(summary_rerank_fallback_enabled=False),
        runtime=_Runtime(_snapshot()),
        provider=_EmbeddingProvider(rerank_error=_eligible_network_error()),
        failure_budget=budget,
    )

    result = await decision_plugin.classify_summary_request("请总结群聊内容")

    assert result == SummaryRequestClassificationResult.unavailable(
        SummaryRequestUnavailableReason.RERANK_UNAVAILABLE
    )
    assert len(budget.record_calls) == 1


@pytest.mark.asyncio
async def test_budget_unavailable_forbids_fallback_but_not_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = _BrokenFailureBudget()
    provider = _EmbeddingProvider(rerank_error=_eligible_network_error())
    _wire(
        monkeypatch,
        config=_config(summary_rerank_fallback_enabled=True),
        runtime=_Runtime(_snapshot()),
        provider=provider,
        failure_budget=budget,
    )

    failed = await decision_plugin.classify_summary_request("请总结群聊内容")
    assert failed == SummaryRequestClassificationResult.unavailable(
        SummaryRequestUnavailableReason.FAILURE_BUDGET_UNAVAILABLE
    )

    provider.rerank_error = None
    provider.rerank_scores = [0.9, 0.1]
    succeeded = await decision_plugin.classify_summary_request("请总结群聊内容")
    assert succeeded == SummaryRequestClassificationResult.matched()
    assert budget.clear_calls == 1


@pytest.mark.asyncio
async def test_numeric_and_explicit_cosine_paths_ignore_failure_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = _BrokenFailureBudget()
    provider = _EmbeddingProvider(
        query_vector=[1.0, 0.0],
        rerank_enabled=False,
    )
    _wire(
        monkeypatch,
        config=_config(summary_similarity_threshold=0.7),
        runtime=_Runtime(_snapshot()),
        provider=provider,
        failure_budget=budget,
    )

    numeric = await decision_plugin.classify_summary_request("总结过去 50 条")
    cosine = await decision_plugin.classify_summary_request("请总结群聊内容")

    assert numeric == SummaryRequestClassificationResult.matched()
    assert cosine == SummaryRequestClassificationResult.matched()
    assert budget.record_calls == 0
    assert budget.clear_calls == 0


def test_failure_budget_reason_codes_remain_safe_and_narrow() -> None:
    assert (
        SummaryRequestUnavailableReason.RERANK_FAILURE_BUDGET_EXHAUSTED.value
        == "rerank_failure_budget_exhausted"
    )
    assert (
        SummaryRequestUnavailableReason.FAILURE_BUDGET_UNAVAILABLE.value
        == "failure_budget_unavailable"
    )
