"""KomariMemory 对话总结 LLM 服务测试。"""

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from komari_bot.plugins.komari_memory.config_schema import KomariMemoryConfigSchema
from komari_bot.plugins.komari_memory.services import llm_service as llm_service_module
from komari_bot.plugins.komari_memory.services.llm_service import summarize_conversation
from komari_bot.plugins.komari_memory.services.redis_manager import MessageSchema


class _FakeLLMProvider:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.messages_calls: list[dict[str, Any]] = []
        self.completion_calls: list[dict[str, Any]] = []
        self.text_calls: list[dict[str, Any]] = []

    async def generate_text_with_messages(self, **kwargs: Any) -> str:
        self.messages_calls.append(kwargs)
        if not self._responses:
            raise AssertionError
        response = self._responses.pop(0)
        if response == "__raise__":
            raise RuntimeError("boom")
        return response

    async def generate_messages_completion(self, **kwargs: Any) -> Any:
        self.completion_calls.append(kwargs)
        return SimpleNamespace(tool_calls=[])

    async def generate_text(self, **kwargs: Any) -> str:
        self.text_calls.append(kwargs)
        if not self._responses:
            raise AssertionError
        return self._responses.pop(0)


class _ToolFakeLLMProvider(_FakeLLMProvider):
    async def generate_messages_completion(self, **kwargs: Any) -> Any:
        self.completion_calls.append(kwargs)
        return SimpleNamespace(
            tool_calls=[
                SimpleNamespace(
                    function=SimpleNamespace(name="output_summary_result"),
                    parsed_arguments={
                        "memories": [
                            {"content": "工具调用生成了一条有效总结记忆", "importance": 4}
                        ]
                    },
                )
            ]
        )


def _make_config(**overrides: Any) -> KomariMemoryConfigSchema:
    base = {
        "bot_nickname": "小鞠知花",
        "llm_model_summary": "summary-model",
        "llm_temperature_summary": 0.3,
        "llm_max_tokens_summary": 2048,
    }
    base.update(overrides)
    return KomariMemoryConfigSchema(**base)


def _make_message(
    *,
    content: str,
    user_id: str = "10001",
    user_nickname: str = "阿明",
    is_bot: bool = False,
) -> MessageSchema:
    return MessageSchema(
        user_id=user_id,
        user_nickname=user_nickname,
        group_id="114514",
        content=content,
        timestamp=1.0,
        message_id=f"msg-{user_id}-{len(content)}",
        is_bot=is_bot,
    )


