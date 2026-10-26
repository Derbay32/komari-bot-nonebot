"""Native image transport safety and failure taxonomy."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
from importlib import import_module
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PIL import Image

from komari_bot.agent import AgentHost, AgentTaskError, AgentTaskResult
from komari_bot.context.redis_store import MessageSchema
from komari_bot.plugins.komari_chat.services.image_downloader import (
    ImageDownloadPolicy,
)
from komari_bot.plugins.komari_chat.services.reply_context import ReplyContext
from tests.komari_chat.hindsight_recall_support import (
    FakeHindsightMemory,
    fake_recall_session,
    recall_response,
)

native_module = import_module(
    "komari_bot.plugins.komari_chat.services.native_image"
)
service_module = import_module(
    "komari_bot.plugins.komari_chat.services.dsh_chat_service"
)
failure_module = import_module(
    "komari_bot.plugins.komari_chat.services.image_failure"
)

_RAW_QUOTED = "https://example.invalid/private/quoted.png?token=quoted-secret"
_RAW_CURRENT = "https://example.invalid/private/current.png?token=current-secret"


def _png(color: tuple[int, int, int] = (10, 20, 30), size: tuple[int, int] = (2, 2)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _data_uri(data: bytes, media_type: str = "image/png") -> str:
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"


def _config(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "bot_nickname": "小鞠",
        "knowledge_enabled": False,
        "knowledge_limit": 3,
        "agent_max_rounds": 5,
        "agent_max_tool_calls_per_round": 3,
        "agent_max_total_tool_calls": 8,
        "vision_image_download_max_count": 8,
        "vision_image_download_max_bytes": 16 * 1024 * 1024,
        "vision_image_download_total_max_bytes": 32 * 1024 * 1024,
        "vision_image_download_max_pixels": 100_000_000,
        "vision_image_download_concurrency": 2,
        "vision_image_download_connect_timeout_seconds": 5.0,
        "vision_image_download_read_timeout_seconds": 30.0,
        "vision_image_download_total_timeout_seconds": 45.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _message(index: int = 0, content: str = "看看图片") -> MessageSchema:
    return MessageSchema(
        user_id=f"u-{index}",
        user_nickname=f"用户{index}",
        group_id="g-1",
        content=content,
        timestamp=float(index),
        message_id=f"m-{index}",
    )


class _FakeDownloader:
    def __init__(self, responses: dict[str, str | None]) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self.close_calls = 0
        self.downloaded_bytes = 0

    async def download(self, url: str) -> str | None:
        self.calls.append(url)
        result = self.responses.get(url)
        if result is not None:
            self.downloaded_bytes += len(result)
        return result

    async def close(self) -> None:
        self.close_calls += 1


def _wire_downloader(
    monkeypatch: pytest.MonkeyPatch,
    downloader: _FakeDownloader,
) -> None:
    monkeypatch.setattr(
        native_module,
        "ImageDownloadSession",
        lambda _policy: downloader,
    )


def test_descriptors_preserve_stable_quoted_first_order_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = _FakeDownloader(
        {
            _RAW_QUOTED: _data_uri(_png()),
            _RAW_CURRENT: _data_uri(_png((99, 1, 2))),
        }
    )
    _wire_downloader(monkeypatch, downloader)
    image_input = native_module.NativeImageInput(
        quoted_sources=[_RAW_QUOTED, "https://example.invalid/missing.png"],
        current_sources=[_RAW_CURRENT],
        policy=ImageDownloadPolicy(),
        admission_guard=lambda: True,
        group_id="g-1",
        request_trace_id="trace-native-1",
    )

    descriptors = asyncio.run(image_input.prepare())

    assert [descriptor.index for descriptor in descriptors] == [0, 2]
    assert [descriptor.origin for descriptor in descriptors] == ["quoted", "current"]
    for descriptor in descriptors:
        payload = asyncio.run(
            image_input.read_chunk(
                {"image_index": descriptor.index, "offset": 0}
            )
        )
        decoded = base64.b64decode(cast("str", payload["data"]), validate=True)
        assert payload == {
            "image_index": descriptor.index,
            "offset": 0,
            "data": base64.b64encode(decoded).decode("ascii"),
            "eof": True,
        }
        assert hashlib.sha256(decoded).hexdigest() == descriptor.sha256
        assert len(decoded) == descriptor.byte_count
    assert image_input.download_failures == 1
    assert image_input.all_downloads_failed is False
    assert _RAW_QUOTED not in repr(image_input)
    assert _RAW_CURRENT not in repr(image_input)

    asyncio.run(image_input.aclose())
    with pytest.raises(native_module.ImageChunkRejectedError):
        asyncio.run(image_input.read_chunk({"image_index": 0, "offset": 0}))
    assert downloader.close_calls == 1


def test_chunk_reader_is_bounded_and_rejects_malformed_offsets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = _png() * 1
    downloader = _FakeDownloader({_RAW_CURRENT: _data_uri(data)})
    _wire_downloader(monkeypatch, downloader)
    image_input = native_module.NativeImageInput(
        quoted_sources=[],
        current_sources=[_RAW_CURRENT],
        policy=ImageDownloadPolicy(max_image_bytes=16 * 1024 * 1024),
        admission_guard=lambda: True,
        group_id="g-1",
        request_trace_id="trace-native-2",
    )
    descriptor = asyncio.run(image_input.prepare())[0]

    seen = b""
    offset = 0
    while True:
        payload = asyncio.run(
            image_input.read_chunk({"image_index": descriptor.index, "offset": offset})
        )
        chunk = base64.b64decode(cast("str", payload["data"]), validate=True)
        assert 0 < len(chunk) <= native_module.MAX_TRUSTED_CHUNK_BYTES
        seen += chunk
        offset += len(chunk)
        if payload["eof"]:
            break
    assert seen == data

    for invalid in (
        {"image_index": True, "offset": 0},
        {"image_index": 0, "offset": False},
        {"image_index": 0, "offset": -1},
        {"image_index": 0, "offset": len(data)},
        {"image_index": 1, "offset": 0},
        {"image_index": 0},
        {"image_index": 0, "offset": 0, "extra": 1},
    ):
        with pytest.raises(native_module.ImageChunkRejectedError):
            asyncio.run(image_input.read_chunk(invalid))
    asyncio.run(image_input.aclose())


def test_admission_is_checked_at_each_download_and_chunk_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = _FakeDownloader({_RAW_CURRENT: _data_uri(_png())})
    _wire_downloader(monkeypatch, downloader)
    allowed = True
    image_input = native_module.NativeImageInput(
        quoted_sources=[],
        current_sources=[_RAW_CURRENT],
        policy=ImageDownloadPolicy(),
        admission_guard=lambda: allowed,
        group_id="g-1",
        request_trace_id="trace-native-3",
    )
    allowed = False
    with pytest.raises(native_module.ImageChunkAdmissionError):
        asyncio.run(image_input.prepare())
    assert downloader.calls == []
    asyncio.run(image_input.aclose())


def test_native_failure_summary_distinguishes_download_and_model_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = _FakeDownloader(
        {
            _RAW_QUOTED: _data_uri(_png()),
            _RAW_CURRENT: None,
        }
    )
    _wire_downloader(monkeypatch, downloader)
    image_input = native_module.NativeImageInput(
        quoted_sources=[_RAW_QUOTED],
        current_sources=[_RAW_CURRENT],
        policy=ImageDownloadPolicy(),
        admission_guard=lambda: True,
        group_id="g-1",
        request_trace_id="trace-native-4",
    )
    asyncio.run(image_input.prepare())

    partial = image_input.failure_summary()
    assert partial.mode == "native"
    assert partial.all_images_unavailable is False
    assert partial.total_images == 2
    assert partial.failed_images == 1
    assert partial.error_types == ("image_unavailable",)
    assert partial.stages == ("download",)

    model_failed = image_input.failure_summary(model_failed=True)
    assert model_failed.failed_images == 2
    assert model_failed.error_types == ("image_unavailable", "vision_failed")
    assert model_failed.stages == ("download", "vision")
    asyncio.run(image_input.aclose())


def _quote() -> ReplyContext:
    return ReplyContext(
        source_side="user",
        message_id="quote-1",
        user_id="u-2",
        user_nickname="引用用户",
        text="引用的图片",
        image_sources=(_RAW_QUOTED,),
        image_count=1,
        has_visible_image=True,
    )


class _ImageHost:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.requests: list[Any] = []
        self.callbacks: list[dict[str, Any]] = []

    async def execute(self, request: Any, callbacks: dict[str, Any]) -> AgentTaskResult:
        self.requests.append(request)
        self.callbacks.append(callbacks)
        if self.error is not None:
            raise self.error
        await self._read_all_images(request, callbacks)
        await callbacks["admission"](SimpleNamespace(name="admission", data={}))
        await callbacks["chat.submit_final_response"](
            SimpleNamespace(name="chat.submit_final_response", data={"content": "原生回复"})
        )
        await callbacks["chat.submit_favorability"](
            SimpleNamespace(
                name="chat.submit_favorability",
                data={"delta": 1, "reason": "原生图片"},
            )
        )
        return AgentTaskResult(
            task_id=request.task_id,
            kind=request.kind,
            result={
                "content": "原生回复",
                "favorability_delta": 1,
                "favorability_reason": "原生图片",
            },
        )

    async def _read_all_images(
        self, request: Any, callbacks: dict[str, Any]
    ) -> None:
        for descriptor in request.task_data["images"]:
            offset = 0
            while True:
                payload = await callbacks["chat.read_image_chunk"](
                    SimpleNamespace(
                        name="chat.read_image_chunk",
                        data={"image_index": descriptor["index"], "offset": offset},
                    )
                )
                chunk = base64.b64decode(cast("str", payload["data"]), validate=True)
                offset += len(chunk)
                if payload["eof"]:
                    break
            assert offset == descriptor["byte_count"]


class _EmptyKnowledge:
    async def __call__(self, **_kwargs: object) -> list[object]:
        return []


def _service(host: _ImageHost) -> Any:
    async def empty(**_kwargs: object) -> list[object]:
        return []

    async def uid(_uid: str) -> list[object]:
        return []

    async def favorability(_uid: str) -> object:
        return SimpleNamespace(
            favorability=4,
            stage_index=1,
            stage_name="熟悉",
            stage_prompt="熟悉",
        )

    return service_module.DshChatService(
        host_getter=lambda: cast("AgentHost", host),
        knowledge_search=empty,
        uid_search=uid,
        favorability_getter=favorability,
        group_bindings_getter=lambda _group: {},
    )


def test_service_sends_only_closed_descriptors_and_reads_bounded_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = _FakeDownloader(
        {
            _RAW_QUOTED: _data_uri(_png()),
            _RAW_CURRENT: _data_uri(_png((7, 8, 9))),
        }
    )
    _wire_downloader(monkeypatch, downloader)
    host = _ImageHost()
    monkeypatch.setattr(
        service_module, "_user_data_config", lambda: SimpleNamespace(max_favorability_delta_per_reply=3)
    )
    memory = FakeHindsightMemory(response=recall_response())
    result = asyncio.run(
        _service(host).generate(
            message=_message(0, content="当前图片"),
            recent_messages=[],
            reply_context=_quote(),
            recall_session=fake_recall_session(memory),
            request_trace_id="chat-native-service-1",
            config=_config(),
            image_urls=[_RAW_CURRENT],
        )
    )

    assert result.content == "原生回复"
    assert result.image_failure_summary is None
    assert len(host.requests) == 1
    task_data = host.requests[0].task_data
    assert [image["index"] for image in task_data["images"]] == [0, 1]
    assert [image["origin"] for image in task_data["images"]] == ["quoted", "current"]
    rendered = repr(task_data)
    assert _RAW_QUOTED not in rendered
    assert _RAW_CURRENT not in rendered
    assert "base64" not in rendered
    assert set(host.callbacks[0]) == {
        "admission",
        "chat.read_image_chunk",
        "chat.submit_final_response",
        "chat.submit_favorability",
    }


def test_service_all_downloads_failed_stops_before_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = _FakeDownloader({_RAW_CURRENT: None})
    _wire_downloader(monkeypatch, downloader)
    host = _ImageHost()
    monkeypatch.setattr(
        service_module, "_user_data_config", lambda: SimpleNamespace(max_favorability_delta_per_reply=3)
    )

    with pytest.raises(failure_module.ImageUnderstandingFailureError) as excinfo:
        asyncio.run(
            _service(host).generate(
                message=_message(0),
                recent_messages=[],
                reply_context=None,
                recall_session=cast(
                    "Any", fake_recall_session(FakeHindsightMemory(response=recall_response()))
                ),
                request_trace_id="chat-native-service-2",
                config=_config(),
                image_urls=[_RAW_CURRENT],
            )
        )

    assert host.requests == []
    summary = excinfo.value.summary
    assert summary.mode == "native"
    assert summary.all_images_unavailable is True
    assert summary.error_types == ("image_unavailable",)
    assert summary.stages == ("download",)


@pytest.mark.parametrize(
    ("code", "error_types", "stages"),
    [
        ("image_model_failed", ("vision_failed",), ("vision",)),
        ("image_input_failed", ("vision_failed",), ("vision",)),
    ],
)
def test_service_maps_verified_image_task_failures_to_safe_summary(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    error_types: tuple[str, ...],
    stages: tuple[str, ...],
) -> None:
    downloader = _FakeDownloader({_RAW_CURRENT: _data_uri(_png())})
    _wire_downloader(monkeypatch, downloader)
    host = _ImageHost(error=AgentTaskError(code, "unsafe provider text"))
    monkeypatch.setattr(
        service_module, "_user_data_config", lambda: SimpleNamespace(max_favorability_delta_per_reply=3)
    )

    with pytest.raises(failure_module.ImageUnderstandingFailureError) as excinfo:
        asyncio.run(
            _service(host).generate(
                message=_message(0),
                recent_messages=[],
                reply_context=None,
                recall_session=cast(
                    "Any", fake_recall_session(FakeHindsightMemory(response=recall_response()))
                ),
                request_trace_id="chat-native-service-3",
                config=_config(),
                image_urls=[_RAW_CURRENT],
            )
        )

    summary = excinfo.value.summary
    assert summary.mode == "native"
    assert summary.all_images_unavailable is False
    assert summary.failed_images == 1
    assert summary.error_types == error_types
    assert summary.stages == stages
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert "unsafe provider text" not in str(excinfo.value)


def test_service_keeps_invalid_transport_distinct_from_image_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = _FakeDownloader({_RAW_CURRENT: _data_uri(_png())})
    _wire_downloader(monkeypatch, downloader)
    host = _ImageHost(error=AgentTaskError("invalid_image_input", "unsafe transport text"))
    monkeypatch.setattr(
        service_module, "_user_data_config", lambda: SimpleNamespace(max_favorability_delta_per_reply=3)
    )

    with pytest.raises(service_module.ImageTransportTaskError) as excinfo:
        asyncio.run(
            _service(host).generate(
                message=_message(0),
                recent_messages=[],
                reply_context=None,
                recall_session=cast(
                    "Any", fake_recall_session(FakeHindsightMemory(response=recall_response()))
                ),
                request_trace_id="chat-native-service-4",
                config=_config(),
                image_urls=[_RAW_CURRENT],
            )
        )

    assert excinfo.value.code == "invalid_image_input"
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert "unsafe transport text" not in str(excinfo.value)


def test_partial_download_failure_success_attaches_safe_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = _FakeDownloader(
        {
            _RAW_QUOTED: _data_uri(_png()),
            _RAW_CURRENT: None,
        }
    )
    _wire_downloader(monkeypatch, downloader)
    host = _ImageHost()
    monkeypatch.setattr(
        service_module,
        "_user_data_config",
        lambda: SimpleNamespace(max_favorability_delta_per_reply=3),
    )
    result = asyncio.run(
        _service(host).generate(
            message=_message(0),
            recent_messages=[],
            reply_context=_quote(),
            recall_session=cast(
                "Any", fake_recall_session(FakeHindsightMemory(response=recall_response()))
            ),
            request_trace_id="chat-native-partial-1",
            config=_config(),
            image_urls=[_RAW_CURRENT],
        )
    )

    assert result.content == "原生回复"
    summary = result.image_failure_summary
    assert summary is not None
    assert summary.mode == "native"
    assert summary.total_images == 2
    assert summary.failed_images == 1
    assert summary.error_types == ("image_unavailable",)
    assert summary.stages == ("download",)
    assert len(host.requests[0].task_data["images"]) == 1


def test_partial_download_plus_model_failure_counts_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = _FakeDownloader(
        {
            _RAW_QUOTED: _data_uri(_png()),
            _RAW_CURRENT: None,
        }
    )
    _wire_downloader(monkeypatch, downloader)
    host = _ImageHost(error=AgentTaskError("image_model_failed", "unsafe"))
    monkeypatch.setattr(
        service_module,
        "_user_data_config",
        lambda: SimpleNamespace(max_favorability_delta_per_reply=3),
    )
    with pytest.raises(failure_module.ImageUnderstandingFailureError) as excinfo:
        asyncio.run(
            _service(host).generate(
                message=_message(0),
                recent_messages=[],
                reply_context=_quote(),
                recall_session=cast(
                    "Any", fake_recall_session(FakeHindsightMemory(response=recall_response()))
                ),
                request_trace_id="chat-native-partial-2",
                config=_config(),
                image_urls=[_RAW_CURRENT],
            )
        )
    summary = excinfo.value.summary
    assert summary.failed_images == 2
    assert summary.error_types == ("image_unavailable", "vision_failed")
    assert summary.stages == ("download", "vision")


def test_generic_task_error_with_images_is_not_wrapped_as_image_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = _FakeDownloader({_RAW_CURRENT: _data_uri(_png())})
    _wire_downloader(monkeypatch, downloader)
    host = _ImageHost(error=AgentTaskError("incomplete_result", "safe"))
    monkeypatch.setattr(
        service_module,
        "_user_data_config",
        lambda: SimpleNamespace(max_favorability_delta_per_reply=3),
    )
    with pytest.raises(AgentTaskError) as excinfo:
        asyncio.run(
            _service(host).generate(
                message=_message(0),
                recent_messages=[],
                reply_context=None,
                recall_session=cast(
                    "Any", fake_recall_session(FakeHindsightMemory(response=recall_response()))
                ),
                request_trace_id="chat-native-generic-1",
                config=_config(),
                image_urls=[_RAW_CURRENT],
            )
        )
    assert excinfo.value.code == "incomplete_result"
    assert not isinstance(excinfo.value, failure_module.ImageUnderstandingFailureError)


def test_text_only_generic_task_error_is_not_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = _FakeDownloader({})
    _wire_downloader(monkeypatch, downloader)
    host = _ImageHost(error=AgentTaskError("incomplete_result", "safe"))
    monkeypatch.setattr(
        service_module,
        "_user_data_config",
        lambda: SimpleNamespace(max_favorability_delta_per_reply=3),
    )
    with pytest.raises(AgentTaskError):
        asyncio.run(
            _service(host).generate(
                message=_message(0),
                recent_messages=[],
                reply_context=None,
                recall_session=cast(
                    "Any", fake_recall_session(FakeHindsightMemory(response=recall_response()))
                ),
                request_trace_id="chat-native-generic-2",
                config=_config(),
                image_urls=None,
            )
        )
    assert downloader.calls == []


def test_download_policy_is_frozen_before_recall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = _FakeDownloader(
        {
            _RAW_QUOTED: _data_uri(_png()),
            _RAW_CURRENT: _data_uri(_png((1, 2, 3))),
        }
    )
    _wire_downloader(monkeypatch, downloader)
    host = _ImageHost()
    monkeypatch.setattr(
        service_module,
        "_user_data_config",
        lambda: SimpleNamespace(max_favorability_delta_per_reply=3),
    )
    config = _config(vision_image_download_max_count=2)
    session = fake_recall_session(FakeHindsightMemory(response=recall_response()))
    original_recall = session.recall

    async def mutate_after_recall(query: str) -> Any:
        response = await original_recall(query)
        config.vision_image_download_max_count = 8
        return response

    session.recall = mutate_after_recall  # type: ignore[method-assign]

    result = asyncio.run(
        _service(host).generate(
            message=_message(0),
            recent_messages=[],
            reply_context=_quote(),
            recall_session=cast("Any", session),
            request_trace_id="chat-native-freeze-1",
            config=config,
            image_urls=[_RAW_CURRENT, "https://example.invalid/third"],
        )
    )

    assert result.content == "原生回复"
    assert downloader.calls == [_RAW_QUOTED, _RAW_CURRENT]
    assert len(host.requests[0].task_data["images"]) == 2


class _DebugRedis:
    async def get_buffer(self, _group_id: str, limit: int = 100) -> list[object]:
        del limit
        return []

    async def push_message(self, _group_id: str, _message: object) -> None:
        return None


def test_debug_image_failure_clears_handler_and_service_frame_locals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import types


    handler_module = import_module(
        "komari_bot.plugins.komari_chat.handlers.message_handler"
    )
    downloader = _FakeDownloader({_RAW_CURRENT: None})
    _wire_downloader(monkeypatch, downloader)
    host = _ImageHost()
    monkeypatch.setattr(
        service_module,
        "_user_data_config",
        lambda: SimpleNamespace(max_favorability_delta_per_reply=3),
    )
    handler = handler_module.MessageHandler.__new__(handler_module.MessageHandler)
    handler.redis = _DebugRedis()
    handler.hindsight_memory = FakeHindsightMemory(response=recall_response())
    handler._reaction_tasks = set()
    handler.dsh_chat = _service(host)
    monkeypatch.setattr(handler_module, "get_config", lambda: _config())
    monkeypatch.setattr(handler_module, "get_context_config", lambda: _config())

    with pytest.raises(failure_module.ImageUnderstandingFailureError) as excinfo:
        asyncio.run(
            handler.generate_debug_reply(
                group_id="g-debug",
                user_id="u-debug",
                user_nickname="调试",
                content="看图",
                image_urls=[_RAW_CURRENT],
                request_trace_id="debug-native-1",
            )
        )

    frames: list[types.FrameType] = []
    tb = excinfo.value.__traceback__
    while tb is not None:
        filename = tb.tb_frame.f_code.co_filename.replace("\\", "/")
        if "/komari_bot/plugins/komari_chat/" in filename:
            frames.append(tb.tb_frame)
        tb = tb.tb_next
    debug_frame = next(f for f in frames if f.f_code.co_name == "generate_debug_reply")
    assert debug_frame.f_locals.get("image_urls") is None
    assert debug_frame.f_locals.get("reply_context") is None
    service_frames = [f for f in frames if "dsh_chat_service.py" in f.f_code.co_filename]
    assert service_frames
    for frame in frames:
        rendered = repr(frame.f_locals)
        assert _RAW_CURRENT not in rendered
        assert "base64" not in rendered


def test_native_service_logs_do_not_expose_urls_or_payloads(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    source = _png()
    downloader = _FakeDownloader({_RAW_CURRENT: _data_uri(source)})
    _wire_downloader(monkeypatch, downloader)
    host = _ImageHost()
    monkeypatch.setattr(
        service_module,
        "_user_data_config",
        lambda: SimpleNamespace(max_favorability_delta_per_reply=3),
    )
    with caplog.at_level(logging.DEBUG):
        asyncio.run(
            _service(host).generate(
                message=_message(0),
                recent_messages=[],
                reply_context=None,
                recall_session=cast(
                    "Any", fake_recall_session(FakeHindsightMemory(response=recall_response()))
                ),
                request_trace_id="chat-native-logs-1",
                config=_config(),
                image_urls=[_RAW_CURRENT],
            )
        )
    rendered = caplog.text
    assert _RAW_CURRENT not in rendered
    assert "current-secret" not in rendered
    assert _data_uri(source).split(",", 1)[1] not in rendered
    assert "base64" not in rendered


def test_cancellation_during_download_closes_task_owned_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BlockingDownloader:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.close_calls = 0
            self.downloaded_bytes = 0

        async def download(self, _url: str) -> str | None:
            self.started.set()
            await self.release.wait()
            return _data_uri(_png())

        async def close(self) -> None:
            self.close_calls += 1

    downloader = _BlockingDownloader()
    monkeypatch.setattr(
        native_module,
        "ImageDownloadSession",
        lambda _policy: downloader,
    )
    host = _ImageHost()
    monkeypatch.setattr(
        service_module,
        "_user_data_config",
        lambda: SimpleNamespace(max_favorability_delta_per_reply=3),
    )

    async def run() -> None:
        task = asyncio.create_task(
            _service(host).generate(
                message=_message(0),
                recent_messages=[],
                reply_context=None,
                recall_session=cast(
                    "Any", fake_recall_session(FakeHindsightMemory(response=recall_response()))
                ),
                request_trace_id="chat-native-cancel-1",
                config=_config(),
                image_urls=[_RAW_CURRENT],
            )
        )
        await asyncio.wait_for(downloader.started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert downloader.close_calls >= 1
        assert host.requests == []

    asyncio.run(run())


def test_chunk_read_rechecks_admission_before_returning_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = _FakeDownloader({_RAW_CURRENT: _data_uri(_png())})
    _wire_downloader(monkeypatch, downloader)
    allowed = True
    image_input = native_module.NativeImageInput(
        quoted_sources=[],
        current_sources=[_RAW_CURRENT],
        policy=ImageDownloadPolicy(),
        admission_guard=lambda: allowed,
        group_id="g-1",
        request_trace_id="trace-native-admission",
    )
    descriptor = asyncio.run(image_input.prepare())[0]
    allowed = False
    with pytest.raises(native_module.ImageChunkAdmissionError) as excinfo:
        asyncio.run(
            image_input.read_chunk({"image_index": descriptor.index, "offset": 0})
        )
    assert excinfo.value.callback_code == "admission_denied"
    asyncio.run(image_input.aclose())
