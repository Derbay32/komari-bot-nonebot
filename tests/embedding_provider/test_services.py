"""EmbeddingProvider 远程协议、超时、重试与日志边界测试。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

import aiohttp
import pytest

from komari_bot.plugins.embedding_provider.config_schema import DynamicConfigSchema
from komari_bot.plugins.embedding_provider.embedding_service import (
    EmbeddingResponseValidationError,
    EmbeddingService,
)
from komari_bot.plugins.embedding_provider.request_safety import (
    RemoteResponseDecodeError,
    RemoteResponseTooLargeError,
    RemoteServiceRequestError,
    build_request_timeout,
    read_bounded_json_response,
    request_with_retry,
)
from komari_bot.plugins.embedding_provider.rerank_service import (
    RerankResponseValidationError,
    RerankService,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class _Session:
    closed = False

    async def close(self) -> None:
        self.closed = True


class _ChunkedContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, _size: int) -> Any:
        for chunk in self._chunks:
            yield chunk


class _Response:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        content_length: int | None = None,
    ) -> None:
        self.content = _ChunkedContent(chunks)
        self.content_length = content_length


class _LogCapture:
    def __init__(self) -> None:
        self.records: list[tuple[object, ...]] = []

    def _record(self, *args: object, **kwargs: object) -> None:
        self.records.append((*args, kwargs))

    debug = _record
    error = _record
    info = _record
    warning = _record

    def joined(self) -> str:
        return repr(self.records)


def _config(**overrides: Any) -> DynamicConfigSchema:
    values: dict[str, Any] = {
        "embedding_api_url": "https://embedding.invalid/v1/embeddings",
        "embedding_dimension": 3,
        "rerank_enabled": True,
        "rerank_model": "rerank-test",
        "rerank_api_url": "https://rerank.invalid/v1/rerank",
        "request_retry_attempts": 2,
        "request_retry_backoff_seconds": 0,
    }
    values.update(overrides)
    return DynamicConfigSchema(**values)


def _install_post_json(
    monkeypatch: pytest.MonkeyPatch,
    service: object,
    operation: Callable[..., Awaitable[object]],
) -> None:
    monkeypatch.setattr(service, "_post_json", operation)
    monkeypatch.setattr(service, "_http_session", cast("Any", _Session()))


def test_request_timeout_limits_connect_read_and_total() -> None:
    timeout = build_request_timeout(
        _config(
            request_connect_timeout_seconds=4.0,
            request_read_timeout_seconds=12.0,
            request_total_timeout_seconds=20.0,
        )
    )

    assert timeout.connect == 4.0
    assert timeout.sock_connect == 4.0
    assert timeout.sock_read == 12.0
    assert timeout.total == 20.0


@pytest.mark.asyncio
async def test_plugin_startup_uses_async_config_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import komari_bot.plugins.embedding_provider as plugin_module

    class _Manager:
        def __init__(self) -> None:
            self.async_calls = 0

        def get(self) -> object:
            raise AssertionError

        async def get_async(self) -> DynamicConfigSchema:
            self.async_calls += 1
            return _config()

    services: list[object] = []

    class _Service:

        def __init__(self, config: DynamicConfigSchema) -> None:
            self.config = config
            self.cleaned = False
            services.append(self)

        async def cleanup(self) -> None:
            self.cleaned = True

    manager = _Manager()
    monkeypatch.setattr(plugin_module, "config_manager", manager)
    monkeypatch.setattr(plugin_module, "EmbeddingService", _Service)
    monkeypatch.setattr(plugin_module, "RerankService", _Service)
    plugin_module.state.embedding_service = None
    plugin_module.state.rerank_service = None

    try:
        await plugin_module._startup()

        assert manager.async_calls == 1
        assert len(services) == 2
    finally:
        await plugin_module._shutdown()

    assert all(cast("Any", service).cleaned for service in services)


@pytest.mark.asyncio
async def test_bounded_json_reader_accepts_streamed_response() -> None:
    response = cast(
        "aiohttp.ClientResponse",
        _Response([b'{"data":', b"[1,2,3]}"], content_length=16),
    )

    payload = await read_bounded_json_response(response, max_bytes=32)

    assert payload == {"data": [1, 2, 3]}


@pytest.mark.asyncio
async def test_bounded_json_reader_rejects_declared_and_streamed_oversize() -> None:
    declared = cast(
        "aiohttp.ClientResponse",
        _Response([], content_length=33),
    )
    streamed = cast(
        "aiohttp.ClientResponse",
        _Response([b"12345678", b"9"], content_length=None),
    )

    with pytest.raises(RemoteResponseTooLargeError, match="字节上限"):
        await read_bounded_json_response(declared, max_bytes=32)
    with pytest.raises(RemoteResponseTooLargeError, match="字节上限"):
        await read_bounded_json_response(streamed, max_bytes=8)


@pytest.mark.asyncio
async def test_bounded_json_reader_rejects_invalid_json_without_echoing_body() -> None:
    canary = b"UPSTREAM-RESPONSE-CANARY"
    response = cast(
        "aiohttp.ClientResponse",
        _Response([canary], content_length=len(canary)),
    )

    with pytest.raises(RemoteResponseDecodeError) as exc_info:
        await read_bounded_json_response(response, max_bytes=128)

    assert canary.decode() not in str(exc_info.value)


@pytest.mark.asyncio
async def test_retry_chain_obeys_business_total_deadline() -> None:
    async def _slow_operation() -> object:
        await asyncio.sleep(1)
        return object()

    with pytest.raises(RemoteServiceRequestError, match="请求超时"):
        await request_with_retry(
            _slow_operation,
            service_name="embedding_api",
            request_hash="deadline-test",
            config=_config(request_total_timeout_seconds=0.1),
        )


def test_embedding_response_accepts_ordered_finite_vectors() -> None:
    vectors = EmbeddingService._validate_embedding_response(
        {
            "data": [
                {"index": 0, "embedding": [1, 2.5, 3]},
                {"index": 1, "embedding": [4.0, 5, 6]},
            ]
        },
        expected_count=2,
        expected_dimension=3,
    )

    assert vectors == [[1.0, 2.5, 3.0], [4.0, 5.0, 6.0]]


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"data": []}, "数量不匹配"),
        (
            {
                "data": [
                    {"index": 1, "embedding": [1, 2, 3]},
                    {"index": 0, "embedding": [4, 5, 6]},
                ]
            },
            "按输入顺序",
        ),
        (
            {"data": [{"index": 0, "embedding": [1, 2]}]},
            "维度不匹配",
        ),
        (
            {"data": [{"index": 0, "embedding": [1, float("nan"), 3]}]},
            "有限数",
        ),
        (
            {"data": [{"index": 0, "embedding": [1, float("inf"), 3]}]},
            "有限数",
        ),
    ],
)
def test_embedding_response_rejects_protocol_mismatches(
    payload: object,
    reason: str,
) -> None:
    expected_count = 2 if reason == "按输入顺序" else 1
    with pytest.raises(EmbeddingResponseValidationError, match=reason):
        EmbeddingService._validate_embedding_response(
            payload,
            expected_count=expected_count,
            expected_dimension=3,
        )


@pytest.mark.asyncio
async def test_embedding_retries_transient_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = EmbeddingService(_config())
    calls = 0

    async def _post_json(*_args: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise aiohttp.ServerTimeoutError
        return {"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]}

    _install_post_json(monkeypatch, service, _post_json)

    result = await service.embed("测试输入")

    assert calls == 2
    assert result == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_embedding_does_not_retry_oversized_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = EmbeddingService(_config(request_retry_attempts=3))
    calls = 0

    async def _post_json(*_args: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise RemoteResponseTooLargeError("远程响应超过字节上限")

    _install_post_json(monkeypatch, service, _post_json)

    with pytest.raises(RemoteServiceRequestError):
        await service.embed("测试输入")

    assert calls == 1


@pytest.mark.asyncio
async def test_embedding_instruction_fallback_is_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = EmbeddingService(_config())
    payloads: list[dict[str, object]] = []

    async def _post_json(*_args: object, **kwargs: object) -> object:
        payload = cast("dict[str, object]", kwargs["payload"])
        payloads.append(payload)
        if "instruction" in payload:
            raise aiohttp.ClientResponseError(
                request_info=cast("Any", None),
                history=(),
                status=422,
            )
        return {"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]}

    _install_post_json(monkeypatch, service, _post_json)

    result = await service.embed("测试输入", instruction="查询指令")

    assert result == [0.1, 0.2, 0.3]
    assert len(payloads) == 2
    assert "instruction" in payloads[0]
    assert "instruction" not in payloads[1]


def test_rerank_response_validates_indices_and_finite_scores() -> None:
    results = RerankService._validate_rerank_response(
        {
            "results": [
                {"index": 1, "relevance_score": 0.5},
                {"index": 0, "relevance_score": 0.9},
            ]
        },
        document_count=2,
        top_n=2,
    )

    assert [(item.index, item.relevance_score) for item in results] == [
        (0, 0.9),
        (1, 0.5),
    ]


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (
            {
                "results": [
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.8},
                ]
            },
            "重复",
        ),
        ({"results": [{"index": 2, "relevance_score": 0.9}]}, "越界"),
        (
            {"results": [{"index": 0, "relevance_score": float("nan")}]},
            "有限数",
        ),
    ],
)
def test_rerank_response_rejects_protocol_mismatches(
    payload: object,
    reason: str,
) -> None:
    with pytest.raises(RerankResponseValidationError, match=reason):
        RerankService._validate_rerank_response(
            payload,
            document_count=2,
            top_n=2,
        )


@pytest.mark.asyncio
async def test_failure_logs_do_not_contain_embedding_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import komari_bot.plugins.embedding_provider.embedding_service as service_module
    import komari_bot.plugins.embedding_provider.request_safety as safety_module

    canary = "EMBEDDING-CANARY-DO-NOT-LOG"
    service = EmbeddingService(_config(request_retry_attempts=1))
    logs = _LogCapture()

    async def _post_json(*_args: object, **_kwargs: object) -> object:
        msg = f"上游异常包含 {canary}"
        raise RuntimeError(msg)

    _install_post_json(monkeypatch, service, _post_json)
    monkeypatch.setattr(service_module, "logger", logs)
    monkeypatch.setattr(safety_module, "logger", logs)

    with pytest.raises(RemoteServiceRequestError) as exc_info:
        await service.embed(canary)

    assert canary not in str(exc_info.value)
    assert canary not in logs.joined()


@pytest.mark.asyncio
async def test_rerank_logs_only_query_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import komari_bot.plugins.embedding_provider.rerank_service as service_module

    canary = "RERANK-QUERY-CANARY-DO-NOT-LOG"
    service = RerankService(_config())
    logs = _LogCapture()

    async def _post_json(*_args: object, **_kwargs: object) -> object:
        return {"results": [{"index": 0, "relevance_score": 0.9}]}

    _install_post_json(monkeypatch, service, _post_json)
    monkeypatch.setattr(service_module, "logger", logs)

    result = await service.rerank(canary, ["候选文档"], top_n=1)

    assert result[0].index == 0
    assert canary not in logs.joined()
