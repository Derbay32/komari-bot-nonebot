"""TSK-200 debug entry-points: safe results only, no collector surface.

Drives the production ``.debug`` command handlers and report delivery with
fake platforms/business seams:

- the command owns a ``debug-*`` request ID and passes it explicitly;
- the private report renders request ID/type/status/safe error code and only
  whitelisted final-result fields;
- the group receipt never carries input/output/exception text;
- the image-failure safe summary is surfaced without URL/base64/vision text;
- ``DebugReplyResult``/entry signatures expose no collector alias.
"""

from __future__ import annotations

import inspect
from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast, get_type_hints

import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.event import Sender

from komari_bot.plugins.komari_chat.services.image_failure import (
    ImageFailureSummary,
)

if TYPE_CHECKING:
    from nonebug import App


def _build_group_event(plain_text: str, *, user_id: int = 42) -> GroupMessageEvent:
    message = Message(plain_text)
    return GroupMessageEvent.model_construct(
        time=1,
        self_id=669293859,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        message_id=1,
        group_id=12345,
        message=message,
        original_message=message,
        raw_message=plain_text,
        font=14,
        sender=Sender.model_construct(user_id=user_id, nickname="tester", card=""),
        to_me=True,
        reply=None,
    )


class _RecordingBot:
    self_id = "669293859"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def call_api(self, api: str, **kwargs: Any) -> None:
        self.calls.append({"api": api, **kwargs})


@pytest.fixture
def debug_commands(app: App) -> Any:
    del app
    return import_module("komari_bot.plugins.komari_debug.commands")


def _forward_payload(calls: list[dict[str, Any]], api: str) -> str:
    payloads = [str(call["messages"]) for call in calls if call["api"] == api]
    return "\n".join(payloads)


@pytest.mark.asyncio
async def test_private_report_renders_only_safe_request_and_final_fields(
    debug_commands: Any,
) -> None:
    bot = _RecordingBot()
    event = _build_group_event(".debug reply 测试")

    await debug_commands._deliver_debug_report(
        bot=bot,
        event=event,
        request_id="debug-reply-safe-1",
        result_type="reply",
        succeeded=True,
        public_requested=False,
        final_result_info={
            "reply_text": "只进私聊的回复正文",
            "favorability_delta": "好感度变化: +2",
            "raw_exception": "exception-canary-991",
            "tool_arguments": "tool-args-canary-992",
        },
    )

    private_payload = _forward_payload(bot.calls, "send_private_forward_msg")
    assert "请求 ID: debug-reply-safe-1" in private_payload
    assert "类型: reply" in private_payload
    assert "状态: 成功" in private_payload
    assert "只进私聊的回复正文" in private_payload
    assert "好感度变化: +2" in private_payload
    assert "exception-canary-991" not in private_payload
    assert "tool-args-canary-992" not in private_payload
    # 群内只有安全回执，没有诊断正文
    assert all(call["api"] != "send_group_forward_msg" for call in bot.calls)
    group_calls = [call for call in bot.calls if call["api"] == "send_group_msg"]
    assert len(group_calls) == 1
    receipt = str(group_calls[0]["message"])
    assert "debug-reply-safe-1" in receipt
    assert "只进私聊的回复正文" not in receipt
    assert "exception-canary-991" not in receipt


