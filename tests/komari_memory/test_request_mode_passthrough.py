"""komari_memory 槽位请求协议透传与 continuation 附着验收测试（issue #23）。

只断言外部可观察行为：发送到假网关的 kwargs 中的 request_api /
stream_enabled，以及多轮工具循环中 continuation 元数据的附着。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
from types import SimpleNamespace
from typing import Any, cast

import nonebot.plugin
import pytest

from komari_bot.plugins.komari_memory.config_schema import KomariMemoryConfigSchema
from komari_bot.plugins.komari_memory.services import llm_service as llm_service_module
from komari_bot.plugins.komari_memory.services.llm_service import (
    summarize_conversation,
)
from komari_bot.plugins.komari_memory.services.redis_manager import MessageSchema


def _make_config(**overrides: Any) -> KomariMemoryConfigSchema:
    base: dict[str, Any] = {
        "bot_nickname": "小鞠知花",
        "llm_model_summary": "summary-model",
        "llm_temperature_summary": 0.3,
        "llm_max_tokens_summary": 2048,
        "llm_request_api_summary": "responses",
        "llm_stream_enabled_summary": True,
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
        is_bot=is_bot,
        message_id="1",
        timestamp=1.0,
    )


async def _run_summarize(**kwargs: Any) -> Any:
    """await 重试装饰器包装后的 summarize_conversation（Awaitable 转协程）。"""
    return await summarize_conversation(**kwargs)


class _RecordingLLMProvider:
    """记录全部调用 kwargs 的假网关。"""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.messages_calls: list[dict[str, Any]] = []
        self.completion_calls: list[dict[str, Any]] = []
        self.text_calls: list[dict[str, Any]] = []

    def _pop(self) -> Any:
        if not self._responses:
            raise AssertionError
        response = self._responses.pop(0)
        if response == "__raise__":
            raise RuntimeError("boom")
        return response

    async def generate_text_with_messages(self, **kwargs: Any) -> str:
        self.messages_calls.append(kwargs)
        return self._pop()

    async def generate_messages_completion(self, **kwargs: Any) -> Any:
        self.completion_calls.append(kwargs)
        return self._pop()

    async def generate_text(self, **kwargs: Any) -> str:
        self.text_calls.append(kwargs)
        return self._pop()


def test_summary_json_mode_passes_summary_slot_request_mode(
    monkeypatch: Any,
) -> None:
    fake_provider = _RecordingLLMProvider(
        [
            json.dumps(
                {"memories": [{"content": "大家约好周末一起吃拉面", "importance": 4}]},
                ensure_ascii=False,
            )
        ]
    )
    monkeypatch.setattr(llm_service_module, "llm_provider", fake_provider)

    asyncio.run(
        _run_summarize(
            messages=[_make_message(content="周末一起吃拉面吧")],
            config=_make_config(),
            participants=["10001"],
            display_name_map={"10001": "阿明"},
        )
    )

    assert len(fake_provider.messages_calls) == 1
    call = fake_provider.messages_calls[0]
    assert call["request_api"] == "responses"
    assert call["stream_enabled"] is True


def test_summary_tool_calling_layer_passes_summary_slot_request_mode(
    monkeypatch: Any,
) -> None:
    tool_completion = SimpleNamespace(
        tool_calls=[
            SimpleNamespace(
                function=SimpleNamespace(name="output_summary_result"),
                parsed_arguments={
                    "memories": [
                        {"content": "工具调用生成了一条大家约好周末一起吃拉面", "importance": 4}
                    ]
                },
            )
        ]
    )
    fake_provider = _RecordingLLMProvider(["__raise__", tool_completion])
    monkeypatch.setattr(llm_service_module, "llm_provider", fake_provider)

    asyncio.run(
        _run_summarize(
            messages=[_make_message(content="周末一起吃拉面吧")],
            config=_make_config(),
            participants=["10001"],
            display_name_map={"10001": "阿明"},
        )
    )

    assert len(fake_provider.completion_calls) == 1
    call = fake_provider.completion_calls[0]
    assert call["request_api"] == "responses"
    assert call["stream_enabled"] is True


def test_summary_direct_output_layer_passes_summary_slot_request_mode(
    monkeypatch: Any,
) -> None:
    direct_payload = json.dumps(
        {"memories": [{"content": "直接输出的大家约好周末一起吃拉面", "importance": 3}]},
        ensure_ascii=False,
    )
    fake_provider = _RecordingLLMProvider(["__raise__", "__raise__", direct_payload])
    monkeypatch.setattr(llm_service_module, "llm_provider", fake_provider)

    asyncio.run(
        _run_summarize(
            messages=[_make_message(content="周末一起吃拉面吧")],
            config=_make_config(),
            participants=["10001"],
            display_name_map={"10001": "阿明"},
        )
    )

    direct_call = fake_provider.messages_calls[-1]
    assert direct_call["request_api"] == "responses"
    assert direct_call["stream_enabled"] is True


# ==================== 互动历史总结 ====================


def test_interaction_event_summary_passes_summary_slot_request_mode(
    monkeypatch: Any,
) -> None:
    from komari_bot.plugins.komari_memory.services import (
        interaction_event_summary_service as summary_module,
    )

    monkeypatch.setattr(
        summary_module,
        "get_config",
        lambda: _make_config(),
    )
    calls: list[dict[str, Any]] = []

    async def _generate_text(**kwargs: Any) -> str:
        calls.append(dict(kwargs))
        return json.dumps(
            {"event_summary": "阶段摘要", "importance": 4}, ensure_ascii=False
        )

    monkeypatch.setattr(
        summary_module,
        "llm_provider",
        SimpleNamespace(generate_text=_generate_text),
    )

    asyncio.run(
        summary_module.summarize_interaction_events(
            user_id="123456",
            display_name="阿明",
            records=[
                {
                    "event": "事件-1",
                    "result": "小鞠认真回应",
                    "emotion": "开心",
                    "display_name": "阿明",
                    "timestamp": 1.0,
                }
            ],
        )
    )

    assert calls
    for call in calls:
        assert call["request_api"] == "responses"
        assert call["stream_enabled"] is True


# ==================== 忘却模糊化 ====================


def test_forgetting_fuzzy_summary_passes_summary_slot_request_mode(
    monkeypatch: Any,
) -> None:
    from komari_bot.plugins.komari_memory.services import (
        forgetting_service as forgetting_service_module,
    )
    from komari_bot.plugins.komari_memory.services.forgetting_service import (
        ForgettingService,
    )

    config = _make_config()
    service = ForgettingService(
        pg_pool=cast("Any", SimpleNamespace()),
        config_provider=lambda: cast("Any", config),
    )
    llm_calls: list[dict[str, Any]] = []

    async def _fake_generate_text(**kwargs: Any) -> str:
        llm_calls.append(dict(kwargs))
        return "<content>模糊后的结果</content>"

    monkeypatch.setattr(
        forgetting_service_module,
        "llm_provider",
        SimpleNamespace(generate_text=_fake_generate_text),
    )

    async def _run_fuzzy() -> str:
        return await service._generate_fuzzy_summary("原始总结内容", 10)

    content = asyncio.run(_run_fuzzy())

    assert content == "模糊后的结果"
    assert llm_calls
    assert llm_calls[0]["request_api"] == "responses"
    assert llm_calls[0]["stream_enabled"] is True


# ==================== 画像 Agent ====================


def _load_profile_agent_service(monkeypatch: Any) -> Any:
    monkeypatch.setattr(
        nonebot.plugin,
        "require",
        lambda name: types.SimpleNamespace(generate_messages_completion=None)
        if name == "llm_provider"
        else object(),
    )
    sys.modules.pop("komari_bot.plugins.komari_memory.agent.profile_agent_service", None)
    return importlib.import_module(
        "komari_bot.plugins.komari_memory.agent.profile_agent_service"
    )


def _tool_call_completion(
    *,
    continuation: Any = None,
) -> Any:
    return SimpleNamespace(
        content="",
        tool_calls=[
            SimpleNamespace(
                id="call_1",
                type="function",
                function=SimpleNamespace(
                    name="count_profile_traits",
                    arguments='{"user_id": "10001"}',
                ),
                raw_arguments='{"user_id": "10001"}',
                parsed_arguments={"user_id": "10001"},
            )
        ],
        finish_reason="tool_calls",
        usage=None,
        duration_ms=None,
        reasoning_content=None,
        continuation=continuation,
    )


def _final_completion() -> Any:
    return SimpleNamespace(
        content="画像维护完成",
        tool_calls=[],
        finish_reason="stop",
        usage=None,
        duration_ms=None,
        reasoning_content=None,
        continuation=None,
    )


def _patch_profile_agent_harness(module: Any, monkeypatch: Any) -> None:
    async def _initial_messages(**_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {"role": "system", "content": "画像工作流规则"},
            {"role": "user", "content": "对话内容"},
        ]

    monkeypatch.setattr(module, "_build_initial_messages", _initial_messages)


def test_profile_agent_passes_summary_slot_request_mode(monkeypatch: Any) -> None:
    module = _load_profile_agent_service(monkeypatch)
    _patch_profile_agent_harness(module, monkeypatch)
    llm_calls: list[dict[str, Any]] = []

    async def _fake_completion(**kwargs: Any) -> Any:
        llm_calls.append(dict(kwargs))
        return _final_completion()

    monkeypatch.setattr(
        module, "llm_provider", SimpleNamespace(generate_messages_completion=_fake_completion)
    )

    class _Staging:
        async def preview(self) -> Any:
            return SimpleNamespace(staged_count=0, diff=[], summary="空")

        async def discard(self) -> None:
            return None

    asyncio.run(
        module._run_profile_agent_locked(
            staging=_Staging(),
            conversation_text="对话",
            participants=["10001"],
            display_name_map={"10001": "阿明"},
            bot_user_ids=set(),
            config=_make_config(),
            trace_id="trace-1",
            collector=None,
        )
    )

    assert llm_calls
    assert llm_calls[0]["request_api"] == "responses"
    assert llm_calls[0]["stream_enabled"] is True


def test_profile_agent_attaches_continuation_to_assistant_message(
    monkeypatch: Any,
) -> None:
    """多轮工具循环：continuation 经统一构造函数附着为 assistant 消息元数据。"""
    from komari_bot.plugins.llm_provider.base_client import (
        CONTINUATION_METADATA_KEY,
        LLMProviderContinuationSchema,
    )

    module = _load_profile_agent_service(monkeypatch)
    _patch_profile_agent_harness(module, monkeypatch)
    completions = [
        _tool_call_completion(
            continuation=LLMProviderContinuationSchema(
                api="responses",
                output_items=[{"type": "reasoning", "id": "rs_1"}],
            )
        ),
        _final_completion(),
    ]
    llm_calls: list[dict[str, Any]] = []

    async def _fake_completion(**kwargs: Any) -> Any:
        llm_calls.append(dict(kwargs))
        return completions.pop(0)

    monkeypatch.setattr(
        module, "llm_provider", SimpleNamespace(generate_messages_completion=_fake_completion)
    )

    class _Staging:
        async def read_profile(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(traits=[])

        async def preview(self) -> Any:
            return SimpleNamespace(staged_count=0, diff=[], summary="空")

        async def discard(self) -> None:
            return None

    asyncio.run(
        module._run_profile_agent_locked(
            staging=_Staging(),
            conversation_text="对话",
            participants=["10001"],
            display_name_map={"10001": "阿明"},
            bot_user_ids=set(),
            config=_make_config(),
            trace_id="trace-1",
            collector=None,
        )
    )

    assert len(llm_calls) == 2
    second_round_messages = llm_calls[1]["messages"]
    assistant_with_continuation = [
        message
        for message in second_round_messages
        if message.get("role") == "assistant"
        and CONTINUATION_METADATA_KEY in message
    ]
    assert len(assistant_with_continuation) == 1
    assert assistant_with_continuation[0][CONTINUATION_METADATA_KEY] == {
        "api": "responses",
        "output_items": [{"type": "reasoning", "id": "rs_1"}],
    }


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