async def _run_summarize_conversation(
    *,
    messages: list[MessageSchema],
    config: KomariMemoryConfigSchema,
    participants: list[str] | None = None,
    display_name_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    return await summarize_conversation(
        messages=messages,
        config=config,
        participants=participants or ["10001"],
        display_name_map=display_name_map or {"10001": "阿明"},
    )


def test_summarize_conversation_uses_json_mode_messages(monkeypatch: Any) -> None:
    fake_provider = _FakeLLMProvider(
        [
            json.dumps(
                {"memories": [{"content": "大家约好周末一起吃拉面。", "importance": 4}]},
                ensure_ascii=False,
            )
        ]
    )
    monkeypatch.setattr(llm_service_module, "llm_provider", fake_provider)

    result = asyncio.run(
        _run_summarize_conversation(
            messages=[
                _make_message(content="周末一起吃拉面吧"),
                _make_message(content="好呀", user_id="10002", user_nickname="小绿"),
            ],
            config=_make_config(),
            participants=["10001", "10002"],
            display_name_map={"10001": "阿明", "10002": "小绿"},
        )
    )

    assert result == {"memories": [{"content": "大家约好周末一起吃拉面。", "importance": 4}]}
    assert len(fake_provider.messages_calls) == 1
    call = fake_provider.messages_calls[0]
    assert call["response_format"] == {"type": "json_object"}
    assert call["request_phase"] == "summary_json_mode"
    messages = call["messages"]
    assert messages[0]["role"] == "system"
    assert 'source_type="conversation_history"' in messages[1]["content"]
    assert "[user_id:10001] 阿明: 周末一起吃拉面吧" in messages[1]["content"]
    assert "请生成对话总结。" in messages[1]["content"]
    assert "summary_workflow" not in messages[2]["content"]


def test_summarize_conversation_falls_back_to_tool_calling(monkeypatch: Any) -> None:
    fake_provider = _ToolFakeLLMProvider(["不是 JSON"])
    monkeypatch.setattr(llm_service_module, "llm_provider", fake_provider)

    result = asyncio.run(
        _run_summarize_conversation(
            messages=[_make_message(content="今天讨论了知识库维护方式")],
            config=_make_config(),
        )
    )

    assert result["memories"][0]["content"] == "工具调用生成了一条有效总结记忆"
    assert len(fake_provider.messages_calls) == 1
    assert len(fake_provider.completion_calls) == 1
    completion_call = fake_provider.completion_calls[0]
    assert completion_call["tool_choice"] == {
        "type": "function",
        "function": {"name": "output_summary_result"},
    }
    assert completion_call["parallel_tool_calls"] is False


def test_summarize_conversation_falls_back_to_direct_output(monkeypatch: Any) -> None:
    fake_provider = _FakeLLMProvider(
        [
            "不是 JSON",
            '```json\n{"memories": [{"content": "直接输出兜底解析成功的总结记忆", "importance": 5}]}\n```',
        ]
    )
    monkeypatch.setattr(llm_service_module, "llm_provider", fake_provider)

    result = asyncio.run(
        _run_summarize_conversation(
            messages=[_make_message(content="群里决定今晚修复总结逻辑")],
            config=_make_config(),
        )
    )

    assert result == {
        "memories": [{"content": "直接输出兜底解析成功的总结记忆", "importance": 5}]
    }
    assert [call["request_phase"] for call in fake_provider.messages_calls] == [
        "summary_json_mode",
        "summary_direct_output",
    ]


def test_normalize_summary_result_filters_and_limits_memories() -> None:
    normalized = llm_service_module._normalize_summary_result(
        {
            "memories": [
                {"content": "太短", "importance": 5},
                {"content": "这是一条有效的总结记忆", "importance": 9},
                {"content": "这是一条有效的总结记忆", "importance": 1},
                *[
                    {"content": f"额外有效总结记忆第{index}条", "importance": "坏值"}
                    for index in range(10)
                ],
            ],
            "summary": "旧字段应被忽略",
            "user_interaction_operations": [{"旧字段": "应被忽略"}],
        }
    )

    assert len(normalized["memories"]) == 8
    assert normalized["memories"][0] == {"content": "这是一条有效的总结记忆", "importance": 5}
    assert all(set(memory) == {"content", "importance"} for memory in normalized["memories"])


def test_build_summary_messages_keeps_profile_agent_user_prefix(monkeypatch: Any) -> None:
    async def _template() -> dict[str, str]:
        return {
            "memory_summary_common_system": "共用系统提示",
            "summary_workflow_system": "工作流 {{json_response_example}}",
            "json_response_example": '{"memories": []}',
        }

    monkeypatch.setattr(
        llm_service_module,
        "get_summary_template",
        _template,
    )

    messages = asyncio.run(
        llm_service_module._build_summary_messages(
            conversation_text="[user_id:10001] 阿明: 你好",
            participants=["10001"],
            display_name_map={"10001": "阿明"},
        )
    )

    assert messages[0] == {"role": "system", "content": "共用系统提示"}
    assert messages[1]["content"].startswith(
        '<untrusted_context source_type="conversation_history"'
    )
    assert "[user_id:10001] 阿明: 你好" in messages[1]["content"]
    assert '["10001"]' in messages[1]["content"]
    assert messages[1]["content"].endswith("请生成对话总结。")
    assert messages[2] == {"role": "system", "content": '工作流 {"memories": []}'}


def test_build_summary_messages_does_not_truncate_valid_chunk_after_twelve_thousand_chars(
    monkeypatch: Any,
) -> None:
    async def _template() -> dict[str, str]:
        return {
            "memory_summary_common_system": "共用系统提示",
            "summary_workflow_system": "工作流 {{json_response_example}}",
            "json_response_example": '{"memories": []}',
        }

    monkeypatch.setattr(
        llm_service_module,
        "get_summary_template",
        _template,
    )
    tail_canary = "十二千字符后的尾部金丝雀"

    messages = asyncio.run(
        llm_service_module._build_summary_messages(
            conversation_text=f"{'x' * 12_500}{tail_canary}",
            participants=["10001"],
            display_name_map={"10001": "阿明"},
        )
    )

    assert tail_canary in messages[1]["content"]
    assert 'truncated="false"' in messages[1]["content"]


def test_generate_reply_marks_memory_reply_phase(monkeypatch: Any) -> None:
    fake_provider = _FakeLLMProvider(["<content>记忆回复</content>"])
    monkeypatch.setattr(llm_service_module, "llm_provider", fake_provider)

    async def _run() -> str:
        return await llm_service_module.generate_reply(
            config=_make_config(response_tag="content"),
            messages=[{"role": "user", "content": "你好"}],
        )

    result = asyncio.run(_run())

    assert result == "记忆回复"
    assert fake_provider.messages_calls[0]["request_phase"] == "memory_reply"