@pytest.mark.asyncio
async def test_debug_reply_command_owns_request_id_and_surfaces_safe_image_summary(
    debug_commands: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    summary = ImageFailureSummary(
        mode="native",
        all_images_unavailable=False,
        total_images=2,
        attempted_images=2,
        failed_images=1,
        error_types=("image_unavailable",),
        stages=("download",),
    )

    async def _fake_generate(**kwargs: Any) -> SimpleNamespace:
        captured["generate"] = kwargs
        trace_id = kwargs["request_trace_id"]
        assert isinstance(trace_id, str) and trace_id.startswith("debug-reply-")
        return SimpleNamespace(
            reply="干跑回复正文",
            reply_to_message_id=None,
            favorability_delta=1,
            favorability_reason="安全",
            request_id=trace_id,
            image_failure_summary=summary,
        )

    async def _fake_deliver(**kwargs: Any) -> None:
        captured["report"] = kwargs

    async def _allow_superuser(_bot: object, _event: object) -> bool:
        return True

    monkeypatch.setattr(debug_commands, "SUPERUSER", _allow_superuser)
    monkeypatch.setattr(debug_commands, "generate_debug_reply", _fake_generate)
    monkeypatch.setattr(debug_commands, "_deliver_debug_report", _fake_deliver)

    await debug_commands.handle_debug_reply(
        cast("Any", _RecordingBot()),
        _build_group_event(".debug reply 看图"),
        "看图",
    )

    generate_kwargs = captured["generate"]
    report_kwargs = captured["report"]
    assert "collector" not in generate_kwargs
    assert report_kwargs["request_id"] == generate_kwargs["request_trace_id"]
    assert report_kwargs["request_id"].startswith("debug-reply-")
    assert report_kwargs["succeeded"] is True
    assert report_kwargs["public_requested"] is False
    assert "collector" not in report_kwargs
    assert "extra_info" not in report_kwargs
    final = report_kwargs["final_result_info"]
    assert final["reply_text"] == "干跑回复正文"
    assert final["image_failure_mode"] == "native"
    assert final["image_failed_count"] == 1
    assert final["image_failure_stages"] == "download"
    # 安全摘要字段只有固定枚举/计数，不含 URL/base64/视觉正文
    rendered = repr(final)
    assert "https://" not in rendered
    assert "base64" not in rendered


@pytest.mark.asyncio
async def test_debug_reply_failure_reports_fixed_code_not_exception_text(
    debug_commands: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def _fail_generate(**_kwargs: Any) -> SimpleNamespace:
        msg = "provider said secret-exception-canary-993"
        raise RuntimeError(msg)

    async def _fake_deliver(**kwargs: Any) -> None:
        captured["report"] = kwargs

    async def _allow_superuser(_bot: object, _event: object) -> bool:
        return True

    monkeypatch.setattr(debug_commands, "SUPERUSER", _allow_superuser)
    monkeypatch.setattr(debug_commands, "generate_debug_reply", _fail_generate)
    monkeypatch.setattr(debug_commands, "_deliver_debug_report", _fake_deliver)

    await debug_commands.handle_debug_reply(
        cast("Any", _RecordingBot()),
        _build_group_event(".debug reply 出错"),
        "出错",
    )

    report_kwargs = captured["report"]
    assert report_kwargs["succeeded"] is False
    assert report_kwargs["error"] == "debug_reply_failed"
    assert "secret-exception-canary-993" not in repr(report_kwargs)
    assert "collector" not in report_kwargs


def test_debug_reply_result_has_no_collector_surface() -> None:
    message_handler = import_module(
        "komari_bot.plugins.komari_chat.handlers.message_handler"
    )
    fields = message_handler.DebugReplyResult.__dataclass_fields__
    assert "collector" not in fields
    hints = get_type_hints(message_handler.DebugReplyResult)
    assert hints["request_id"] is str
    assert hints["image_failure_summary"] == (
        message_handler.ImageFailureSummary | None
    )
    assert fields["image_failure_summary"].default is None
    for kept in (
        "reply",
        "reply_to_message_id",
        "favorability_delta",
        "favorability_reason",
    ):
        assert kept in fields


def test_entry_signatures_take_explicit_request_trace_id() -> None:
    summary_entry = import_module(
        "komari_bot.plugins.group_history_summary.execution_service"
    )
    message_handler = import_module(
        "komari_bot.plugins.komari_chat.handlers.message_handler"
    )

    for function in (
        message_handler.MessageHandler.generate_debug_reply,
        summary_entry.execute_group_summary,
    ):
        parameters = inspect.signature(function).parameters
        assert "collector" not in parameters
        assert "request_trace_id" in parameters
        assert parameters["request_trace_id"].default is None
