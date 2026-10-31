"""TSK-194A shared Hindsight retain caller contract.

The tests drive the real service/session seam.  Only SDK I/O and the config
getter are replaced with boundary doubles.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from hindsight_client import RecallResponse
from hindsight_client_api.exceptions import NotFoundException
from hindsight_client_api.models.document_response import DocumentResponse
from hindsight_client_api.models.retain_response import RetainResponse

from komari_bot.plugins.komari_memory.services import hindsight_service
from komari_bot.plugins.komari_memory.services.hindsight_retain import (
    HindsightRetainRequest,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

BANK_ID = "shared-bank-1"
SOURCE_ID = "conversation:msg-1"
CONTENT = "今天去了海边，风很大。"
SECRET = "hindsight-secret-value"
TIMEOUT = 0.5
TARGET = "a" * 64


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _request(
    *,
    target_fingerprint: str | None = None,
    source_id: str = SOURCE_ID,
    content: str = CONTENT,
    timestamp: datetime | None = None,
    metadata: Mapping[str, str] | None = None,
) -> HindsightRetainRequest:
    resolved_target = TARGET if target_fingerprint is None else target_fingerprint
    return HindsightRetainRequest.create(
        target_fingerprint=resolved_target,
        source_id=source_id,
        content=content,
        timestamp=timestamp,
        metadata={} if metadata is None else metadata,
    )


def test_serialized_shape_identity_and_source_preserving_change_detection() -> None:
    timestamp = datetime(
        2026,
        1,
        2,
        11,
        4,
        5,
        123456,
        tzinfo=timezone(timedelta(hours=8)),
    )
    request = _request(
        timestamp=timestamp,
        metadata={"topic": "旅行", "speaker": "小鞠"},
    )

    serialized = request.to_json()
    assert set(serialized) == {
        "version",
        "target_fingerprint",
        "source_id",
        "content",
        "timestamp",
        "metadata",
    }
    assert serialized["version"] == 1
    assert serialized["target_fingerprint"] == TARGET
    assert serialized["source_id"] == SOURCE_ID
    assert serialized["content"] == CONTENT
    assert serialized["timestamp"] == "2026-01-02T03:04:05.123456Z"
    assert serialized["metadata"] == {"speaker": "小鞠", "topic": "旅行"}

    expected_document_id = "komari-online-v1-" + _digest(
        {"target_fingerprint": TARGET, "source_id": SOURCE_ID}
    )
    expected_payload_sha256 = _digest(serialized)
    expected_operation_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            "komari:hindsight:retain:v1:"
            f"{TARGET}:{expected_document_id}:{expected_payload_sha256}",
        )
    )
    assert request.document_id == expected_document_id
    assert request.payload_sha256 == expected_payload_sha256
    assert request.operation_id == expected_operation_id
    assert str(uuid.UUID(request.operation_id)) == request.operation_id

    changed = _request(
        timestamp=timestamp,
        metadata={"topic": "旅行", "speaker": "小鞠"},
        content=CONTENT + "后来下雨了。",
    )
    assert changed.document_id == request.document_id
    assert changed.payload_sha256 != request.payload_sha256
    assert changed.operation_id != request.operation_id

    metadata_changed = _request(
        timestamp=timestamp,
        metadata={"topic": "旅行", "speaker": "小鞠", "extra": "x"},
    )
    assert metadata_changed.document_id == request.document_id
    assert metadata_changed.operation_id != request.operation_id

    other_source = _request(
        timestamp=timestamp,
        source_id="conversation:msg-2",
        metadata={"topic": "旅行", "speaker": "小鞠"},
    )
    assert other_source.document_id != request.document_id


def test_round_trip_json_keeps_operation_identity_after_reconstruction() -> None:
    request = _request(
        timestamp=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        metadata={"speaker": "小鞠", "topic": "旅行"},
    )

    serialized = request.to_json()
    restored = HindsightRetainRequest.from_json(
        json.loads(json.dumps(serialized, ensure_ascii=False))
    )

    assert restored == request
    assert restored.to_json() == serialized
    assert restored.document_id == request.document_id
    assert restored.payload_sha256 == request.payload_sha256
    assert restored.operation_id == request.operation_id


def test_request_keeps_immutable_metadata_and_hides_sensitive_values() -> None:
    caller_metadata = {"topic": "旅行"}
    request = _request(metadata=caller_metadata)
    caller_metadata["topic"] = "changed"
    caller_metadata["new"] = "value"

    assert request.metadata == {"topic": "旅行"}
    rendered = repr(request)
    assert CONTENT not in rendered
    assert "旅行" not in rendered
    assert SOURCE_ID not in rendered
    assert TARGET not in rendered

    serialized = request.to_json()
    serialized_metadata = serialized["metadata"]
    assert isinstance(serialized_metadata, dict)
    serialized_metadata["topic"] = "mutated"
    assert request.metadata == {"topic": "旅行"}
    assert request.to_json()["metadata"] == {"topic": "旅行"}


def test_target_fingerprint_hex_case_is_normalized_to_canonical_lowercase() -> None:
    upper = _request(target_fingerprint=TARGET.upper())
    lower = _request()

    assert upper.target_fingerprint == TARGET
    assert upper.to_json()["target_fingerprint"] == TARGET
    assert upper.document_id == lower.document_id
    assert upper.operation_id == lower.operation_id


def test_from_json_rejects_missing_extra_or_noncanonical_fields() -> None:
    request = _request(timestamp=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))
    base = request.to_json()

    cases: list[dict[str, object]] = []
    missing = dict(base)
    missing.pop("content")
    cases.append(missing)
    extra = dict(base)
    extra["unexpected"] = "value"
    cases.append(extra)
    wrong_version = dict(base)
    wrong_version["version"] = 2
    cases.append(wrong_version)
    bool_version = dict(base)
    bool_version["version"] = True
    cases.append(bool_version)
    naive_timestamp = dict(base)
    naive_timestamp["timestamp"] = "2026-01-02T03:04:05"
    cases.append(naive_timestamp)
    offset_timestamp = dict(base)
    offset_timestamp["timestamp"] = "2026-01-02T03:04:05+00:00"
    cases.append(offset_timestamp)
    bad_timestamp = dict(base)
    bad_timestamp["timestamp"] = "not-a-timestamp"
    cases.append(bad_timestamp)
    bad_metadata = dict(base)
    bad_metadata["metadata"] = {"topic": 1}
    cases.append(bad_metadata)
    list_metadata = dict(base)
    list_metadata["metadata"] = [["topic", "旅行"]]
    cases.append(list_metadata)

    for value in cases:
        with pytest.raises(ValueError) as captured:
            HindsightRetainRequest.from_json(value)
        message = str(captured.value)
        assert message
        assert CONTENT not in message
        assert "旅行" not in message

    for value in (None, [], "text", 1):
        with pytest.raises(ValueError):
            HindsightRetainRequest.from_json(value)


@pytest.mark.parametrize(
    ("overrides", "expected_message"),
    [
        (
            {"target_fingerprint": "abc"},
            "target fingerprint must be 64 hex characters",
        ),
        (
            {"target_fingerprint": "g" * 64},
            "target fingerprint must be 64 hex characters",
        ),
        (
            {"source_id": ""},
            "source id must be a non-blank string",
        ),
        (
            {"source_id": "   "},
            "source id must be a non-blank string",
        ),
        (
            {"content": ""},
            "content must be a non-blank string",
        ),
        (
            {"content": " \t\n "},
            "content must be a non-blank string",
        ),
    ],
)
def test_create_rejects_blank_or_malformed_identity_fields(
    overrides: dict[str, object],
    expected_message: str,
) -> None:
    arguments: dict[str, object] = {
        "target_fingerprint": TARGET,
        "source_id": SOURCE_ID,
        "content": CONTENT,
        "timestamp": None,
        "metadata": {},
    }
    arguments.update(overrides)
    with pytest.raises(ValueError) as captured:
        HindsightRetainRequest.create(**arguments)  # type: ignore[arg-type]
    assert str(captured.value) == expected_message
    assert CONTENT not in str(captured.value)


def test_create_rejects_naive_non_finite_or_non_datetime_timestamps() -> None:
    for timestamp in (
        datetime(2026, 1, 2, 3, 4, 5),  # noqa: DTZ001
        float("nan"),
        "2026-01-02T03:04:05Z",
        object(),
    ):
        with pytest.raises(ValueError) as captured:
            HindsightRetainRequest.create(
                target_fingerprint=TARGET,
                source_id=SOURCE_ID,
                content=CONTENT,
                timestamp=timestamp,  # type: ignore[arg-type]
                metadata={},
            )
        assert (
            str(captured.value) == "timestamp must be a finite timezone-aware datetime"
        )


def test_create_rejects_nul_and_invalid_utf8_without_echoing_values() -> None:
    sensitive = "secret-sentinel"
    for content in (
        f"{sensitive}\x00tail",
        f"{sensitive}\ud800tail",
    ):
        with pytest.raises(ValueError) as captured:
            _request(content=content)
        message = str(captured.value)
        assert message == "text must be valid UTF-8 without NUL characters"
        assert sensitive not in message

    for source_id in (f"{sensitive}\x00", f"{sensitive}\udfff"):
        with pytest.raises(ValueError) as captured:
            _request(source_id=source_id)
        message = str(captured.value)
        assert message == "text must be valid UTF-8 without NUL characters"
        assert sensitive not in message

    for metadata in (
        {"topic": f"{sensitive}\x00"},
        {f"{sensitive}\x00": "value"},
        {"topic": f"{sensitive}\ud800"},
    ):
        with pytest.raises(ValueError) as captured:
            _request(metadata=metadata)
        message = str(captured.value)
        assert message == "text must be valid UTF-8 without NUL characters"
        assert sensitive not in message


def test_create_rejects_reserved_or_non_string_metadata() -> None:
    for metadata in (
        {"komari_online_schema": "1"},
        {"komari_online_payload_sha256": "x"},
        {"komari_online_": "x"},
        {"topic": 1},
        {1: "topic"},
        [("topic", "旅行")],
    ):
        with pytest.raises(ValueError) as captured:
            HindsightRetainRequest.create(
                target_fingerprint=TARGET,
                source_id=SOURCE_ID,
                content=CONTENT,
                timestamp=None,
                metadata=metadata,  # type: ignore[arg-type]
            )
        message = str(captured.value)
        assert message == (
            "metadata must be a string-to-string object without reserved keys"
        )
        assert CONTENT not in message
        assert "旅行" not in message


def test_protocol_version_is_serialized_but_not_caller_controlled() -> None:
    constructor = cast("Any", HindsightRetainRequest)
    with pytest.raises(TypeError):
        constructor(
            version=1,
            target_fingerprint=TARGET,
            source_id=SOURCE_ID,
            content=CONTENT,
            timestamp=None,
            metadata={},
        )


_ABSENT = object()
BUSINESS_METADATA = {"speaker": "小鞠", "topic": "旅行"}


@dataclass
class _ConfigState:
    values: Any

    async def __call__(self) -> Any:
        if isinstance(self.values, BaseException):
            raise self.values
        return self.values


def _config(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "hindsight_base_url": "http://127.0.0.1:9999",
        "hindsight_api_key": SECRET,
        "hindsight_bank_id": BANK_ID,
        "hindsight_request_timeout_seconds": TIMEOUT,
        "hindsight_recovery_interval_seconds": 0.01,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


TARGET = hindsight_service.HindsightSettings.from_config(_config()).target_fingerprint


def _reserved_metadata(request: HindsightRetainRequest) -> dict[str, str]:
    return {
        "komari_online_schema": "1",
        "komari_online_source_id": request.source_id,
        "komari_online_payload_sha256": request.payload_sha256,
        "komari_online_operation_id": request.operation_id,
        "komari_online_target_fingerprint": request.target_fingerprint,
    }


def _matching_document(
    request: HindsightRetainRequest,
    *,
    text: str | None = None,
    bank_id: str = BANK_ID,
    document_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    include_text: bool = True,
) -> DocumentResponse:
    document_metadata: dict[str, Any] = {
        **BUSINESS_METADATA,
        **_reserved_metadata(request),
    }
    if metadata is not None:
        document_metadata = dict(metadata)
    values: dict[str, Any] = {
        "id": request.document_id if document_id is None else document_id,
        "bank_id": bank_id,
        "original_text": request.content if text is None else text,
        "content_hash": "server-content-hash",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "memory_unit_count": 1,
        "document_metadata": document_metadata,
    }
    if not include_text:
        values.pop("original_text")
    return DocumentResponse.model_validate(values)


def _receipt(**overrides: Any) -> RetainResponse:
    values: dict[str, Any] = {
        "success": True,
        "bank_id": BANK_ID,
        "items_count": 1,
        "async": True,
        "operation_id": "00000000-0000-0000-0000-000000000000",
    }
    values.update(overrides)
    return RetainResponse.model_validate(values)


@dataclass
class _FakeBackend:
    version_handler: Any = None
    document_handler: Any = None
    bank_handler: Any = None
    retain_handler: Any = None
    recall_handler: Any = None
    document: Any = _ABSENT
    events: list[str] = field(default_factory=list)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    forbidden_calls: list[str] = field(default_factory=list)
    clients: list["_FakeClient"] = field(default_factory=list)

    def build_client(self, **kwargs: Any) -> "_FakeClient":
        client = _FakeClient(backend=self, client_kwargs=dict(kwargs))
        self.clients.append(client)
        return client

    async def default_version(self, _client: "_FakeClient") -> Any:
        return SimpleNamespace(api_version="1.1.0")

    async def default_document(
        self,
        _client: "_FakeClient",
        _bank_id: str,
        _document_id: str,
        _timeout: float | None,
    ) -> Any:
        if self.document is _ABSENT:
            raise NotFoundException(status=404, reason="Not Found")
        return self.document

    async def default_bank(
        self,
        _client: "_FakeClient",
        bank_id: str,
        _timeout: float | None,
    ) -> Any:
        return SimpleNamespace(bank_id=bank_id)

    async def default_retain(
        self,
        _client: "_FakeClient",
        kwargs: dict[str, Any],
    ) -> Any:
        return _receipt(operation_id=kwargs["operation_id"])

    async def default_recall(
        self,
        _client: "_FakeClient",
        _call: dict[str, Any],
    ) -> Any:
        return RecallResponse(results=[])

    async def call_version(self, client: "_FakeClient") -> Any:
        handler = self.version_handler or self.default_version
        return await handler(client)

    async def call_document(
        self,
        client: "_FakeClient",
        bank_id: str,
        document_id: str,
        request_timeout: float | None,
    ) -> Any:
        handler = self.document_handler or self.default_document
        return await handler(client, bank_id, document_id, request_timeout)

    async def call_bank(
        self,
        client: "_FakeClient",
        bank_id: str,
        request_timeout: float | None,
    ) -> Any:
        handler = self.bank_handler or self.default_bank
        return await handler(client, bank_id, request_timeout)

    async def call_retain(
        self,
        client: "_FakeClient",
        kwargs: dict[str, Any],
    ) -> Any:
        handler = self.retain_handler or self.default_retain
        return await handler(client, kwargs)

    async def call_recall(
        self,
        client: "_FakeClient",
        call: dict[str, Any],
    ) -> Any:
        handler = self.recall_handler or self.default_recall
        return await handler(client, call)


@dataclass
class _FakeClient:
    backend: _FakeBackend
    client_kwargs: dict[str, Any]
    closed: int = 0

    async def aget_version(self) -> Any:
        self.backend.events.append("version")
        self.backend.calls.append(("version", {}))
        return await self.backend.call_version(self)

    async def arecall(
        self,
        *,
        bank_id: str,
        query: str,
        budget: str = "mid",
        max_tokens: int = 4096,
    ) -> Any:
        call = {
            "bank_id": bank_id,
            "query": query,
            "budget": budget,
            "max_tokens": max_tokens,
        }
        self.backend.events.append("recall")
        self.backend.calls.append(("recall", call))
        return await self.backend.call_recall(self, call)

    async def aretain_batch(self, **kwargs: Any) -> Any:
        self.backend.events.append("retain")
        self.backend.calls.append(("retain", dict(kwargs)))
        return await self.backend.call_retain(self, dict(kwargs))

    async def aclose(self) -> None:
        self.closed += 1

    @property
    def banks(self) -> Any:
        return SimpleNamespace(
            get_bank_config=self._get_bank_config,
            create_bank=self._forbidden("create_bank"),
            update_bank_config=self._forbidden("update_bank_config"),
            get_bank_profile=self._forbidden("get_bank_profile"),
        )

    async def _get_bank_config(
        self,
        bank_id: str,
        *,
        _request_timeout: float | None = None,
    ) -> Any:
        self.backend.events.append("bank")
        self.backend.calls.append(
            ("bank", {"bank_id": bank_id, "timeout": _request_timeout})
        )
        return await self.backend.call_bank(self, bank_id, _request_timeout)

    @property
    def documents(self) -> Any:
        return SimpleNamespace(
            get_document=self._get_document,
            list_documents=self._forbidden("list_documents"),
            delete_document=self._forbidden("delete_document"),
        )

    async def _get_document(
        self,
        bank_id: str,
        document_id: str,
        *,
        _request_timeout: float | None = None,
    ) -> Any:
        self.backend.events.append("document")
        self.backend.calls.append(
            (
                "document",
                {
                    "bank_id": bank_id,
                    "document_id": document_id,
                    "timeout": _request_timeout,
                },
            )
        )
        return await self.backend.call_document(
            self,
            bank_id,
            document_id,
            _request_timeout,
        )

    @property
    def operations(self) -> Any:
        return SimpleNamespace(
            get_operation_status=self._forbidden("get_operation_status"),
            list_operations=self._forbidden("list_operations"),
        )

    def _forbidden(self, name: str) -> Any:
        async def _call(*_args: object, **_kwargs: object) -> Any:
            self.backend.forbidden_calls.append(name)
            raise AssertionError(f"{name} must not be called")  # noqa: TRY003

        return _call


def _service(
    monkeypatch: pytest.MonkeyPatch,
    backend: _FakeBackend,
    config: _ConfigState | None = None,
) -> hindsight_service.HindsightMemoryService:
    monkeypatch.setattr(hindsight_service, "Hindsight", backend.build_client)
    return hindsight_service.HindsightMemoryService(
        config_getter=config or _ConfigState(_config())
    )


def _guard(
    backend: _FakeBackend,
    *,
    state: dict[str, bool] | None = None,
) -> Any:
    admission = {"admitted": True} if state is None else state

    def _check() -> bool:
        backend.events.append("guard")
        return admission["admitted"]

    return _check


def _events_since(backend: _FakeBackend, start: int) -> list[str]:
    return backend.events[start:]


def _calls_since(
    backend: _FakeBackend,
    start: int,
) -> list[tuple[str, dict[str, Any]]]:
    return backend.calls[start:]


def test_target_fingerprint_excludes_credentials_and_keeps_readiness_identity() -> None:
    first = hindsight_service.HindsightSettings.from_config(_config())
    rotated = hindsight_service.HindsightSettings.from_config(
        _config(
            hindsight_api_key="rotated-secret",
            hindsight_request_timeout_seconds=9.0,
            hindsight_recovery_interval_seconds=9.0,
        )
    )
    expected = hashlib.sha256(
        _canonical(
            {
                "bank_id": BANK_ID,
                "base_url": "http://127.0.0.1:9999",
            }
        ).encode("utf-8")
    ).hexdigest()

    assert first.target_fingerprint == expected
    assert rotated.target_fingerprint == first.target_fingerprint
    assert rotated.fingerprint != first.fingerprint

    normalized = hindsight_service.HindsightSettings.from_config(
        _config(hindsight_base_url="  http://127.0.0.1:9999/  ")
    )
    assert normalized.target_fingerprint == first.target_fingerprint

    other_endpoint = hindsight_service.HindsightSettings.from_config(
        _config(hindsight_base_url="http://127.0.0.1:9998")
    )
    other_bank = hindsight_service.HindsightSettings.from_config(
        _config(hindsight_bank_id="other-bank")
    )
    assert other_endpoint.target_fingerprint != first.target_fingerprint
    assert other_bank.target_fingerprint != first.target_fingerprint


async def test_open_session_rejects_expected_target_mismatch_before_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeBackend()
    config = _ConfigState(_config())
    service = _service(monkeypatch, backend, config)
    settings = hindsight_service.HindsightSettings.from_config(config.values)
    with pytest.raises(hindsight_service.HindsightUnavailableError) as captured:
        async with service.open_session(expected_target_fingerprint="b" * 64):
            pass

    assert captured.value.reason_code == "target_changed"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert backend.clients == []
    assert backend.events == []

    async with service.open_session(
        expected_target_fingerprint=settings.target_fingerprint
    ) as session:
        assert session.target_fingerprint == settings.target_fingerprint

    assert backend.events == ["bank", "recall", "bank"]
    assert [client.closed for client in backend.clients] == [1, 1]
    await service.close()


async def test_closed_session_rejects_retain_without_sdk_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeBackend()
    service = _service(monkeypatch, backend)
    request = _request()
    guard = _guard(backend)

    async with service.open_session() as session:
        await session.aclose()
        backend.events.clear()
        backend.calls.clear()

        with pytest.raises(hindsight_service.HindsightUnavailableError) as captured:
            await session.retain(request, submission_guard=guard)

        assert captured.value.reason_code == "session_closed"
        assert backend.events == []
        assert backend.calls == []

    await service.close()


async def test_retain_rejects_request_from_another_target_before_sdk_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeBackend()
    service = _service(monkeypatch, backend)
    old_target = hindsight_service.HindsightSettings.from_config(
        _config(hindsight_base_url="http://127.0.0.1:9998")
    ).target_fingerprint
    request = _request(target_fingerprint=old_target)
    guard = _guard(backend)

    async with service.open_session() as session:
        backend.events.clear()
        backend.calls.clear()

        with pytest.raises(hindsight_service.HindsightUnavailableError) as captured:
            await session.retain(request, submission_guard=guard)

        assert captured.value.reason_code == "target_changed"
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None
        assert backend.events == []
        assert backend.calls == []

    await service.close()


def _call_payloads(
    backend: _FakeBackend,
    start: int,
    name: str,
) -> list[dict[str, Any]]:
    return [
        payload for call_name, payload in backend.calls[start:] if call_name == name
    ]


def _matching_document_with_server_metadata(
    request: HindsightRetainRequest,
) -> DocumentResponse:
    metadata = {
        **BUSINESS_METADATA,
        **_reserved_metadata(request),
        "server_added_key": "server-added-value",
    }
    return _matching_document(request, metadata=metadata)


def _conflicting_document(  # noqa: PLR0911
    request: HindsightRetainRequest,
    variant: str,
) -> Any:
    metadata = {**BUSINESS_METADATA, **_reserved_metadata(request)}
    if variant == "text_diff":
        return _matching_document(request, text="different stored text")
    if variant == "text_none":
        return DocumentResponse.model_validate(
            {
                "id": request.document_id,
                "bank_id": BANK_ID,
                "original_text": None,
                "content_hash": "server-content-hash",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "memory_unit_count": 1,
                "document_metadata": metadata,
            }
        )
    if variant == "text_missing":
        return DocumentResponse.model_construct(
            id=request.document_id,
            bank_id=BANK_ID,
            document_metadata=metadata,
        )
    if variant == "bank_diff":
        return _matching_document(request, bank_id="other-bank")
    if variant == "id_diff":
        return _matching_document(request, document_id="other-document")
    if variant == "reserved_missing":
        metadata.pop("komari_online_payload_sha256")
        return _matching_document(request, metadata=metadata)
    if variant == "reserved_diff":
        metadata["komari_online_operation_id"] = "different-operation"
        return _matching_document(request, metadata=metadata)
    if variant == "business_diff":
        metadata["topic"] = "different-topic"
        return _matching_document(request, metadata=metadata)
    if variant == "metadata_none":
        return DocumentResponse.model_validate(
            {
                "id": request.document_id,
                "bank_id": BANK_ID,
                "original_text": request.content,
                "content_hash": "server-content-hash",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "memory_unit_count": 1,
                "document_metadata": None,
            }
        )
    raise AssertionError(f"unknown conflict variant {variant}")  # noqa: TRY003


async def test_retain_async_success_sends_exact_envelope_and_zero_operation_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeBackend()
    service = _service(monkeypatch, backend)
    request = _request(
        timestamp=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        metadata=BUSINESS_METADATA,
    )
    guard = _guard(backend)

    async with service.open_session() as session:
        event_start = len(backend.events)
        call_start = len(backend.calls)

        await session.retain(request, submission_guard=guard)

        assert _events_since(backend, event_start) == [
            "guard",
            "version",
            "document",
            "bank",
            "guard",
            "retain",
        ]
        assert _call_payloads(backend, call_start, "version") == [{}]
        assert _call_payloads(backend, call_start, "document") == [
            {
                "bank_id": BANK_ID,
                "document_id": request.document_id,
                "timeout": TIMEOUT,
            }
        ]
        assert _call_payloads(backend, call_start, "bank") == [
            {"bank_id": BANK_ID, "timeout": TIMEOUT}
        ]
        assert _call_payloads(backend, call_start, "retain") == [
            {
                "bank_id": BANK_ID,
                "items": [
                    {
                        "content": CONTENT,
                        "timestamp": "2026-01-02T03:04:05Z",
                        "metadata": {
                            **BUSINESS_METADATA,
                            **_reserved_metadata(request),
                        },
                    }
                ],
                "document_id": request.document_id,
                "retain_async": True,
                "operation_id": request.operation_id,
            }
        ]
        assert backend.forbidden_calls == []

    assert backend.clients[-1].closed == 1
    await service.close()


async def test_retain_accepts_existing_matching_document_without_new_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeBackend()
    service = _service(monkeypatch, backend)
    request = _request(metadata=BUSINESS_METADATA)
    backend.document = _matching_document_with_server_metadata(request)
    guard = _guard(backend)

    async with service.open_session() as session:
        event_start = len(backend.events)
        call_start = len(backend.calls)

        await session.retain(request, submission_guard=guard)

        assert _events_since(backend, event_start) == [
            "guard",
            "version",
            "document",
        ]
        assert _call_payloads(backend, call_start, "bank") == []
        assert _call_payloads(backend, call_start, "retain") == []
        assert backend.forbidden_calls == []

        backend.document = SimpleNamespace(
            id=request.document_id,
            bank_id=BANK_ID,
            original_text=CONTENT,
            document_metadata={
                **BUSINESS_METADATA,
                **_reserved_metadata(request),
            },
        )
        structural_start = len(backend.calls)
        await session.retain(request, submission_guard=guard)
        assert _call_payloads(backend, structural_start, "bank") == []
        assert _call_payloads(backend, structural_start, "retain") == []

    await service.close()


@pytest.mark.parametrize(
    "variant",
    [
        "text_diff",
        "text_none",
        "text_missing",
        "bank_diff",
        "id_diff",
        "reserved_missing",
        "reserved_diff",
        "business_diff",
        "metadata_none",
    ],
)
async def test_retain_never_overwrites_a_conflicting_document(
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    backend = _FakeBackend()
    service = _service(monkeypatch, backend)
    request = _request(metadata=BUSINESS_METADATA)
    backend.document = _conflicting_document(request, variant)
    guard = _guard(backend)

    async with service.open_session() as session:
        event_start = len(backend.events)
        call_start = len(backend.calls)
        with pytest.raises(hindsight_service.HindsightUnavailableError) as captured:
            await session.retain(request, submission_guard=guard)

        assert captured.value.reason_code == "document_conflict"
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None
        assert "retain" not in _events_since(backend, event_start)
        assert _call_payloads(backend, call_start, "retain") == []

        # A conflict is document-local and must not poison availability.
        backend.document = _ABSENT
        permitted = _request(
            source_id=f"conversation:{variant}",
            metadata={"topic": "其他"},
        )
        permitted_start = len(backend.events)
        await session.retain(permitted, submission_guard=_guard(backend))
        assert "retain" in _events_since(backend, permitted_start)

    await service.close()


async def test_document_read_failure_fails_closed_and_starts_outage(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_sentinel = "raw-sdk-read-body-secret"
    backend = _FakeBackend()
    service = _service(monkeypatch, backend)

    async def _broken_read(
        _client: _FakeClient,
        _bank_id: str,
        _document_id: str,
        _timeout: float | None,
    ) -> Any:
        raise RuntimeError(raw_sentinel)

    backend.document_handler = _broken_read
    request = _request()
    guard = _guard(backend)

    async with service.open_session() as session:
        call_start = len(backend.calls)
        with pytest.raises(hindsight_service.HindsightUnavailableError) as captured:
            await session.retain(request, submission_guard=guard)

        assert captured.value.reason_code == "document_read_failed"
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None
        assert raw_sentinel not in str(captured.value)
        assert raw_sentinel not in caplog.text
        assert _call_payloads(backend, call_start, "retain") == []
        assert _call_payloads(backend, call_start, "bank") == []

    with pytest.raises(hindsight_service.HindsightUnavailableError) as cooldown:
        async with service.open_session():
            pass
    assert cooldown.value.reason_code == "recovery_cooldown"
    await service.close()


@pytest.mark.parametrize("malformed", [object(), None, "not-a-document"])
async def test_malformed_document_response_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    malformed: object,
) -> None:
    backend = _FakeBackend()
    backend.document = malformed
    service = _service(monkeypatch, backend)

    async with service.open_session() as session:
        with pytest.raises(hindsight_service.HindsightUnavailableError) as captured:
            await session.retain(_request(), submission_guard=_guard(backend))
        assert captured.value.reason_code == "document_read_failed"
        assert not any(call_name == "retain" for call_name, _payload in backend.calls)

    await service.close()


async def test_missing_bank_immediately_before_retain_prevents_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeBackend()
    available = {"value": True}

    async def _bank(
        _client: _FakeClient,
        bank_id: str,
        _timeout: float | None,
    ) -> Any:
        if not available["value"]:
            raise NotFoundException(status=404, reason="Not Found")
        return SimpleNamespace(bank_id=bank_id)

    backend.bank_handler = _bank
    service = _service(monkeypatch, backend)
    request = _request()
    guard = _guard(backend)

    async with service.open_session() as session:
        available["value"] = False
        call_start = len(backend.calls)
        with pytest.raises(hindsight_service.HindsightUnavailableError) as captured:
            await session.retain(request, submission_guard=guard)

        assert captured.value.reason_code == "bank_unavailable"
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None
        assert _call_payloads(backend, call_start, "retain") == []

    await service.close()


@pytest.mark.parametrize(
    "version_response",
    [
        SimpleNamespace(api_version="1.0.9"),
        SimpleNamespace(api_version="1.1"),
        SimpleNamespace(api_version="not-semver"),
        SimpleNamespace(),
    ],
)
async def test_unsupported_version_blocks_all_document_preflight(
    monkeypatch: pytest.MonkeyPatch,
    version_response: object,
) -> None:
    backend = _FakeBackend()

    async def _version(_client: _FakeClient) -> Any:
        return version_response

    backend.version_handler = _version
    service = _service(monkeypatch, backend)
    request = _request()

    async with service.open_session() as session:
        call_start = len(backend.calls)
        with pytest.raises(hindsight_service.HindsightUnavailableError) as captured:
            await session.retain(request, submission_guard=_guard(backend))

        assert captured.value.reason_code == "target_version_unsupported"
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None
        assert _call_payloads(backend, call_start, "document") == []
        assert _call_payloads(backend, call_start, "bank") == []
        assert _call_payloads(backend, call_start, "retain") == []

    await service.close()


@pytest.mark.parametrize(
    ("variant", "expected_code"),
    [
        ("success_false", "retain_failed"),
        ("wrong_bank", "response_invalid"),
        ("sync_receipt", "response_invalid"),
        ("wrong_count", "response_invalid"),
        ("wrong_operation", "response_invalid"),
        ("missing_operation", "response_invalid"),
        ("foreign_receipt", "response_invalid"),
        ("none_receipt", "response_invalid"),
    ],
)
async def test_invalid_or_unknown_receipts_are_failures(
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
    expected_code: str,
) -> None:
    backend = _FakeBackend()

    async def _retain(  # noqa: PLR0911
        _client: _FakeClient,
        kwargs: dict[str, Any],
    ) -> Any:
        operation_id = kwargs["operation_id"]
        if variant == "success_false":
            return _receipt(success=False, operation_id=operation_id)
        if variant == "wrong_bank":
            return _receipt(
                bank_id="other-bank",
                operation_id=operation_id,
            )
        if variant == "sync_receipt":
            return _receipt(
                operation_id=operation_id,
                **{"async": False},
            )
        if variant == "wrong_count":
            return _receipt(items_count=2, operation_id=operation_id)
        if variant == "wrong_operation":
            return _receipt(operation_id="different-operation")
        if variant == "missing_operation":
            return _receipt(operation_id=None)
        if variant == "foreign_receipt":
            return object()
        return None

    backend.retain_handler = _retain
    service = _service(monkeypatch, backend)
    request = _request()

    async with service.open_session() as session:
        with pytest.raises(hindsight_service.HindsightUnavailableError) as captured:
            await session.retain(request, submission_guard=_guard(backend))

        assert captured.value.reason_code == expected_code
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None
        assert backend.forbidden_calls == []
        assert _call_payloads(backend, 0, "retain")

    await service.close()


@pytest.mark.parametrize("phase", ["document", "retain"])
async def test_entire_retain_attempt_uses_one_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    backend = _FakeBackend()

    async def _slow_document(
        _client: _FakeClient,
        _bank_id: str,
        _document_id: str,
        _timeout: float | None,
    ) -> Any:
        await asyncio.sleep(10)
        return _ABSENT

    async def _slow_retain(
        _client: _FakeClient,
        _kwargs: dict[str, Any],
    ) -> Any:
        await asyncio.sleep(10)
        return object()

    if phase == "document":
        backend.document_handler = _slow_document
    else:
        backend.retain_handler = _slow_retain
    service = _service(
        monkeypatch,
        backend,
        _ConfigState(_config(hindsight_request_timeout_seconds=0.05)),
    )

    async with service.open_session() as session:
        with pytest.raises(hindsight_service.HindsightUnavailableError) as captured:
            await session.retain(_request(), submission_guard=_guard(backend))

        assert captured.value.reason_code == "request_timeout"
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None

    await service.close()


async def test_cancellation_propagates_and_closes_owned_client_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeBackend()
    entered = asyncio.Event()

    async def _blocking_document(
        _client: _FakeClient,
        _bank_id: str,
        _document_id: str,
        _timeout: float | None,
    ) -> Any:
        entered.set()
        await asyncio.sleep(10)
        return _ABSENT

    backend.document_handler = _blocking_document
    service = _service(monkeypatch, backend)

    async def _run() -> None:
        async with service.open_session() as session:
            await session.retain(_request(), submission_guard=_guard(backend))

    task = asyncio.create_task(_run())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert backend.clients[-1].closed == 1

    backend.document_handler = None
    async with service.open_session() as session:
        await session.retain(_request(), submission_guard=_guard(backend))
    assert any(name == "retain" for name, _payload in backend.calls)
    await service.close()


async def test_shutdown_keeps_exact_client_ownership_for_retain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeBackend()
    service = _service(monkeypatch, backend)
    context = service.open_session()
    session = await context.__aenter__()
    client = backend.clients[-1]
    backend.events.clear()
    backend.calls.clear()

    await service.close()
    assert client.closed == 1

    with pytest.raises(hindsight_service.HindsightUnavailableError) as captured:
        await session.retain(_request(), submission_guard=_guard(backend))
    assert captured.value.reason_code == "session_closed"
    assert backend.events == []
    assert backend.calls == []

    await context.__aexit__(None, None, None)
    await service.close()
    assert client.closed == 1


async def test_guard_rejected_before_preflight_reads_does_not_poison_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeBackend()
    service = _service(monkeypatch, backend)
    admission = {"admitted": False}
    guard = _guard(backend, state=admission)
    request = _request()

    async with service.open_session() as session:
        backend.events.clear()
        backend.calls.clear()

        with pytest.raises(hindsight_service.HindsightUnavailableError) as captured:
            await session.retain(request, submission_guard=guard)

        assert captured.value.reason_code == "submission_not_admitted"
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None
        assert backend.events == ["guard"]
        assert backend.calls == []
        assert backend.forbidden_calls == []

        admission["admitted"] = True
        await session.retain(request, submission_guard=guard)
        assert [name for name, _payload in backend.calls] == [
            "version",
            "document",
            "bank",
            "retain",
        ]

    await service.close()


@pytest.mark.parametrize("phase", ["version", "document", "bank"])
async def test_guard_revoked_during_preflight_blocks_the_write(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    backend = _FakeBackend()
    admission = {"admitted": True}
    guard = _guard(backend, state=admission)

    async def _revoke() -> None:
        admission["admitted"] = False

    service = _service(monkeypatch, backend)

    async with service.open_session() as session:
        if phase == "version":

            async def _version(client: _FakeClient) -> Any:
                await _revoke()
                return await backend.default_version(client)

            backend.version_handler = _version
        elif phase == "document":

            async def _document(
                client: _FakeClient,
                bank_id: str,
                document_id: str,
                request_timeout: float | None,
            ) -> Any:
                await _revoke()
                return await backend.default_document(
                    client,
                    bank_id,
                    document_id,
                    request_timeout,
                )

            backend.document_handler = _document
        else:

            async def _bank(
                client: _FakeClient,
                bank_id: str,
                request_timeout: float | None,
            ) -> Any:
                await _revoke()
                return await backend.default_bank(
                    client,
                    bank_id,
                    request_timeout,
                )

            backend.bank_handler = _bank

        event_start = len(backend.events)
        with pytest.raises(hindsight_service.HindsightUnavailableError) as captured:
            await session.retain(_request(), submission_guard=guard)

        assert captured.value.reason_code == "submission_not_admitted"
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None
        events = _events_since(backend, event_start)
        assert events[-1] == "guard"
        assert events.count("guard") == 2
        assert "retain" not in events
        assert not any(call_name == "retain" for call_name, _payload in backend.calls)

        # Revocation is request-local and must not cool down the service.
        backend.version_handler = None
        backend.document_handler = None
        backend.bank_handler = None
        admission["admitted"] = True
        permitted = _request(source_id=f"conversation:revoked:{phase}")
        await session.retain(permitted, submission_guard=guard)
        assert any(call_name == "retain" for call_name, _payload in backend.calls)

    await service.close()


async def test_successful_version_check_is_cached_only_within_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeBackend()
    service = _service(monkeypatch, backend)

    async with service.open_session() as session:
        call_start = len(backend.calls)
        await session.retain(
            _request(source_id="conversation:first"),
            submission_guard=_guard(backend),
        )
        await session.retain(
            _request(source_id="conversation:second"),
            submission_guard=_guard(backend),
        )
        assert len(_call_payloads(backend, call_start, "version")) == 1
        assert len(_call_payloads(backend, call_start, "document")) == 2
        assert len(_call_payloads(backend, call_start, "retain")) == 2

    async with service.open_session() as new_session:
        call_start = len(backend.calls)
        await new_session.retain(
            _request(source_id="conversation:third"),
            submission_guard=_guard(backend),
        )
        assert len(_call_payloads(backend, call_start, "version")) == 1

    await service.close()


async def test_real_pinned_sdk_wire_contract_for_async_retain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []

    async def _record(request_value: web.Request, body: object = None) -> None:
        requests.append(
            {
                "method": request_value.method,
                "path": request_value.path,
                "authorization": request_value.headers.get("Authorization"),
                "body": body,
            }
        )

    async def _config_route(request_value: web.Request) -> web.Response:
        await _record(request_value)
        return web.json_response(
            {
                "bank_id": BANK_ID,
                "config": {},
                "overrides": {},
            }
        )

    async def _recall_route(request_value: web.Request) -> web.Response:
        await _record(request_value, await request_value.json())
        return web.json_response({"results": []})

    async def _version_route(request_value: web.Request) -> web.Response:
        await _record(request_value)
        return web.json_response(
            {
                "api_version": "1.1.0",
                "features": {
                    "observations": True,
                    "mcp": False,
                    "worker": True,
                    "bank_config_api": True,
                    "bank_llm_health": True,
                    "file_upload_api": True,
                    "document_export_api": True,
                    "document_import_api": True,
                    "audit_log": True,
                    "llm_trace": False,
                    "store_document_text": True,
                },
            }
        )

    async def _document_route(request_value: web.Request) -> web.Response:
        await _record(request_value)
        return web.json_response({"detail": "not found"}, status=404)

    async def _retain_route(request_value: web.Request) -> web.Response:
        body = await request_value.json()
        await _record(request_value, body)
        return web.json_response(
            {
                "success": True,
                "bank_id": BANK_ID,
                "items_count": 1,
                "async": True,
                "operation_id": body["operation_id"],
            }
        )

    async def _profile_route(request_value: web.Request) -> web.Response:
        await _record(request_value)
        return web.json_response({"detail": "retired"}, status=410)

    async def _operations_route(request_value: web.Request) -> web.Response:
        await _record(request_value)
        return web.json_response({"detail": "must not poll"}, status=500)

    async def _unexpected(request_value: web.Request) -> web.Response:
        await _record(request_value)
        raise web.HTTPNotFound(text="unexpected request")

    app = web.Application()
    app.router.add_get("/v1/default/banks/{bank_id}/config", _config_route)
    app.router.add_post(
        "/v1/default/banks/{bank_id}/memories/recall",
        _recall_route,
    )
    app.router.add_get("/version", _version_route)
    app.router.add_get(
        "/v1/default/banks/{bank_id}/documents/{document_id}",
        _document_route,
    )
    app.router.add_post("/v1/default/banks/{bank_id}/memories", _retain_route)
    app.router.add_get("/v1/default/banks/{bank_id}/profile", _profile_route)
    app.router.add_get(
        "/v1/default/banks/{bank_id}/operations/{operation_id}",
        _operations_route,
    )
    app.router.add_route("*", "/{tail:.*}", _unexpected)

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    server = TestServer(app)
    await server.start_server()
    service: hindsight_service.HindsightMemoryService | None = None
    try:
        config = _ConfigState(
            _config(hindsight_base_url=str(server.make_url("/")).rstrip("/"))
        )
        settings = hindsight_service.HindsightSettings.from_config(config.values)
        request = _request(
            target_fingerprint=settings.target_fingerprint,
            timestamp=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
            metadata=BUSINESS_METADATA,
        )
        service = hindsight_service.HindsightMemoryService(config_getter=config)
        async with service.open_session() as session:
            await session.retain(request, submission_guard=_wire_guard)
        await service.close()
    finally:
        if service is not None:
            await service.close()
        await server.close()

    paths = [entry["path"] for entry in requests]
    assert len([path for path in paths if path.endswith("/config")]) == 3
    assert len([path for path in paths if path.endswith("/memories/recall")]) == 1
    assert paths.count("/version") == 1
    assert len([path for path in paths if "/documents/" in path]) == 1
    assert len([path for path in paths if path.endswith("/memories")]) == 1
    assert not any(path.endswith("/profile") for path in paths)
    assert not any("/operations/" in path for path in paths)
    assert not any("/reflect" in path for path in paths)
    assert all(entry["authorization"] == f"Bearer {SECRET}" for entry in requests)

    retain_entries = [
        entry for entry in requests if entry["path"].endswith("/memories")
    ]
    assert len(retain_entries) == 1
    assert retain_entries[0]["body"] == {
        "items": [
            {
                "content": CONTENT,
                "timestamp": "2026-01-02T03:04:05Z",
                "metadata": {
                    **BUSINESS_METADATA,
                    **_reserved_metadata(request),
                },
                "document_id": request.document_id,
                "resolve_entities": True,
                "context": None,
                "entities": None,
                "tags": None,
                "observation_scopes": None,
                "strategy": None,
                "update_mode": None,
            }
        ],
        "async": True,
        "operation_id": request.operation_id,
        "document_tags": None,
    }


def _wire_guard() -> bool:
    return True


async def test_shutdown_during_preflight_never_releases_a_late_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeBackend()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _suspended_document(
        _client: _FakeClient,
        _bank_id: str,
        _document_id: str,
        _timeout: float | None,
    ) -> Any:
        entered.set()
        await release.wait()
        raise NotFoundException(status=404, reason="Not Found")

    backend.document_handler = _suspended_document
    service = _service(
        monkeypatch,
        backend,
        _ConfigState(_config(hindsight_request_timeout_seconds=5.0)),
    )
    request = _request()
    guard = _guard(backend)

    async def _run() -> None:
        async with service.open_session() as session:
            await session.retain(request, submission_guard=guard)

    task = asyncio.create_task(_run())
    await entered.wait()
    client = backend.clients[-1]

    await service.close()
    assert client.closed == 1
    release.set()

    with pytest.raises(hindsight_service.HindsightUnavailableError) as captured:
        await task

    assert captured.value.reason_code == "session_closed"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert not any(call_name == "retain" for call_name, _payload in backend.calls)
    assert client.closed == 1


async def _swallow_cancellation_then_return(seconds: float) -> None:
    """Simulate a boundary coroutine that suppresses timeout cancellation."""
    with suppress(asyncio.CancelledError):
        await asyncio.sleep(seconds)


async def test_late_valid_receipt_after_swallowed_timeout_is_not_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeBackend()

    async def _late_receipt(
        _client: _FakeClient,
        kwargs: dict[str, Any],
    ) -> Any:
        await _swallow_cancellation_then_return(1.0)
        return _receipt(operation_id=kwargs["operation_id"])

    backend.retain_handler = _late_receipt
    service = _service(
        monkeypatch,
        backend,
        _ConfigState(_config(hindsight_request_timeout_seconds=0.01)),
    )
    request = _request(metadata=BUSINESS_METADATA)

    async with service.open_session() as session:
        call_start = len(backend.calls)
        with pytest.raises(hindsight_service.HindsightUnavailableError) as captured:
            await session.retain(request, submission_guard=_guard(backend))

        assert captured.value.reason_code == "request_timeout"
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None
        assert _call_payloads(backend, call_start, "retain")
        assert backend.forbidden_calls == []

    await service.close()


@pytest.mark.parametrize("phase", ["version", "document", "bank"])
async def test_expired_preflight_never_issues_a_new_write(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    backend = _FakeBackend()
    service = _service(
        monkeypatch,
        backend,
        _ConfigState(_config(hindsight_request_timeout_seconds=0.01)),
    )
    request = _request(metadata=BUSINESS_METADATA)

    async with service.open_session() as session:
        if phase == "version":

            async def _late_version(client: _FakeClient) -> Any:
                await _swallow_cancellation_then_return(1.0)
                return await backend.default_version(client)

            backend.version_handler = _late_version
        elif phase == "document":

            async def _late_document(
                client: _FakeClient,
                bank_id: str,
                document_id: str,
                request_timeout: float | None,
            ) -> Any:
                await _swallow_cancellation_then_return(1.0)
                return await backend.default_document(
                    client,
                    bank_id,
                    document_id,
                    request_timeout,
                )

            backend.document_handler = _late_document
        else:

            async def _late_bank(
                client: _FakeClient,
                bank_id: str,
                request_timeout: float | None,
            ) -> Any:
                await _swallow_cancellation_then_return(1.0)
                return await backend.default_bank(
                    client,
                    bank_id,
                    request_timeout,
                )

            backend.bank_handler = _late_bank

        call_start = len(backend.calls)
        with pytest.raises(hindsight_service.HindsightUnavailableError) as captured:
            await session.retain(request, submission_guard=_guard(backend))

        assert captured.value.reason_code == "request_timeout"
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None
        assert not any(call_name == "retain" for call_name, _payload in backend.calls)

        if phase == "version":
            assert _call_payloads(backend, call_start, "document") == []
            assert _call_payloads(backend, call_start, "bank") == []
        elif phase == "document":
            assert len(_call_payloads(backend, call_start, "document")) == 1
            assert _call_payloads(backend, call_start, "bank") == []
        else:
            assert len(_call_payloads(backend, call_start, "bank")) == 1

    await service.close()


async def test_late_matching_document_is_not_accepted_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeBackend()
    service = _service(
        monkeypatch,
        backend,
        _ConfigState(_config(hindsight_request_timeout_seconds=0.01)),
    )
    request = _request(metadata=BUSINESS_METADATA)

    async def _late_matching_document(
        _client: _FakeClient,
        _bank_id: str,
        _document_id: str,
        _request_timeout: float | None,
    ) -> Any:
        await _swallow_cancellation_then_return(1.0)
        return _matching_document(request)

    async with service.open_session() as session:
        backend.document_handler = _late_matching_document
        call_start = len(backend.calls)
        with pytest.raises(hindsight_service.HindsightUnavailableError) as captured:
            await session.retain(request, submission_guard=_guard(backend))

        assert captured.value.reason_code == "request_timeout"
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None
        assert _call_payloads(backend, call_start, "bank") == []
        assert _call_payloads(backend, call_start, "retain") == []

    await service.close()
