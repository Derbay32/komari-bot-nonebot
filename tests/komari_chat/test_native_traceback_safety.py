"""Recursive traceback-canary coverage for native image failures."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import os
import types
from importlib import import_module
from typing import Any

import pytest

from komari_bot.agent import AgentTaskError
from komari_bot.plugins.komari_chat.services.reply_context import ReplyContext
from tests.komari_chat.hindsight_recall_support import (
    FakeHindsightMemory,
    FakeRecallSession,
    recall_response,
)
from tests.komari_chat.test_native_image_transport import (
    _config,
    _FakeDownloader,
    _ImageHost,
    _png,
    _service,
    _wire_downloader,
)

failure_module = import_module(
    "komari_bot.plugins.komari_chat.services.image_failure"
)
message_handler_module = import_module(
    "komari_bot.plugins.komari_chat.handlers.message_handler"
)

_URL = "https://evil.example.native/private/photo.png?token=supersecret#frag"
_URL_B = "https://evil2.example.native/private/second.png?token=anothersecret"
_SOURCE = _png()
_BASE64 = base64.b64encode(_SOURCE).decode("ascii")
_DATA_URI = f"data:image/png;base64,{_BASE64}"
_BODY = "private-native-body-canary-9d6a"
_VISION = "private-native-vision-canary-4c2f"


def _iter_komari_chat_frames(exc: BaseException) -> list[types.FrameType]:
    frames: list[types.FrameType] = []
    tb = exc.__traceback__
    while tb is not None:
        filename = (tb.tb_frame.f_code.co_filename or "").replace(os.sep, "/")
        if "komari_bot/plugins/komari_chat" in filename:
            frames.append(tb.tb_frame)
        tb = tb.tb_next
    return frames


def _render_frame_locals(frame: types.FrameType) -> str:
    """Bounded recursive projection of frame locals, including slots."""
    visited: set[int] = set()
    parts: list[str] = []

    def _render(value: object, depth: int) -> None:
        if depth > 6:
            parts.append("<depth>")
            return
        if value is None or isinstance(value, (bool, int, float, str, bytes)):
            parts.append(repr(value))
            return
        if isinstance(
            value,
            (
                types.FrameType,
                types.FunctionType,
                types.BuiltinFunctionType,
                types.MethodType,
                types.ModuleType,
                types.CodeType,
                types.CoroutineType,
                types.GeneratorType,
                type,
            ),
        ):
            parts.append(repr(value))
            return
        value_id = id(value)
        if value_id in visited:
            parts.append("<cycle>")
            return
        visited.add(value_id)
        try:
            if isinstance(value, dict):
                parts.append("{")
                for key, item in value.items():
                    parts.append(repr(key) + ":")
                    _render(item, depth + 1)
                parts.append("}")
            elif isinstance(value, (list, tuple, set, frozenset)):
                parts.append("[")
                for item in value:
                    _render(item, depth + 1)
                parts.append("]")
            elif dataclasses.is_dataclass(value) and not isinstance(value, type):
                parts.append(type(value).__name__ + "(")
                for field in dataclasses.fields(value):
                    try:
                        _render(getattr(value, field.name), depth + 1)
                    except Exception:
                        parts.append("<unreadable>")
                parts.append(")")
            elif hasattr(value, "__dict__"):
                parts.append(type(value).__name__ + "{")
                for key, item in vars(value).items():
                    parts.append(repr(key) + ":")
                    _render(item, depth + 1)
                parts.append("}")
            elif hasattr(value, "__slots__"):
                parts.append(type(value).__name__ + "{")
                for slot in getattr(value, "__slots__", []):
                    if isinstance(slot, str):
                        try:
                            _render(getattr(value, slot), depth + 1)
                        except Exception:
                            parts.append("<unreadable>")
                parts.append("}")
            else:
                parts.append(repr(value))
        finally:
            visited.discard(value_id)

    _render(frame.f_locals, 0)
    return "".join(parts)


def _assert_frames_safe(exc: BaseException) -> list[types.FrameType]:
    frames = _iter_komari_chat_frames(exc)
    assert frames
    for frame in frames:
        rendered = _render_frame_locals(frame)
        for canary in (_URL, _URL_B, _BASE64, _DATA_URI, _BODY, _VISION):
            assert canary not in rendered, (
                f"frame {frame.f_code.co_name} retained canary {canary[:24]!r}"
            )
    return frames


class _DebugRedis:
    async def get_buffer(self, _group_id: str, limit: int = 100) -> list[object]:
        del limit
        return []

    async def push_message(self, _group_id: str, _message: object) -> None:
        return None


def _patch_no_store_recall(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _recall(_self: FakeRecallSession, _query: str) -> object:
        return recall_response()

    monkeypatch.setattr(FakeRecallSession, "recall", _recall)


def _debug_handler(host: _ImageHost) -> Any:
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = _DebugRedis()
    handler.hindsight_memory = FakeHindsightMemory(response=recall_response())
    handler._reaction_tasks = set()
    handler.dsh_chat = _service(host)
    return handler


def _quote() -> ReplyContext:
    return ReplyContext(
        source_side="user",
        message_id="quoted-native",
        user_id="u-quoted",
        user_nickname="Quoted",
        text=_BODY,
        image_sources=(_URL,),
        image_count=1,
        has_visible_image=True,
    )


def _debug_call(handler: Any) -> Any:
    return handler.generate_debug_reply(
        group_id="g-native-trace",
        user_id="u-native-trace",
        user_nickname="Debug",
        content="native traceback",
        image_urls=[_URL_B],
        reply_context=_quote(),
        request_trace_id="debug-native-trace",
    )


def test_all_download_failure_and_debug_frames_have_no_canaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        message_handler_module,
        "get_config",
        lambda: _config(),
    )
    monkeypatch.setattr(
        message_handler_module,
        "get_context_config",
        lambda: _config(),
    )
    _patch_no_store_recall(monkeypatch)
    downloader = _FakeDownloader({_URL: None, _URL_B: None})
    _wire_downloader(monkeypatch, downloader)
    host = _ImageHost()
    handler = _debug_handler(host)

    with pytest.raises(failure_module.ImageUnderstandingFailureError) as excinfo:
        asyncio.run(_debug_call(handler))

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    frames = _assert_frames_safe(excinfo.value)
    wrapper = next(frame for frame in frames if frame.f_code.co_name == "_generate_reply_core")
    core = next(frame for frame in frames if frame.f_code.co_name == "_generate_reply_with_session")
    debug_frame = next(frame for frame in frames if frame.f_code.co_name == "generate_debug_reply")
    assert wrapper.f_locals.get("image_urls") is None
    assert wrapper.f_locals.get("reply_context") is None
    assert core.f_locals.get("image_urls") is None
    assert core.f_locals.get("reply_context") is None
    assert debug_frame.f_locals.get("image_urls") is None
    assert debug_frame.f_locals.get("reply_context") is None
    assert debug_frame.f_locals.get("refetched_context") is None


def test_terminal_provider_failure_and_projection_failure_frames_are_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        message_handler_module,
        "get_config",
        lambda: _config(),
    )
    monkeypatch.setattr(
        message_handler_module,
        "get_context_config",
        lambda: _config(),
    )
    monkeypatch.setattr(
        message_handler_module,
        "GroupTaskFailureNotifier",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("debug must not notify")),
    )
    _patch_no_store_recall(monkeypatch)
    downloader = _FakeDownloader({_URL: _DATA_URI, _URL_B: _DATA_URI})
    _wire_downloader(monkeypatch, downloader)
    service_module = import_module(
        "komari_bot.plugins.komari_chat.services.dsh_chat_service"
    )
    monkeypatch.setattr(
        service_module,
        "_user_data_config",
        lambda: type("Config", (), {"max_favorability_delta_per_reply": 3})(),
    )

    provider_host = _ImageHost(
        error=AgentTaskError("image_model_failed", "unsafe provider body")
    )
    with pytest.raises(failure_module.ImageUnderstandingFailureError) as excinfo:
        asyncio.run(_debug_call(_debug_handler(provider_host)))
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    _assert_frames_safe(excinfo.value)

    transport_host = _ImageHost(
        error=AgentTaskError("invalid_image_input", "unsafe transport body")
    )
    with pytest.raises(service_module.ImageTransportTaskError) as excinfo:
        asyncio.run(_debug_call(_debug_handler(transport_host)))
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    _assert_frames_safe(excinfo.value)
