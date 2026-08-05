"""Ticket 08：Agent Run 协议标记与调试投影白名单验收测试。

- 每次 LLM 调用记录最终生效的 request_api 与 stream_enabled
- JSONL 完整保留 continuation（含加密推理项），走现有脱敏管线
- 调试报告安全投影只展示协议/流式/状态/finish/usage，不外泄 continuation
"""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING, Any

import pytest

from komari_bot.plugins.agent_run_logger.diagnostic import (
    AgentRunCollector,
    LLMCallTrace,
    completion_response_payload,
    record_completion_call,
    record_failed_call,
)
from komari_bot.plugins.llm_provider.base_client import (
    LLMCompletionResultSchema,
    LLMProviderContinuationSchema,
    UnifiedUsageSchema,
)

if TYPE_CHECKING:
    from nonebug import App


@pytest.fixture
def debug_reporting(app: App) -> Any:
    """延迟导入 reporting（需要 nonebot 初始化环境）。"""
    del app
    module_name = "komari_bot.plugins.komari_debug.reporting"
    sys.modules.pop(module_name, None)
    return import_module(module_name)


def _make_completion(*, with_continuation: bool = True) -> LLMCompletionResultSchema:
    continuation = None
    if with_continuation:
        continuation = LLMProviderContinuationSchema(
            api="responses",
            output_items=[
                {"type": "message", "id": "msg_1"},
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "encrypted_content": "gAAAAAB-encrypted-reasoning",
                },
            ],
        )
    return LLMCompletionResultSchema(
        content="回复正文",
        reasoning_content=None,
        tool_calls=[],
        finish_reason="stop",
        usage=UnifiedUsageSchema(input_tokens=10, output_tokens=5, total_tokens=15),
        duration_ms=12.0,
        continuation=continuation,
    )


def test_completion_response_payload_preserves_continuation() -> None:
    """响应载荷完整保留 continuation（含加密推理项）。"""
    payload = completion_response_payload(_make_completion())

    continuation = payload["continuation"]
    assert continuation is not None
    assert continuation["api"] == "responses"
    assert continuation["output_items"] == [
        {"type": "message", "id": "msg_1"},
        {
            "type": "reasoning",
            "id": "rs_1",
            "encrypted_content": "gAAAAAB-encrypted-reasoning",
        },
    ]


def test_completion_response_payload_without_continuation() -> None:
    payload = completion_response_payload(_make_completion(with_continuation=False))
    assert payload["continuation"] is None


def test_record_completion_call_captures_effective_request_mode() -> None:
    """成功调用记录最终生效的 request_api / stream_enabled。"""
    collector = AgentRunCollector(run_type="chat_reply")
    record_completion_call(
        collector,
        phase="normal_reply_round_1",
        round_index=0,
        method="generate_messages_completion",
        model="gpt-x",
        request={
            "messages": [{"role": "user", "content": "hi"}],
            "model": "gpt-x",
            "request_api": "responses",
            "stream_enabled": True,
        },
        completion=_make_completion(),
    )

    call = collector.calls[0]
    assert call.request_api == "responses"
    assert call.stream_enabled is True

    collector.mark_finished(status="success")
    record = collector.build_record()
    round_entry = record["rounds"][0]
    assert round_entry["request_api"] == "responses"
    assert round_entry["stream_enabled"] is True
    # JSONL 完整保留 continuation
    assert round_entry["response"]["continuation"]["api"] == "responses"
    assert (
        round_entry["response"]["continuation"]["output_items"][1][
            "encrypted_content"
        ]
        == "gAAAAAB-encrypted-reasoning"
    )
    # PG 轻索引结构不变：顶层不新增协议筛选字段
    assert "request_api" not in record
    assert "stream_enabled" not in record


def test_record_failed_call_captures_effective_request_mode() -> None:
    """失败调用同样记录协议标记。"""
    collector = AgentRunCollector(run_type="chat_reply")
    record_failed_call(
        collector,
        phase="normal_reply_round_1",
        round_index=0,
        method="generate_messages_completion",
        model="gpt-x",
        request={
            "messages": [],
            "request_api": "responses",
            "stream_enabled": False,
        },
        error=RuntimeError("boom"),
    )

    call = collector.calls[0]
    assert call.request_api == "responses"
    assert call.stream_enabled is False


def test_record_call_without_mode_keys_defaults_to_none() -> None:
    """未显式传协议标记时字段为 None（不伪造生效值）。"""
    collector = AgentRunCollector(run_type="chat_reply")
    record_completion_call(
        collector,
        phase="vision_read_image",
        round_index=0,
        method="generate_messages_completion",
        model="vision-model",
        request={"messages": []},
        completion=_make_completion(with_continuation=False),
    )

    call = collector.calls[0]
    assert call.request_api is None
    assert call.stream_enabled is None


def test_format_call_line_shows_protocol_whitelist_fields(
    debug_reporting: Any,
) -> None:
    """调试报告调用行只展示白名单字段：协议、流式、finish、usage。"""
    call = LLMCallTrace(
        phase="normal_reply_round_1",
        round_index=0,
        method="generate_messages_completion",
        model="gpt-x",
        request_api="responses",
        stream_enabled=True,
        finish_reason="stop",
        duration_ms=12.0,
        usage=UnifiedUsageSchema(input_tokens=10, output_tokens=5, total_tokens=15),
    )

    line = debug_reporting._format_call_line(call, 1)

    assert "responses" in line
    assert "流式" in line
    assert "stop" in line
    # continuation、加密推理项不出现在报告行
    assert "continuation" not in line
    assert "encrypted" not in line


def test_format_call_line_without_protocol_markers(debug_reporting: Any) -> None:
    """无协议标记的旧记录不伪造协议信息。"""
    call = LLMCallTrace(
        phase="vision_read_image",
        round_index=0,
        method="generate_messages_completion",
        model="vision-model",
        finish_reason="stop",
        duration_ms=1.0,
    )

    line = debug_reporting._format_call_line(call, 1)

    assert "responses" not in line
    assert "chat_completions" not in line
