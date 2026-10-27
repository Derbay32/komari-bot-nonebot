"""TSK-193 automatic and model-tool Hindsight recall at production seams."""

from __future__ import annotations

from importlib import import_module
from types import SimpleNamespace
from typing import Any

import pytest

from komari_bot.plugins.komari_memory.services.hindsight_service import (
    HindsightUnavailableError,
)
from tests.komari_chat.hindsight_recall_support import (
    FakeHindsightMemory,
    fake_recall_session,
)

pytestmark = pytest.mark.asyncio

message_handler_module = __import__(
    "komari_bot.plugins.komari_chat.handlers.message_handler",
    fromlist=["MessageHandler"],
)
MessageHandler = message_handler_module.MessageHandler


def _config(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "proactive_enabled": False,
        "context_messages_limit": 10,
        "context_max_utf8_bytes": 24_000,
        "context_max_estimated_tokens": 6_000,
        "summary_max_buffer_size": 500,
        "memory_search_limit": 3,
        "bot_nickname": "小鞠",
        "error_notify_enabled": False,
        "agent_max_rounds": 10,
        "agent_max_tool_calls_per_round": 4,
        "agent_max_total_tool_calls": 20,
        "agent_tool_call_mode": "required",
        "image_understanding_mode": "native",
        "llm_model_chat": "chat-model",
        "llm_temperature_chat": 0.7,
        "llm_max_tokens_chat": 1024,
        "llm_thinking_mode_chat": False,
        "llm_reasoning_effort_chat": "",
        "llm_request_api_chat": "chat_completions",
        "llm_stream_enabled_chat": False,
        "knowledge_enabled": False,
        "vision_image_download_max_count": 4,
        "vision_image_download_max_bytes": 8 * 1024 * 1024,
        "vision_image_download_total_max_bytes": 20 * 1024 * 1024,
        "vision_image_download_max_pixels": 40_000_000,
        "vision_image_download_concurrency": 2,
        "vision_image_download_connect_timeout_seconds": 5.0,
        "vision_image_download_read_timeout_seconds": 30.0,
        "vision_image_download_total_timeout_seconds": 45.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _message(content: str = "你好") -> Any:
    from komari_bot.context.redis_store import MessageSchema

    return MessageSchema(
        user_id="user-1",
        user_nickname="阿虚",
        group_id="group-1",
        content=content,
        timestamp=1.0,
        message_id="msg-1",
    )


class _FakeDshGeneration:
    """Trusted DSH seam double for handler-level session ownership tests."""

    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self.calls = calls
        self.error: BaseException | None = None
        self.result = import_module(
            "komari_bot.plugins.komari_chat.services.chat_result"
        ).ReplyResult(
            content="回复",
            favorability_delta=0,
            favorability_reason="无变化",
        )

    async def generate(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


class _Redis:
    async def get_buffer(self, _group_id: str, limit: int = 100) -> list[object]:
        del limit
        return []

    async def push_message(self, _group_id: str, _message: object) -> None:
        return None

    async def get_global_interaction_buffer(
        self,
        _user_id: str,
        limit: int = 10,
    ) -> list[dict[str, object]]:
        del limit
        return []


def _handler(memory: FakeHindsightMemory) -> Any:
    handler = MessageHandler.__new__(MessageHandler)
    handler.redis = _Redis()
    handler.hindsight_memory = memory
    return handler


def _wire_core(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
    *,
    generate_calls: list[dict[str, Any]],
) -> _FakeDshGeneration:
    service = _FakeDshGeneration(generate_calls)
    handler.dsh_chat = service
    monkeypatch.setattr(message_handler_module, "get_config", lambda: _config())
    monkeypatch.setattr(message_handler_module, "get_context_config", lambda: _config())
    return service


async def _run_core(handler: Any, message: Any | None = None) -> Any:
    return await handler._generate_reply_core(
        message=message or _message(),
        recent_messages=[],
        image_urls=None,
        reply_context=None,
        reply_context_requested=False,
        reply_context_refetched=False,
        request_trace_id="trace-1",
    )


async def test_generation_uses_the_supplied_agent_session_and_returns_host_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = FakeHindsightMemory()
    handler = _handler(memory)
    generate_calls: list[dict[str, Any]] = []
    service = _wire_core(monkeypatch, handler, generate_calls=generate_calls)

    result = await _run_core(handler)

    assert result.content == "回复"
    assert result.hindsight_target_fingerprint == memory.sessions[0].target_fingerprint
    assert service.calls == generate_calls
    assert len(generate_calls) == 1
    assert generate_calls[0]["recall_session"] is memory.sessions[0]
    assert memory.sessions[0].closed is True


@pytest.mark.parametrize(
    ("open_error", "expected"),
    [
        ("configuration_incomplete", "configuration_incomplete"),
        ("recovery_cooldown", "recovery_cooldown"),
    ],
)
async def test_unavailable_session_stops_before_generation(
    monkeypatch: pytest.MonkeyPatch,
    open_error: str,
    expected: str,
) -> None:
    memory = FakeHindsightMemory(open_error=HindsightUnavailableError(open_error))
    handler = _handler(memory)
    generate_calls: list[dict[str, Any]] = []
    _wire_core(monkeypatch, handler, generate_calls=generate_calls)

    with pytest.raises(HindsightUnavailableError) as captured:
        await _run_core(handler)

    assert captured.value.reason_code == expected
    assert generate_calls == []


@pytest.mark.parametrize("recall_error", ["recall_failed", "request_timeout"])
async def test_service_recall_failure_is_terminal_before_host_generation(
    monkeypatch: pytest.MonkeyPatch,
    recall_error: str,
) -> None:
    memory = FakeHindsightMemory()
    handler = _handler(memory)
    generate_calls: list[dict[str, Any]] = []
    service = _wire_core(monkeypatch, handler, generate_calls=generate_calls)
    service.error = HindsightUnavailableError(recall_error)

    with pytest.raises(HindsightUnavailableError) as captured:
        await _run_core(handler)

    assert captured.value.reason_code == recall_error
    assert len(generate_calls) == 1
    assert memory.sessions[0].closed is True


async def test_attempt_reply_reports_dsh_generation_failure_as_generation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = FakeHindsightMemory()
    handler = _handler(memory)
    generate_calls: list[dict[str, Any]] = []
    service = _wire_core(monkeypatch, handler, generate_calls=generate_calls)
    service.error = HindsightUnavailableError("bank_identity_mismatch")

    pending, _stored, failure = await handler._attempt_reply(
        bot_self_id="bot-1",
        adapter_name="OneBot V11",
        message=_message(),
        reply_to_message_id="msg-1",
        image_urls=None,
        reply_context=None,
        reply_context_requested=False,
        reply_context_refetched=False,
        force_reply=True,
        reason="platform_summon",
        recall_session=fake_recall_session(memory),
        store_current=False,
    )

    assert pending is None
    assert failure is not None
    assert failure.stage == "generate"
    assert failure.error_type == "HindsightUnavailableError"
    assert "bank_identity_mismatch" in (failure.summary or "")
    assert "secret" not in (failure.summary or "").lower()
    assert len(generate_calls) == 1


async def test_session_opens_before_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    memory = FakeHindsightMemory()
    original_open_session = memory.open_session

    def _open_session() -> Any:
        order.append("open")
        return original_open_session()

    memory.open_session = _open_session  # type: ignore[method-assign]
    handler = _handler(memory)
    generate_calls: list[dict[str, Any]] = []
    service = _wire_core(monkeypatch, handler, generate_calls=generate_calls)
    original_generate = service.generate

    async def _generate(**kwargs: Any) -> Any:
        order.append("generate")
        return await original_generate(**kwargs)

    service.generate = _generate  # type: ignore[method-assign]

    await _run_core(handler)
    assert order == ["open", "generate"]


async def test_admission_denied_before_session_and_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = FakeHindsightMemory()
    handler = _handler(memory)
    generate_calls: list[dict[str, Any]] = []
    _wire_core(monkeypatch, handler, generate_calls=generate_calls)
    monkeypatch.setattr(
        message_handler_module, "effect_business_admitted", lambda **_kw: False
    )

    result = await _run_core(handler)
    assert result.content == ""
    assert memory.open_calls == 0
    assert generate_calls == []
