"""Agent Run 完整采集与最小过滤边界测试。"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from komari_bot.plugins.agent_run_logger.config_schema import (
    AgentRunLoggerConfigSchema,
)
from komari_bot.plugins.agent_run_logger.diagnostic import (
    AgentRunCollector,
    ToolExecutionTrace,
    record_completion_call,
)
from komari_bot.plugins.agent_run_logger.sanitizer import sanitize_log_value
from komari_bot.plugins.llm_provider.base_client import UnifiedUsageSchema


def test_collector_keeps_full_text_reasoning_and_tools_but_filters_credentials() -> None:
    collector = AgentRunCollector(
        request_id="trace-full",
        run_type="chat_reply",
        task_kind="chat_reply",
        input_data={
            "message": "用户原文\n第二行",
            "profile": "完整画像正文",
            "authorization": "Bearer secret-token",
            "error_context": "Authorization: Bearer nested-secret",
        },
        persist=True,
    )
    completion = SimpleNamespace(
        content="完整回复正文",
        reasoning_content="完整 reasoning 正文",
        tool_calls=[{"id": "call-1", "function": {"name": "search_web"}}],
        finish_reason="tool_calls",
        duration_ms=125.5,
        usage=UnifiedUsageSchema(
            input_tokens=100,
            cached_input_tokens=40,
            cache_miss_input_tokens=60,
            output_tokens=50,
            reasoning_output_tokens=20,
            total_tokens=150,
        ),
    )
    call_id = record_completion_call(
        collector,
        phase="normal_reply_round_1",
        round_index=0,
        method="generate_messages_completion",
        model="deepseek-chat",
        request={
            "messages": [{"role": "user", "content": "完整 prompt 与历史"}],
            "api_key": "sk-secret",
        },
        completion=completion,
    )
    collector.add_tool(
        ToolExecutionTrace(
            call_id=call_id or "",
            tool_name="search_web",
            parsed_arguments={
                "query": "完整搜索词",
                "image_url": "https://example.com/private-image.png",
                "cookie": "session=secret",
            },
            status="success",
            result="完整网页工具结果",
            duration_ms=10.0,
        )
    )

    assert collector.mark_finished(
        status="success",
        output={"reply": "完整最终输出"},
    )
    assert not collector.mark_finished(status="error", error="重复结束")
    record = collector.build_record()
    serialized = json.dumps(record, ensure_ascii=False)

    assert record["schema_version"] == 3
    assert record["input"]["message"] == "用户原文\n第二行"
    assert "完整画像正文" in serialized
    assert "完整 prompt 与历史" in serialized
    assert "完整回复正文" in serialized
    assert "完整 reasoning 正文" in serialized
    assert "完整搜索词" in serialized
    assert "完整网页工具结果" in serialized
    assert "完整最终输出" in serialized
    assert "secret-token" not in serialized
    assert "nested-secret" not in serialized
    assert "sk-secret" not in serialized
    assert "session=secret" not in serialized
    assert "private-image.png" not in serialized
    assert record["rounds"][0]["response"]["reasoning_content"] == (
        "完整 reasoning 正文"
    )
    assert record["usage"]["total_tokens"] == 150
    assert record["usage"]["reasoning_output_tokens"] == 20
    assert record["usage"]["total_tokens_complete"] is True


def test_binary_and_image_values_are_replaced_with_stable_metadata() -> None:
    value = sanitize_log_value(
        {
            "image_base64": "QUJDRA==",
            "part": {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,QUJDRA=="},
            },
            "rendered_image": b"ABCD",
            "normal_text": "保留正文",
        }
    )

    expected_hash = hashlib.sha256(b"ABCD").hexdigest()
    assert value["image_base64"] == {
        "redacted_binary": True,
        "kind": "base64_text",
        "bytes": 4,
        "sha256": expected_hash,
        "source_chars": 8,
    }
    data_url = value["part"]["image_url"]["url"]
    assert data_url["mime_type"] == "image/png"
    assert data_url["bytes"] == 4
    assert data_url["sha256"] == expected_hash
    assert value["rendered_image"]["bytes"] == 4
    assert value["normal_text"] == "保留正文"


def test_new_config_forbids_old_provider_log_fields() -> None:
    config = AgentRunLoggerConfigSchema()
    schema = AgentRunLoggerConfigSchema.model_json_schema()
    assert config.log_enabled is True
    assert config.retention_days == 1
    assert set(schema["properties"]) >= {
        "log_enabled",
        "retention_days",
    }
    assert "llm_log_retention_days" not in schema["properties"]
    assert "llm_log_dir_permission_mode" not in schema["properties"]
    assert schema["additionalProperties"] is False
    with pytest.raises(ValidationError):
        AgentRunLoggerConfigSchema.model_validate({"llm_log_retention_days": 30})


@pytest.mark.parametrize("retention_days", [0, 91])
def test_new_config_rejects_invalid_retention(retention_days: int) -> None:
    with pytest.raises(ValidationError):
        AgentRunLoggerConfigSchema(retention_days=retention_days)
