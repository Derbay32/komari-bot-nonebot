"""group_history_summary 槽位请求协议透传验收测试（issue #23）。

只断言外部可观察行为：发送到假网关的 kwargs 中的 request_api /
stream_enabled，以及 execute_group_summary 对两个槽位配置的透传。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from komari_bot.plugins.group_history_summary.config_schema import (
    DynamicConfigSchema,
    LayoutParamsSchema,
)
from komari_bot.plugins.group_history_summary.execution_service import (
    execute_group_summary,
)
from komari_bot.plugins.group_history_summary.history_service import HistoryMessage

PLANNING_MODEL = "deepseek-chat"
SUMMARY_MODEL = "deepseek-chat"


@pytest.fixture(autouse=True)
def _use_in_memory_group_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    class _FakeLease:
        def __init__(self, manager: Any, group_id: str) -> None:
            self._manager = manager
            self._group_id = group_id

        async def run(self, operation: Any) -> Any:
            return await operation

        async def close(self) -> None:
            self._manager.running_groups.discard(self._group_id)

    class _FakeLockManager:
        def __init__(self) -> None:
            self.running_groups: set[str] = set()

        async def try_acquire(
            self, *, group_id: str, redis_db: int, ttl_seconds: int
        ) -> Any:
            assert redis_db >= 0
            assert ttl_seconds > 0
            if group_id in self.running_groups:
                return None
            self.running_groups.add(group_id)
            return _FakeLease(self, group_id)

    monkeypatch.setattr(exec_module, "_group_lock_manager", _FakeLockManager())


def _build_config(**overrides: Any) -> DynamicConfigSchema:
    defaults: dict[str, Any] = {
        "version": "1.0",
        "plugin_enable": True,
        "min_summary_count": 10,
        "max_summary_count": 200,
        "fetch_batch_size": 50,
        "summary_default_count": 50,
        "summary_planning_model": PLANNING_MODEL,
        "summary_planning_max_tokens": 800,
        "summary_planning_round_limit": 3,
        "summary_planning_request_api": "responses",
        "summary_planning_stream_enabled": True,
        "summary_tool_scan_limit": 300,
        "summary_model": SUMMARY_MODEL,
        "summary_temperature": 0.4,
        "summary_max_tokens": 1200,
        "summary_request_api": "responses",
        "summary_stream_enabled": True,
        "layout_params": LayoutParamsSchema(),
    }
    defaults.update(overrides)
    return DynamicConfigSchema(**defaults)


def _history_message(content: str = "hello") -> HistoryMessage:
    return HistoryMessage(
        user_id="1001",
        nickname="test",
        content=content,
        timestamp=1,
        message_seq=1,
        message_id="1",
        reply_to_message_id=None,
    )


def _final_completion() -> Any:
    return SimpleNamespace(
        content="<content>今天主要讨论了测试。</content>",
        tool_calls=[],
        finish_reason="stop",
        usage=None,
        duration_ms=100.0,
        reasoning_content=None,
    )


@pytest.mark.asyncio
async def test_planner_passes_planning_slot_request_mode(monkeypatch: Any) -> None:
    import komari_bot.plugins.group_history_summary.planner_service as planner_module

    llm_kwargs: list[dict[str, Any]] = []

    async def _fake_gen(**kwargs: Any) -> object:
        llm_kwargs.append(kwargs)
        return SimpleNamespace(
            content="规划完成",
            tool_calls=[],
            finish_reason="stop",
            usage=None,
            duration_ms=None,
            reasoning_content=None,
        )

    monkeypatch.setattr(
        planner_module.llm_provider, "generate_messages_completion", _fake_gen
    )

    await planner_module.plan_summary_request(
        bot=cast("Any", SimpleNamespace()),
        group_id="123",
        bot_self_id="999",
        user_request="总结最近消息",
        planning_model=PLANNING_MODEL,
        planning_max_tokens=800,
        planning_round_limit=3,
        summary_default_count=50,
        min_summary_count=10,
        max_summary_count=200,
        summary_tool_scan_limit=300,
        fetch_batch_size=50,
        planning_request_api="responses",
        planning_stream_enabled=True,
        request_trace_id="trace",
        collector=None,
    )

    assert llm_kwargs
    assert llm_kwargs[0]["request_api"] == "responses"
    assert llm_kwargs[0]["stream_enabled"] is True


@pytest.mark.asyncio
async def test_summarize_passes_summary_slot_request_mode(monkeypatch: Any) -> None:
    import komari_bot.plugins.group_history_summary.summarize_service as summarize_module

    llm_kwargs: list[dict[str, Any]] = []

    async def _fake_gen(**kwargs: Any) -> object:
        llm_kwargs.append(kwargs)
        return _final_completion()

    async def _fake_gen_text(**kwargs: Any) -> str:
        llm_kwargs.append(kwargs)
        return "<content>今天主要讨论了测试。</content>"

    monkeypatch.setattr(
        summarize_module.llm_provider, "generate_messages_completion", _fake_gen
    )
    # collector=None 时走 str 便捷接口，两路径都必须透传
    monkeypatch.setattr(
        summarize_module.llm_provider, "generate_text_with_messages", _fake_gen_text
    )

    await summarize_module.summarize_history_messages(
        history_messages=[_history_message()],
        model=SUMMARY_MODEL,
        temperature=0.4,
        max_tokens=1200,
        request_api="responses",
        stream_enabled=True,
        request_trace_id="trace",
        collector=None,
    )

    assert llm_kwargs
    assert llm_kwargs[0]["request_api"] == "responses"
    assert llm_kwargs[0]["stream_enabled"] is True


@pytest.mark.asyncio
async def test_execute_group_summary_wires_config_slots_to_llm_calls(
    monkeypatch: Any,
) -> None:
    """execute_group_summary 把规划/执行槽位的协议配置透传到实际 LLM 调用。"""
    import komari_bot.plugins.group_history_summary.planner_service as planner_module

    planning_kwargs: list[dict[str, Any]] = []
    summary_kwargs: list[dict[str, Any]] = []

    def _tool_call_completion() -> Any:
        return SimpleNamespace(
            content="",
            tool_calls=[
                SimpleNamespace(
                    id="call_1",
                    type="function",
                    function=SimpleNamespace(
                        name="fetch_recent_group_messages",
                        arguments='{"count": 50}',
                    ),
                    raw_arguments='{"count": 50}',
                    parsed_arguments={"count": 50},
                )
            ],
            finish_reason="tool_calls",
            usage=None,
            duration_ms=None,
            reasoning_content=None,
        )

    planning_round = 0

    async def _fake_gen(**kwargs: Any) -> object:
        nonlocal planning_round
        phase = str(kwargs.get("request_phase", ""))
        if phase.startswith("group_history_summary_plan_round_"):
            planning_kwargs.append(kwargs)
            planning_round += 1
            if planning_round == 1:
                return _tool_call_completion()
            return SimpleNamespace(
                content="规划完成",
                tool_calls=[],
                finish_reason="stop",
                usage=None,
                duration_ms=None,
                reasoning_content=None,
            )
        summary_kwargs.append(kwargs)
        return _final_completion()

    async def _fake_fetch(_count: int = 50, **_kwargs: Any) -> list[HistoryMessage]:
        return [_history_message()]

    async def _fake_gen_text(**kwargs: Any) -> str:
        summary_kwargs.append(kwargs)
        return "<content>今天主要讨论了测试。</content>"

    # conftest 的 llm_provider 替身是跨插件共享单例，按 request_phase 分发记录；
    # dummy create_collector 返回 None，总结走 str 便捷接口，两个方法都要打桩
    monkeypatch.setattr(
        planner_module.llm_provider, "generate_messages_completion", _fake_gen
    )
    monkeypatch.setattr(
        planner_module.llm_provider, "generate_text_with_messages", _fake_gen_text
    )
    monkeypatch.setattr(planner_module, "_fetch_history_window", _fake_fetch)

    config = _build_config()

    await execute_group_summary(
        bot=cast("Any", SimpleNamespace()),
        group_id="wire-group",
        bot_self_id="999",
        user_request="总结",
        config=config,
        history_capability_confirmed=True,
    )

    assert planning_kwargs
    assert planning_kwargs[0]["request_api"] == "responses"
    assert planning_kwargs[0]["stream_enabled"] is True
    assert summary_kwargs
    assert summary_kwargs[0]["request_api"] == "responses"
    assert summary_kwargs[0]["stream_enabled"] is True


@pytest.mark.asyncio
async def test_planner_attaches_continuation_to_assistant_message(
    monkeypatch: Any,
) -> None:
    """Ticket 07：规划循环 assistant 消息携带 continuation 内部元数据。"""
    import komari_bot.plugins.group_history_summary.planner_service as planner_module
    from komari_bot.plugins.llm_provider.base_client import CONTINUATION_METADATA_KEY

    continuation = SimpleNamespace(
        api="responses",
        output_items=[{"type": "function_call", "call_id": "call_1"}],
        model_dump=lambda: {
            "api": "responses",
            "output_items": [{"type": "function_call", "call_id": "call_1"}],
        },
    )
    llm_kwargs: list[dict[str, Any]] = []
    round_index = 0

    async def _fake_gen(**kwargs: Any) -> object:
        nonlocal round_index
        llm_kwargs.append(kwargs)
        round_index += 1
        if round_index == 1:
            return SimpleNamespace(
                content="",
                tool_calls=[
                    SimpleNamespace(
                        id="call_1",
                        type="function",
                        function=SimpleNamespace(
                            name="fetch_recent_group_messages",
                            arguments='{"count": 50}',
                        ),
                        raw_arguments='{"count": 50}',
                        parsed_arguments={"count": 50},
                    )
                ],
                finish_reason="tool_calls",
                usage=None,
                duration_ms=None,
                reasoning_content=None,
                continuation=continuation,
            )
        return SimpleNamespace(
            content="规划完成",
            tool_calls=[],
            finish_reason="stop",
            usage=None,
            duration_ms=None,
            reasoning_content=None,
        )

    async def _fake_fetch(_count: int = 50, **_kwargs: Any) -> list[HistoryMessage]:
        return [_history_message()]

    monkeypatch.setattr(
        planner_module.llm_provider, "generate_messages_completion", _fake_gen
    )
    monkeypatch.setattr(planner_module, "_fetch_history_window", _fake_fetch)

    await planner_module.plan_summary_request(
        bot=cast("Any", SimpleNamespace()),
        group_id="123",
        bot_self_id="999",
        user_request="总结最近消息",
        planning_model=PLANNING_MODEL,
        planning_max_tokens=800,
        planning_round_limit=3,
        summary_default_count=50,
        min_summary_count=10,
        max_summary_count=200,
        summary_tool_scan_limit=300,
        fetch_batch_size=50,
        planning_request_api="responses",
        planning_stream_enabled=False,
        request_trace_id="trace",
        collector=None,
    )

    assert len(llm_kwargs) == 2
    second_round_messages = llm_kwargs[1]["messages"]
    assistant_messages = [
        message for message in second_round_messages if message["role"] == "assistant"
    ]
    assert len(assistant_messages) == 1
    assert assistant_messages[0][CONTINUATION_METADATA_KEY]["api"] == "responses"
    assert assistant_messages[0][CONTINUATION_METADATA_KEY]["output_items"] == [
        {"type": "function_call", "call_id": "call_1"}
    ]
