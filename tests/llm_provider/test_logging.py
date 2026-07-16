"""LLM Provider 聊天日志开关测试。"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager
from importlib import import_module
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from komari_bot.common.llm_log_safety import sanitize_log_text

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

from komari_bot.plugins.llm_provider.base_client import (
    LLMCompletionResultSchema,
    UnifiedUsageSchema,
)
from komari_bot.plugins.llm_provider.config_schema import DynamicConfigSchema

llm_provider_module = import_module("komari_bot.plugins.llm_provider.__init__")
llm_logger_module = import_module("komari_bot.plugins.llm_provider.llm_logger")


async def _log_and_flush(**kwargs: Any) -> None:
    await llm_logger_module.log_llm_call(**kwargs)
    await llm_logger_module.flush_llm_logs()


class _FakeClientPool:
    def __init__(self, client: Any) -> None:
        self.client = client

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[Any]:
        yield self.client


class _FakeClient:
    def __init__(self, response: str) -> None:
        self.response = response
        self.closed = False
        self.calls: list[dict[str, Any]] = []

    async def generate_text(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return self.response

    async def generate_text_with_messages(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return self.response

    async def close(self) -> None:
        self.closed = True


class _FakeCompletionClient:
    def __init__(self, response: LLMCompletionResultSchema) -> None:
        self.response = response
        self.closed = False

    async def generate_text(self, **_kwargs: Any) -> LLMCompletionResultSchema:
        return self.response

    async def generate_text_with_messages(
        self, **_kwargs: Any
    ) -> LLMCompletionResultSchema:
        return self.response

    async def close(self) -> None:
        self.closed = True


class _FailingClient:
    def __init__(self, error_message: str) -> None:
        self.error_message = error_message
        self.closed = False

    async def generate_text(self, **_kwargs: Any) -> str:
        raise RuntimeError(self.error_message)

    async def close(self) -> None:
        self.closed = True


def test_log_llm_call_persists_only_fingerprints_and_private_directory(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(llm_logger_module, "_LOG_DIR", tmp_path / "llm_provider")
    monkeypatch.setattr(llm_logger_module.random, "random", lambda: 1.0)

    full_input = {
        "prompt": "完整 prompt 原文\n第二行",
        "system_instruction": "完整 system prompt 原文",
        "messages": [{"role": "user", "content": "完整 messages 原文"}],
    }

    asyncio.run(
        _log_and_flush(
            method="generate_text",
            model="deepseek-chat",
            input_data=full_input,
            output="完整 output 原文",
            reasoning_chars=len("完整 reasoning 原文"),
            duration_ms=12.345,
        )
    )

    log_dir = tmp_path / "llm_provider"
    [log_file] = list(log_dir.glob("*.jsonl"))
    record = json.loads(log_file.read_text(encoding="utf-8"))

    serialized_record = json.dumps(record, ensure_ascii=False)
    assert record["schema_version"] == 2
    assert record["input_summary"]["payload_fingerprint"] == (
        llm_logger_module.build_content_summary(full_input)
    )
    assert record["output_summary"] == llm_logger_module.build_content_summary(
        "完整 output 原文"
    )
    assert record["reasoning_chars"] == len("完整 reasoning 原文")
    assert "完整 prompt 原文" not in serialized_record
    assert "完整 system prompt 原文" not in serialized_record
    assert "完整 messages 原文" not in serialized_record
    assert "完整 output 原文" not in serialized_record
    assert "完整 reasoning 原文" not in serialized_record
    assert "reasoning_content" not in record
    assert log_dir.stat().st_mode & 0o777 == 0o700


def test_dynamic_config_schema_accepts_log_settings() -> None:
    config = DynamicConfigSchema()

    assert config.llm_log_retention_days == 30

    for mode in ("0o700", "0o750", ""):
        assert (
            DynamicConfigSchema(
                llm_log_dir_permission_mode=mode
            ).llm_log_dir_permission_mode
            == mode
        )


def test_log_file_contains_no_sensitive_canary_from_any_payload_layer(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    canaries = {
        "group": "CANARY_GROUP_MESSAGE_9f3a",
        "profile": "CANARY_USER_PROFILE_7c2b",
        "knowledge": "CANARY_KNOWLEDGE_4d8e",
        "web": "CANARY_WEB_RESULT_1a6f",
        "token": "CANARY_API_TOKEN_5b9c",
        "base64": "CANARY_IMAGE_BASE64_3e7d",
        "output": "CANARY_MODEL_OUTPUT_8a4c",
        "error": "CANARY_ERROR_BODY_2f6b",
    }
    monkeypatch.setattr(llm_logger_module, "_LOG_DIR", tmp_path / "llm_provider")
    monkeypatch.setattr(llm_logger_module.random, "random", lambda: 1.0)

    asyncio.run(
        _log_and_flush(
            method="generate_messages_completion",
            model="deepseek-chat",
            input_data={
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": canaries["group"]},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{canaries['base64']}"
                                },
                            },
                        ],
                    }
                ],
                "profile": canaries["profile"],
                "knowledge": canaries["knowledge"],
                "web_result": canaries["web"],
                "api_token": canaries["token"],
            },
            output=canaries["output"],
            error=canaries["error"],
            error_type="UpstreamError",
        )
    )

    [log_file] = list((tmp_path / "llm_provider").glob("*.jsonl"))
    persisted_text = log_file.read_text(encoding="utf-8")
    assert all(canary not in persisted_text for canary in canaries.values())


def test_scrub_legacy_logs_removes_historical_plaintext(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "llm_provider"
    log_dir.mkdir()
    legacy_file = log_dir / "2026-06-12.jsonl"
    legacy_file.write_text(
        json.dumps(
            {
                "timestamp": "2026-06-12T12:00:00+08:00",
                "method": "generate_messages_completion",
                "model": "deepseek-chat",
                "input": {
                    "trace_id": "chat-legacy",
                    "phase": "reply",
                    "messages": [{"role": "user", "content": "历史私密消息"}],
                },
                "output": "历史私密回复",
                "reasoning_content": "历史私密推理",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(llm_logger_module, "_LOG_DIR", log_dir)

    scrubbed = asyncio.run(llm_logger_module.scrub_legacy_logs())

    sanitized_text = legacy_file.read_text(encoding="utf-8")
    record = json.loads(sanitized_text)
    assert scrubbed == 1
    assert record["schema_version"] == 2
    assert record["trace_id"] == "chat-legacy"
    assert record["reasoning_chars"] == len("历史私密推理")
    assert "历史私密消息" not in sanitized_text
    assert "历史私密回复" not in sanitized_text
    assert "历史私密推理" not in sanitized_text
    assert sanitize_log_text(sanitized_text) == sanitized_text


def test_messages_payload_summary_keeps_only_image_volume_metadata() -> None:
    canary = "CANARY_IMAGE_BODY_5a2d"
    summary = llm_provider_module._summarize_messages_payload(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,QUJDRA=="},
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"https://example.com/{canary}.png"},
                    },
                ],
            }
        ]
    )

    assert summary["image_parts"] == 2
    assert summary["image_data_url_parts"] == 1
    assert summary["image_remote_url_parts"] == 1
    assert summary["image_bytes"] == 4
    assert canary not in json.dumps(summary, ensure_ascii=False)


@pytest.mark.parametrize("retention_days", [0, 91])
def test_dynamic_config_schema_rejects_invalid_log_retention_days(
    retention_days: int,
) -> None:
    with pytest.raises(ValidationError):
        DynamicConfigSchema(llm_log_retention_days=retention_days)


@pytest.mark.parametrize("permission_mode", ["700", "0o888", "private"])
def test_dynamic_config_schema_rejects_invalid_log_permission_mode(
    permission_mode: str,
) -> None:
    with pytest.raises(ValidationError):
        DynamicConfigSchema(llm_log_dir_permission_mode=permission_mode)


def test_cleanup_old_logs_respects_explicit_seven_days(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(llm_logger_module, "_LOG_DIR", tmp_path)
    (tmp_path / "2026-06-04.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "2026-06-05.jsonl").write_text("{}\n", encoding="utf-8")

    class _FixedDatetime(llm_logger_module.datetime):
        @classmethod
        def now(cls, _tz: Any = None) -> Any:
            return super().fromisoformat("2026-06-12T12:00:00+08:00")

    monkeypatch.setattr(llm_logger_module, "datetime", _FixedDatetime)

    asyncio.run(llm_logger_module.cleanup_old_logs(retention_days=7))

    assert not (tmp_path / "2026-06-04.jsonl").exists()
    assert (tmp_path / "2026-06-05.jsonl").exists()


def test_cleanup_old_logs_uses_dynamic_retention_days(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(llm_logger_module, "_LOG_DIR", tmp_path)
    monkeypatch.setattr(
        llm_logger_module,
        "_get_runtime_config",
        lambda: DynamicConfigSchema(llm_log_retention_days=30),
    )
    (tmp_path / "2026-05-12.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "2026-06-04.jsonl").write_text("{}\n", encoding="utf-8")

    class _FixedDatetime(llm_logger_module.datetime):
        @classmethod
        def now(cls, _tz: Any = None) -> Any:
            return super().fromisoformat("2026-06-12T12:00:00+08:00")

    monkeypatch.setattr(llm_logger_module, "datetime", _FixedDatetime)

    asyncio.run(llm_logger_module.cleanup_old_logs())

    assert not (tmp_path / "2026-05-12.jsonl").exists()
    assert (tmp_path / "2026-06-04.jsonl").exists()


def test_cleanup_old_logs_falls_back_to_thirty_days_on_config_error(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(llm_logger_module, "_LOG_DIR", tmp_path)

    def _raise_config_error() -> DynamicConfigSchema:
        msg = "配置读取失败"
        raise RuntimeError(msg)

    monkeypatch.setattr(llm_logger_module, "_get_runtime_config", _raise_config_error)
    (tmp_path / "2026-05-12.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "2026-06-04.jsonl").write_text("{}\n", encoding="utf-8")

    class _FixedDatetime(llm_logger_module.datetime):
        @classmethod
        def now(cls, _tz: Any = None) -> Any:
            return super().fromisoformat("2026-06-12T12:00:00+08:00")

    monkeypatch.setattr(llm_logger_module, "datetime", _FixedDatetime)

    asyncio.run(llm_logger_module.cleanup_old_logs())

    assert not (tmp_path / "2026-05-12.jsonl").exists()
    assert (tmp_path / "2026-06-04.jsonl").exists()


def test_log_llm_call_tightens_existing_directory_permission(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "llm_provider"
    log_dir.mkdir(mode=0o777)
    log_dir.chmod(0o777)
    monkeypatch.setattr(llm_logger_module, "_LOG_DIR", log_dir)
    monkeypatch.setattr(llm_logger_module.random, "random", lambda: 1.0)
    monkeypatch.setattr(
        llm_logger_module,
        "_get_log_dir_permission_mode",
        lambda: "0o750",
    )

    asyncio.run(
        _log_and_flush(
            method="generate_text",
            model="deepseek-chat",
            input_data="prompt",
            output="回复",
        )
    )

    assert log_dir.stat().st_mode & 0o777 == 0o750

    original_chmod = type(log_dir).chmod
    chmod_calls: list[int] = []

    def _count_chmod(self: Path, mode: int) -> None:
        if self == log_dir:
            chmod_calls.append(mode)
        original_chmod(self, mode)

    monkeypatch.setattr(type(log_dir), "chmod", _count_chmod)

    asyncio.run(
        _log_and_flush(
            method="generate_text",
            model="deepseek-chat",
            input_data="prompt",
            output="回复",
        )
    )

    assert chmod_calls == []


def test_log_llm_call_skips_chmod_when_permission_mode_is_empty(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "llm_provider"
    monkeypatch.setattr(llm_logger_module, "_LOG_DIR", log_dir)
    monkeypatch.setattr(llm_logger_module.random, "random", lambda: 1.0)
    monkeypatch.setattr(llm_logger_module, "_get_log_dir_permission_mode", lambda: "")
    chmod_calls: list[int] = []

    def _count_chmod(self: Path, mode: int) -> None:
        if self == log_dir:
            chmod_calls.append(mode)

    monkeypatch.setattr(type(log_dir), "chmod", _count_chmod)

    asyncio.run(
        _log_and_flush(
            method="generate_text",
            model="deepseek-chat",
            input_data="prompt",
            output="回复",
        )
    )

    assert chmod_calls == []
    assert list(log_dir.glob("*.jsonl"))


def test_log_llm_call_continues_when_permission_mode_is_invalid(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    log_dir = tmp_path / "llm_provider"
    monkeypatch.setattr(llm_logger_module, "_LOG_DIR", log_dir)
    monkeypatch.setattr(llm_logger_module.random, "random", lambda: 1.0)
    monkeypatch.setattr(
        llm_logger_module, "_get_log_dir_permission_mode", lambda: "bad"
    )
    chmod_calls: list[int] = []

    def _count_chmod(self: Path, mode: int) -> None:
        if self == log_dir:
            chmod_calls.append(mode)

    monkeypatch.setattr(type(log_dir), "chmod", _count_chmod)

    asyncio.run(
        _log_and_flush(
            method="generate_text",
            model="deepseek-chat",
            input_data="prompt",
            output="回复",
        )
    )

    assert chmod_calls == []
    assert list(log_dir.glob("*.jsonl"))


def test_generate_text_does_not_record_log_by_default(monkeypatch: Any) -> None:
    fake_client = _FakeClient("普通调用结果")
    log_calls: list[dict[str, Any]] = []

    async def _fake_log_llm_call(**kwargs: Any) -> None:
        log_calls.append(kwargs)

    monkeypatch.setattr(
        llm_provider_module, "_client_pool", _FakeClientPool(fake_client)
    )
    monkeypatch.setattr(llm_provider_module, "log_llm_call", _fake_log_llm_call)

    result = asyncio.run(
        llm_provider_module.generate_text(
            prompt="这是一段内部总结请求",
            model="deepseek-chat",
        )
    )

    assert result == "普通调用结果"
    assert fake_client.closed is False
    assert log_calls == []


def test_generate_text_error_log_does_not_include_upstream_body(
    monkeypatch: Any,
) -> None:
    canary = "CANARY_UPSTREAM_ERROR_BODY_6d4a"
    fake_client = _FailingClient(canary)
    logged_parts: list[str] = []

    def capture_error(message: object, *args: object, **_kwargs: object) -> None:
        logged_parts.append(f"{message!s} {args!r}")

    monkeypatch.setattr(
        llm_provider_module, "_client_pool", _FakeClientPool(fake_client)
    )
    monkeypatch.setattr(llm_provider_module.logger, "error", capture_error)

    with pytest.raises(RuntimeError, match=canary):
        asyncio.run(
            llm_provider_module.generate_text(
                prompt="测试",
                model="deepseek-chat",
            )
        )

    assert fake_client.closed is False
    assert canary not in " ".join(logged_parts)
    assert "RuntimeError" in " ".join(logged_parts)


def test_generate_text_with_messages_records_log_for_chat_reply(
    monkeypatch: Any,
) -> None:
    fake_client = _FakeClient("<content>聊天回复</content>")
    log_calls: list[dict[str, Any]] = []

    async def _fake_log_llm_call(**kwargs: Any) -> None:
        log_calls.append(kwargs)

    monkeypatch.setattr(
        llm_provider_module, "_client_pool", _FakeClientPool(fake_client)
    )
    monkeypatch.setattr(llm_provider_module, "log_llm_call", _fake_log_llm_call)

    result = asyncio.run(
        llm_provider_module.generate_text_with_messages(
            messages=[{"role": "user", "content": "你好"}],
            model="deepseek-chat",
            request_trace_id="chat-1001",
            record_chat_log=True,
        )
    )

    assert result == "<content>聊天回复</content>"
    assert fake_client.closed is False
    assert len(log_calls) == 1
    assert log_calls[0]["method"] == "generate_text_with_messages"
    assert log_calls[0]["input_data"]["trace_id"] == "chat-1001"


def test_generate_text_records_only_request_fingerprints(monkeypatch: Any) -> None:
    fake_client = _FakeClient("聊天回复")
    log_calls: list[dict[str, Any]] = []

    async def _fake_log_llm_call(**kwargs: Any) -> None:
        log_calls.append(kwargs)

    async def _fake_search_knowledge(_query: str, *, limit: int) -> list[Any]:
        assert limit == 2
        return [type("Knowledge", (), {"content": "知识库内容"})()]

    monkeypatch.setattr(
        llm_provider_module, "_client_pool", _FakeClientPool(fake_client)
    )
    monkeypatch.setattr(llm_provider_module, "log_llm_call", _fake_log_llm_call)
    monkeypatch.setattr(
        llm_provider_module.knowledge_plugin,
        "search_knowledge",
        _fake_search_knowledge,
    )

    result = asyncio.run(
        llm_provider_module.generate_text(
            prompt="完整 prompt",
            model="deepseek-chat",
            system_instruction="系统 {{DYNAMIC_KNOWLEDGE_BASE}}",
            temperature=1,
            max_tokens=128,
            response_format={"type": "json_object"},
            enable_knowledge=True,
            knowledge_query="查询词",
            knowledge_limit=2,
            record_chat_log=True,
            request_trace_id="trace-1",
            request_phase="reply",
            frequency_penalty=0.2,
        )
    )

    assert result == "聊天回复"
    input_data = log_calls[0]["input_data"]
    serialized_input = json.dumps(input_data, ensure_ascii=False)
    assert input_data["prompt_summary"] == llm_logger_module.build_content_summary(
        "完整 prompt"
    )
    assert input_data["system_instruction_summary"] == (
        llm_logger_module.build_content_summary(
            "系统 相关知识已作为独立的不可信数据块提供。"
        )
    )
    assert input_data["temperature"] == 1
    assert input_data["max_tokens"] == 128
    assert input_data["response_format_summary"] == (
        llm_logger_module.build_content_summary({"type": "json_object"})
    )
    assert input_data["enable_knowledge"] is True
    assert input_data["knowledge_query_summary"] == (
        llm_logger_module.build_content_summary("查询词")
    )
    assert input_data["knowledge_limit"] == 2
    assert input_data["parameter_keys"] == ["frequency_penalty"]
    assert "完整 prompt" not in serialized_input
    assert "知识库内容" not in serialized_input
    assert "查询词" not in serialized_input
    assert "知识库内容" not in fake_client.calls[0]["system_instruction"]
    [knowledge_context] = fake_client.calls[0]["untrusted_contexts"]
    assert knowledge_context.source_type == "knowledge"
    assert knowledge_context.content == "知识库内容"


def test_generate_messages_completion_records_only_safe_metadata(
    monkeypatch: Any,
) -> None:
    response = LLMCompletionResultSchema(
        content="完整 completion 输出",
        reasoning_content="完整思考内容",
        finish_reason="stop",
    )
    fake_client = _FakeCompletionClient(response)
    log_calls: list[dict[str, Any]] = []

    async def _fake_log_llm_call(**kwargs: Any) -> None:
        log_calls.append(kwargs)

    monkeypatch.setattr(
        llm_provider_module, "_client_pool", _FakeClientPool(fake_client)
    )
    monkeypatch.setattr(llm_provider_module, "log_llm_call", _fake_log_llm_call)

    messages = [{"role": "user", "content": "完整 messages 原文"}]
    tools = [{"type": "function", "function": {"name": "query"}}]
    result = asyncio.run(
        llm_provider_module.generate_messages_completion(
            messages=messages,
            model="deepseek-chat",
            temperature=0.7,
            max_tokens=256,
            response_format={"type": "json_object"},
            record_chat_log=True,
            tools=tools,
            tool_choice="auto",
            parallel_tool_calls=True,
            request_trace_id="trace-2",
            top_p=0.9,
        )
    )

    assert result == response
    assert fake_client.closed is False
    log_call = log_calls[0]
    input_data = log_call["input_data"]
    serialized_input = json.dumps(input_data, ensure_ascii=False)
    assert (
        input_data["payload_summary"]["sha256"]
        == (llm_logger_module.build_content_summary(messages)["sha256"])
    )
    assert input_data["tools_count"] == 1
    assert input_data["tools_summary"] == llm_logger_module.build_content_summary(tools)
    assert input_data["tool_choice_summary"] == (
        llm_logger_module.build_content_summary("auto")
    )
    assert input_data["parallel_tool_calls"] is True
    assert input_data["temperature"] == 0.7
    assert input_data["max_tokens"] == 256
    assert input_data["response_format_summary"] == (
        llm_logger_module.build_content_summary({"type": "json_object"})
    )
    assert input_data["parameter_keys"] == ["top_p"]
    assert "完整 messages 原文" not in serialized_input
    assert "query" not in serialized_input
    assert log_call["output"] == "完整 completion 输出"
    assert log_call["reasoning_chars"] == len("完整思考内容")
    assert "reasoning_content" not in log_call
    assert log_call["finish_reason"] == "stop"


def test_log_llm_call_records_full_usage_fields(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setattr(llm_logger_module, "_LOG_DIR", tmp_path / "llm_provider")
    monkeypatch.setattr(llm_logger_module.random, "random", lambda: 1.0)

    usage = UnifiedUsageSchema(
        input_tokens=100,
        cached_input_tokens=60,
        cache_miss_input_tokens=40,
        output_tokens=50,
        reasoning_output_tokens=20,
        total_tokens=150,
    )

    asyncio.run(
        _log_and_flush(
            method="generate_completion",
            model="deepseek-chat",
            input_data={"prompt": "测试"},
            output="回复",
            duration_ms=99.99,
            usage=usage,
        )
    )

    log_dir = tmp_path / "llm_provider"
    [log_file] = list(log_dir.glob("*.jsonl"))
    record = json.loads(log_file.read_text(encoding="utf-8"))

    assert record["usage"] == {
        "input_tokens": 100,
        "cached_input_tokens": 60,
        "cache_miss_input_tokens": 40,
        "output_tokens": 50,
        "reasoning_output_tokens": 20,
        "total_tokens": 150,
    }
    assert record["duration_ms"] == 99.99


def test_log_llm_call_skips_none_usage_fields(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr(llm_logger_module, "_LOG_DIR", tmp_path / "llm_provider")
    monkeypatch.setattr(llm_logger_module.random, "random", lambda: 1.0)

    usage = UnifiedUsageSchema(
        input_tokens=10,
        output_tokens=5,
    )

    asyncio.run(
        _log_and_flush(
            method="generate_text",
            model="deepseek-chat",
            input_data="prompt",
            output="回复",
            usage=usage,
        )
    )

    log_dir = tmp_path / "llm_provider"
    [log_file] = list(log_dir.glob("*.jsonl"))
    record = json.loads(log_file.read_text(encoding="utf-8"))

    assert "input_tokens" in record["usage"]
    assert record["usage"]["input_tokens"] == 10
    assert "output_tokens" in record["usage"]
    assert record["usage"]["output_tokens"] == 5
    # 未报告的字段不应出现在 JSONL 中
    assert "cached_input_tokens" not in record["usage"]
    assert "cache_miss_input_tokens" not in record["usage"]
    assert "reasoning_output_tokens" not in record["usage"]
    assert "total_tokens" not in record["usage"]


def test_log_llm_call_without_usage_omits_usage_key(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setattr(llm_logger_module, "_LOG_DIR", tmp_path / "llm_provider")
    monkeypatch.setattr(llm_logger_module.random, "random", lambda: 1.0)

    asyncio.run(
        _log_and_flush(
            method="generate_text",
            model="deepseek-chat",
            input_data="prompt",
            output="回复",
        )
    )

    log_dir = tmp_path / "llm_provider"
    [log_file] = list(log_dir.glob("*.jsonl"))
    record = json.loads(log_file.read_text(encoding="utf-8"))

    assert "usage" not in record


def test_generate_completion_records_usage_in_jsonl(monkeypatch: Any) -> None:
    usage = UnifiedUsageSchema(
        input_tokens=20,
        output_tokens=10,
        total_tokens=30,
    )
    response = LLMCompletionResultSchema(
        content="completion 输出",
        finish_reason="stop",
        usage=usage,
    )
    fake_client = _FakeCompletionClient(response)
    log_calls: list[dict[str, Any]] = []

    async def _fake_log_llm_call(**kwargs: Any) -> None:
        log_calls.append(kwargs)

    monkeypatch.setattr(
        llm_provider_module, "_client_pool", _FakeClientPool(fake_client)
    )
    monkeypatch.setattr(llm_provider_module, "log_llm_call", _fake_log_llm_call)

    result = asyncio.run(
        llm_provider_module.generate_completion(
            prompt="测试",
            model="deepseek-chat",
            record_chat_log=True,
            request_trace_id="trace-3",
            tools=[{"type": "function", "function": {"name": "query"}}],
        )
    )

    assert result.usage is not None
    assert result.usage.input_tokens == 20
    assert result.duration_ms is not None
    assert result.duration_ms > 0

    log_call = log_calls[0]
    assert log_call["usage"] == usage
    assert log_call["duration_ms"] is not None


def test_generate_messages_completion_records_usage_with_duration(
    monkeypatch: Any,
) -> None:
    usage = UnifiedUsageSchema(
        input_tokens=30,
        cached_input_tokens=10,
        output_tokens=15,
        total_tokens=45,
    )
    response = LLMCompletionResultSchema(
        content="messages completion 输出",
        finish_reason="stop",
        usage=usage,
    )
    fake_client = _FakeCompletionClient(response)
    log_calls: list[dict[str, Any]] = []

    async def _fake_log_llm_call(**kwargs: Any) -> None:
        log_calls.append(kwargs)

    monkeypatch.setattr(
        llm_provider_module, "_client_pool", _FakeClientPool(fake_client)
    )
    monkeypatch.setattr(llm_provider_module, "log_llm_call", _fake_log_llm_call)

    result = asyncio.run(
        llm_provider_module.generate_messages_completion(
            messages=[{"role": "user", "content": "你好"}],
            model="deepseek-chat",
            record_chat_log=True,
            request_trace_id="trace-4",
        )
    )

    assert result.usage is not None
    assert result.usage.input_tokens == 30
    assert result.duration_ms is not None
    assert log_calls[0]["usage"] == usage


def test_bounded_log_writer_never_blocks_producer_when_queue_is_full(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    started = threading.Event()
    release = threading.Event()
    written_batches: list[int] = []

    def _slow_write(batch: list[Any]) -> None:
        written_batches.append(len(batch))
        started.set()
        release.wait(timeout=2)

    monkeypatch.setattr(llm_logger_module, "_LOG_QUEUE_MAX_SIZE", 1)
    monkeypatch.setattr(llm_logger_module, "_write_log_batch_sync", _slow_write)
    writer = llm_logger_module._AsyncLogWriter()
    item = llm_logger_module._LogWriteItem(
        log_file=tmp_path / "2026-07-16.jsonl",
        line="{}\n",
        permission_mode="0o700",
    )

    async def _run() -> None:
        assert writer.enqueue(item) is True
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0)
        assert started.is_set()

        started_at = time.monotonic()
        assert writer.enqueue(item) is True
        assert writer.enqueue(item) is False
        assert time.monotonic() - started_at < 0.05
        assert writer.dropped_count == 1

        release.set()
        await writer.flush()
        await writer.shutdown()

    asyncio.run(_run())

    assert written_batches == [1, 1]
