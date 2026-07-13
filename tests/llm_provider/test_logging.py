"""LLM Provider 聊天日志开关测试。"""

from __future__ import annotations

import asyncio
import json
from importlib import import_module
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

if TYPE_CHECKING:
    from pathlib import Path

from komari_bot.plugins.llm_provider.base_client import (
    LLMCompletionResultSchema,
    UnifiedUsageSchema,
)
from komari_bot.plugins.llm_provider.config_schema import DynamicConfigSchema

llm_provider_module = import_module("komari_bot.plugins.llm_provider.__init__")
llm_logger_module = import_module("komari_bot.plugins.llm_provider.llm_logger")


class _FakeClient:
    def __init__(self, response: str) -> None:
        self.response = response
        self.closed = False

    async def generate_text(self, **_kwargs: Any) -> str:
        return self.response

    async def generate_text_with_messages(self, **_kwargs: Any) -> str:
        return self.response

    async def close(self) -> None:
        self.closed = True


class _FakeCompletionClient:
    def __init__(self, response: LLMCompletionResultSchema) -> None:
        self.response = response
        self.closed = False

    async def generate_text(self, **_kwargs: Any) -> LLMCompletionResultSchema:
        return self.response

    async def generate_text_with_messages(self, **_kwargs: Any) -> LLMCompletionResultSchema:
        return self.response

    async def close(self) -> None:
        self.closed = True


def test_log_llm_call_keeps_full_payload_and_private_directory(
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
        llm_logger_module.log_llm_call(
            method="generate_text",
            model="deepseek-chat",
            input_data=full_input,
            output="完整 output 原文",
            reasoning_content="完整 reasoning 原文",
            duration_ms=12.345,
        )
    )

    log_dir = tmp_path / "llm_provider"
    [log_file] = list(log_dir.glob("*.jsonl"))
    record = json.loads(log_file.read_text(encoding="utf-8"))

    assert record["input"] == full_input
    assert record["output"] == "完整 output 原文"
    assert record["reasoning_content"] == "完整 reasoning 原文"
    assert log_dir.stat().st_mode & 0o777 == 0o700


def test_dynamic_config_schema_accepts_log_settings() -> None:
    config = DynamicConfigSchema()

    assert config.llm_log_retention_days == 30

    for mode in ("0o700", "0o750", ""):
        assert DynamicConfigSchema(llm_log_dir_permission_mode=mode).llm_log_dir_permission_mode == mode


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
        llm_logger_module.log_llm_call(
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
        chmod_calls.append(mode)
        original_chmod(self, mode)

    monkeypatch.setattr(type(log_dir), "chmod", _count_chmod)

    asyncio.run(
        llm_logger_module.log_llm_call(
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

    def _count_chmod(_self: Path, mode: int) -> None:
        chmod_calls.append(mode)

    monkeypatch.setattr(type(log_dir), "chmod", _count_chmod)

    asyncio.run(
        llm_logger_module.log_llm_call(
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
    monkeypatch.setattr(llm_logger_module, "_get_log_dir_permission_mode", lambda: "bad")
    chmod_calls: list[int] = []

    def _count_chmod(_self: Path, mode: int) -> None:
        chmod_calls.append(mode)

    monkeypatch.setattr(type(log_dir), "chmod", _count_chmod)

    asyncio.run(
        llm_logger_module.log_llm_call(
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

    monkeypatch.setattr(llm_provider_module, "_get_client", lambda: fake_client)
    monkeypatch.setattr(llm_provider_module, "log_llm_call", _fake_log_llm_call)

    result = asyncio.run(
        llm_provider_module.generate_text(
            prompt="这是一段内部总结请求",
            model="deepseek-chat",
        )
    )

    assert result == "普通调用结果"
    assert fake_client.closed is True
    assert log_calls == []


def test_generate_text_with_messages_records_log_for_chat_reply(
    monkeypatch: Any,
) -> None:
    fake_client = _FakeClient("<content>聊天回复</content>")
    log_calls: list[dict[str, Any]] = []

    async def _fake_log_llm_call(**kwargs: Any) -> None:
        log_calls.append(kwargs)

    monkeypatch.setattr(llm_provider_module, "_get_client", lambda: fake_client)
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
    assert fake_client.closed is True
    assert len(log_calls) == 1
    assert log_calls[0]["method"] == "generate_text_with_messages"
    assert log_calls[0]["input_data"]["trace_id"] == "chat-1001"


def test_generate_text_records_full_actual_request_payload(monkeypatch: Any) -> None:
    fake_client = _FakeClient("聊天回复")
    log_calls: list[dict[str, Any]] = []

    async def _fake_log_llm_call(**kwargs: Any) -> None:
        log_calls.append(kwargs)

    async def _fake_search_knowledge(_query: str, *, limit: int) -> list[Any]:
        assert limit == 2
        return [type("Knowledge", (), {"content": "知识库内容"})()]

    monkeypatch.setattr(llm_provider_module, "_get_client", lambda: fake_client)
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
    assert input_data["prompt"] == "完整 prompt"
    assert input_data["system_instruction"] == "系统 知识库内容"
    assert input_data["temperature"] == 1
    assert input_data["max_tokens"] == 128
    assert input_data["response_format"] == {"type": "json_object"}
    assert input_data["enable_knowledge"] is True
    assert input_data["knowledge_query"] == "查询词"
    assert input_data["knowledge_limit"] == 2
    assert input_data["kwargs"] == {"frequency_penalty": 0.2}


def test_generate_messages_completion_records_full_request_and_output(
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

    monkeypatch.setattr(llm_provider_module, "_get_client", lambda: fake_client)
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
    assert fake_client.closed is True
    log_call = log_calls[0]
    input_data = log_call["input_data"]
    assert input_data["messages"] == messages
    assert input_data["tools"] == tools
    assert input_data["tool_choice"] == "auto"
    assert input_data["parallel_tool_calls"] is True
    assert input_data["temperature"] == 0.7
    assert input_data["max_tokens"] == 256
    assert input_data["response_format"] == {"type": "json_object"}
    assert input_data["kwargs"] == {"top_p": 0.9}
    assert json.loads(log_call["output"])["content"] == "完整 completion 输出"
    assert log_call["reasoning_content"] == "完整思考内容"


def test_log_llm_call_records_full_usage_fields(monkeypatch: Any, tmp_path: Path) -> None:
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
        llm_logger_module.log_llm_call(
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
        llm_logger_module.log_llm_call(
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


def test_log_llm_call_without_usage_omits_usage_key(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr(llm_logger_module, "_LOG_DIR", tmp_path / "llm_provider")
    monkeypatch.setattr(llm_logger_module.random, "random", lambda: 1.0)

    asyncio.run(
        llm_logger_module.log_llm_call(
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

    monkeypatch.setattr(llm_provider_module, "_get_client", lambda: fake_client)
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

    monkeypatch.setattr(llm_provider_module, "_get_client", lambda: fake_client)
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
