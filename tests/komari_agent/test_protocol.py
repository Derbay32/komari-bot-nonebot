"""Unit tests for the versioned task envelope and closed frame kinds."""

from __future__ import annotations

import json

import pytest

from komari_bot.agent import (
    AgentProtocolError,
    AgentTaskBudget,
    AgentTaskRequest,
    TaskKind,
)
from komari_bot.agent.protocol import (
    ReadyFrame,
    decode_server_frame,
    encode_callback_result_error,
    encode_frame,
    encode_request,
)


def _request() -> AgentTaskRequest:
    return AgentTaskRequest(
        task_id="task-1",
        kind=TaskKind.CHAT,
        task_data={"message": "hello"},
        budget=AgentTaskBudget(max_rounds=3, max_tool_calls_per_round=4, max_total_tool_calls=8),
    )


def test_ready_frame_round_trips() -> None:
    frame = decode_server_frame(
        b'{"v":1,"kind":"ready","id":"ready","payload":{"state":"ready","protocol_version":1,'
        b'"host_version":"0.1.0","bindings":{"chat":true,"summary":false}}}\n'
    )
    assert isinstance(frame, ReadyFrame)
    assert frame.state == "ready"
    assert frame.bindings == {TaskKind.CHAT: True, TaskKind.SUMMARY: False}


def test_unknown_kind_fails_closed() -> None:
    with pytest.raises(AgentProtocolError, match="unknown_frame_kind"):
        decode_server_frame(b'{"v":1,"kind":"management","id":"x","payload":{}}\n')


def test_unknown_envelope_field_fails_closed() -> None:
    with pytest.raises(AgentProtocolError, match="invalid_envelope"):
        decode_server_frame(
            b'{"v":1,"kind":"result","id":"x","payload":{"ok":true,"result":{}},"extra":1}\n'
        )


def test_late_or_unknown_callback_fields_fail_closed() -> None:
    with pytest.raises(AgentProtocolError, match="invalid_callback_payload"):
        decode_server_frame(
            b'{"v":1,"kind":"callback","id":"cb-1","payload":{"request_id":"r1","task_id":"t1","name":"admission"}}\n'
        )


def test_oversized_frame_is_rejected_before_use() -> None:
    with pytest.raises(AgentProtocolError):
        encode_frame("cancel", "x", {"reason": "a" * (1024 * 1024)})


def test_task_request_serializes_closed_shape() -> None:
    encoded = encode_request("req-1", _request())
    parsed = json.loads(encoded)
    assert parsed["kind"] == "request"
    assert parsed["payload"]["kind"] == "chat"
    assert parsed["payload"]["task_data"] == {"message": "hello"}
    assert "messages" not in parsed["payload"]
    assert "tools" not in parsed["payload"]
    assert "credentials" not in parsed["payload"]


def test_task_budget_rejects_non_integer_and_out_of_bounds() -> None:
    with pytest.raises(TypeError):
        AgentTaskBudget(max_rounds=True, max_tool_calls_per_round=4, max_total_tool_calls=8)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="between 1 and"):
        AgentTaskBudget(max_rounds=0, max_tool_calls_per_round=4, max_total_tool_calls=8)


def test_task_data_rejects_non_json_values() -> None:
    with pytest.raises(TypeError):
        AgentTaskRequest(
            task_id="task-1",
            kind=TaskKind.CHAT,
            task_data={"callback": object()},  # type: ignore[dict-item]
            budget=AgentTaskBudget(max_rounds=3, max_tool_calls_per_round=4, max_total_tool_calls=8),
        )


def test_callback_error_is_sanitized_to_a_stable_code() -> None:
    encoded = encode_callback_result_error("cb-1", "Not a Safe Code")
    parsed = json.loads(encoded)
    assert parsed["payload"]["error"]["code"] == "callback_failed"
    assert "Not a Safe Code" not in encoded.decode()
