"""execution_service 与诊断追踪测试。"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from typing import Any, cast

import pytest

from komari_bot.plugins.group_history_summary.config_schema import (
    DynamicConfigSchema,
    LayoutParamsSchema,
)
from komari_bot.plugins.group_history_summary.execution_service import (
    CapabilityNotSupportedError,
    SummaryBusyError,
    SummaryServiceUnavailableError,
    execute_group_summary,
)
from komari_bot.plugins.group_history_summary.history_service import (
    HistoryFetchMetadata,
    HistoryMessage,
)
from komari_bot.plugins.llm_provider.diagnostic import (
    LLMDiagnosticCollector,
)

PLANNING_MODEL = "deepseek-chat"
SUMMARY_MODEL = "deepseek-chat"


class _FakeSummaryLease:
    def __init__(self, manager: _FakeSummaryLockManager, group_id: str) -> None:
        self._manager = manager
        self._group_id = group_id

    async def run(self, operation: Any) -> Any:
        return await operation

    async def close(self) -> None:
        self._manager.running_groups.discard(self._group_id)


class _FakeSummaryLockManager:
    def __init__(self) -> None:
        self.running_groups: set[str] = set()

    async def try_acquire(
        self,
        *,
        group_id: str,
        redis_db: int,
        ttl_seconds: int,
    ) -> _FakeSummaryLease | None:
        assert redis_db >= 0
        assert ttl_seconds > 0
        if group_id in self.running_groups:
            return None
        self.running_groups.add(group_id)
        return _FakeSummaryLease(self, group_id)


@pytest.fixture(autouse=True)
def _use_in_memory_group_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    monkeypatch.setattr(exec_module, "_group_lock_manager", _FakeSummaryLockManager())


async def _async_return_true(*_args: Any, **_kwargs: Any) -> bool:
    return True


async def _async_return_false(*_args: Any, **_kwargs: Any) -> bool:
    return False


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
        "summary_planning_thinking_mode": False,
        "summary_planning_reasoning_effort": "",
        "summary_tool_scan_limit": 300,
        "summary_model": SUMMARY_MODEL,
        "summary_temperature": 0.4,
        "summary_max_tokens": 1200,
        "summary_thinking_mode": False,
        "summary_reasoning_effort": "",
        "assistant_prefill_enabled": False,
        "dsv4_roleplay_instruct_mode": "auto",
        "layout_params": LayoutParamsSchema(),
    }
    defaults.update(overrides)
    return DynamicConfigSchema(**defaults)


def _build_history_message(
    *,
    user_id: str,
    nickname: str,
    content: str,
    timestamp: int,
    message_seq: int,
) -> HistoryMessage:
    return HistoryMessage(
        user_id=user_id,
        nickname=nickname,
        content=content,
        timestamp=timestamp,
        message_seq=message_seq,
        message_id=str(message_seq),
        reply_to_message_id=None,
    )


def _fake_completion(
    content: str = "规划完成",
    tool_calls: list[object] | None = None,
    finish_reason: str = "stop",
    usage: object | None = None,
    duration_ms: float | None = None,
) -> object:
    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls or [],
        finish_reason=finish_reason,
        usage=usage,
        duration_ms=duration_ms,
        reasoning_content=None,
    )


def _fake_tool_call(
    *,
    tool_id: str = "call_1",
    func_name: str = "fetch_recent_group_messages",
    raw_arguments: str = '{"count": 50}',
    parsed_arguments: dict[str, object] | None = None,
) -> object:
    class _Func:
        name = func_name
        arguments = raw_arguments

    return SimpleNamespace(
        id=tool_id,
        type="function",
        function=_Func(),
        raw_arguments=raw_arguments,
        parsed_arguments=parsed_arguments or {"count": 50},
    )


# ======================== 规划轮次追踪测试 ========================


@pytest.mark.asyncio
async def test_plan_rounds_recorded_in_collector(monkeypatch: Any) -> None:
    """验证规划每轮在 collector 中产生 LLMCallTrace 与 ToolExecutionTrace。"""
    import komari_bot.plugins.group_history_summary.planner_service as planner_module

    completions = [
        _fake_completion(
            tool_calls=[_fake_tool_call()],
            finish_reason="tool_calls",
            duration_ms=500.0,
        ),
        _fake_completion(
            tool_calls=[_fake_tool_call(func_name="fetch_messages_by_user", parsed_arguments={"count": 10, "display_name": "阿明"})],
            finish_reason="tool_calls",
            duration_ms=300.0,
        ),
        _fake_completion(content="够了"),
    ]

    call_index = 0
    llm_kwargs: list[dict[str, Any]] = []

    async def _fake_gen(**kwargs: Any) -> object:
        nonlocal call_index
        llm_kwargs.append(kwargs)
        result = completions[min(call_index, len(completions) - 1)]
        call_index += 1
        return result

    async def _fake_fetch(_count: int = 50, **_kwargs: Any) -> list[HistoryMessage]:
        return [_build_history_message(user_id="1001", nickname="test", content="hello", timestamp=1, message_seq=1)]

    monkeypatch.setattr(planner_module.llm_provider, "generate_messages_completion", _fake_gen)
    monkeypatch.setattr(planner_module, "_fetch_history_window", _fake_fetch)

    collector = LLMDiagnosticCollector(request_id="test-trace")

    result = await planner_module.plan_summary_request(
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
        request_trace_id="test-trace",
        collector=collector,
    )

    assert result.messages is not None
    assert len(collector.calls) == 3
    assert collector.calls[0].phase == "group_history_summary_plan_round_0"
    assert collector.calls[1].phase == "group_history_summary_plan_round_1"
    assert collector.calls[2].phase == "group_history_summary_plan_round_2"
    assert collector.calls[0].duration_ms == 500.0
    assert collector.calls[1].duration_ms == 300.0
    assert len(collector.tools) == 2
    assert collector.tools[0].tool_name == "fetch_recent_group_messages"
    assert collector.tools[1].tool_name == "fetch_messages_by_user"
    assert [kwargs["request_trace_id"] for kwargs in llm_kwargs] == [
        "test-trace",
        "test-trace",
        "test-trace",
    ]
    assert [kwargs["request_phase"] for kwargs in llm_kwargs] == [
        "group_history_summary_plan_round_0",
        "group_history_summary_plan_round_1",
        "group_history_summary_plan_round_2",
    ]


@pytest.mark.asyncio
async def test_plan_tool_summary_excludes_message_content(monkeypatch: Any) -> None:
    """验证工具结果摘要不包含消息正文。"""
    import komari_bot.plugins.group_history_summary.planner_service as planner_module

    completions = [
        _fake_completion(
            tool_calls=[_fake_tool_call(parsed_arguments={"count": 20, "include_bot_replies": False})],
            finish_reason="tool_calls",
        ),
        _fake_completion(content="够了"),
    ]
    call_index = 0

    async def _fake_gen(**_kwargs: Any) -> object:
        nonlocal call_index
        result = completions[min(call_index, len(completions) - 1)]
        call_index += 1
        return result

    async def _fake_fetch(_count: int = 50, **_kwargs: Any) -> list[HistoryMessage]:
        return [_build_history_message(user_id="1001", nickname="test", content="秘密内容不应该出现在摘要中", timestamp=1, message_seq=1)]

    monkeypatch.setattr(planner_module.llm_provider, "generate_messages_completion", _fake_gen)
    monkeypatch.setattr(planner_module, "_fetch_history_window", _fake_fetch)

    collector = LLMDiagnosticCollector()

    await planner_module.plan_summary_request(
        bot=cast("Any", SimpleNamespace()),
        group_id="123",
        bot_self_id="999",
        user_request="总结",
        planning_model=PLANNING_MODEL,
        planning_max_tokens=800,
        planning_round_limit=3,
        summary_default_count=50,
        min_summary_count=10,
        max_summary_count=200,
        summary_tool_scan_limit=300,
        fetch_batch_size=50,
        request_trace_id="trace",
        collector=collector,
    )

    assert len(collector.tools) == 1
    result_summary = json.loads(collector.tools[0].result_summary or "{}")
    assert result_summary["source"] == "recent_group_messages"
    assert result_summary["matched_count"] == 1
    assert "filters" in result_summary
    assert "秘密内容" not in str(result_summary)


# ======================== 最终总结追踪测试 ========================


@pytest.mark.asyncio
async def test_final_summary_recorded_in_collector(monkeypatch: Any) -> None:
    """验证最终总结调用在 collector 中产生正确的阶段追踪。"""
    import komari_bot.plugins.group_history_summary.summarize_service as summarize_module

    completion_kwargs: dict[str, Any] = {}

    async def _fake_gen(**kwargs: Any) -> object:
        completion_kwargs.update(kwargs)
        return SimpleNamespace(
            content="<content>今天主要讨论了测试。</content>",
            tool_calls=[],
            finish_reason="stop",
            usage=None,
            duration_ms=250.0,
        )

    monkeypatch.setattr(
        summarize_module.llm_provider,
        "generate_messages_completion",
        _fake_gen,
    )

    collector = LLMDiagnosticCollector()
    history = [_build_history_message(user_id="1001", nickname="test", content="hello", timestamp=1, message_seq=1)]

    text = await summarize_module.summarize_history_messages(
        history_messages=history,
        model=SUMMARY_MODEL,
        temperature=0.4,
        max_tokens=1200,
        request_trace_id="trace-1",
        collector=collector,
    )

    assert text == "今天主要讨论了测试。"
    assert completion_kwargs["request_trace_id"] == "trace-1"
    assert completion_kwargs["request_phase"] == "group_history_summary_final"
    assert len(collector.calls) == 1
    assert collector.calls[0].phase == "group_history_summary_final"
    assert collector.calls[0].parent_call_id == "trace-1"
    assert collector.calls[0].finish_reason == "stop"
    assert collector.calls[0].duration_ms == 250.0


@pytest.mark.asyncio
async def test_final_summary_empty_text_result_recorded(monkeypatch: Any) -> None:
    """验证最终总结返回空 content 时仍记录调用。"""
    import komari_bot.plugins.group_history_summary.summarize_service as summarize_module

    async def _fake_gen(**_kwargs: Any) -> object:
        return SimpleNamespace(
            content="",
            tool_calls=[],
            finish_reason="stop",
            usage=None,
            duration_ms=100.0,
        )

    monkeypatch.setattr(
        summarize_module.llm_provider,
        "generate_messages_completion",
        _fake_gen,
    )

    collector = LLMDiagnosticCollector()
    history = [_build_history_message(user_id="1001", nickname="test", content="hello", timestamp=1, message_seq=1)]

    text = await summarize_module.summarize_history_messages(
        history_messages=history,
        model=SUMMARY_MODEL,
        temperature=0.4,
        max_tokens=1200,
        collector=collector,
    )

    assert "信息较少" in text
    assert len(collector.calls) == 1


@pytest.mark.asyncio
async def test_empty_history_no_llm_call_traced(monkeypatch: Any) -> None:
    """验证空历史不产生 LLM 调用追踪。"""
    import komari_bot.plugins.group_history_summary.summarize_service as summarize_module

    called = False

    async def _fake_noop(**_kwargs: Any) -> object:
        nonlocal called
        called = True
        return SimpleNamespace(content="", tool_calls=[], finish_reason="stop")

    monkeypatch.setattr(
        summarize_module.llm_provider,
        "generate_messages_completion",
        _fake_noop,
    )
    monkeypatch.setattr(
        summarize_module.llm_provider,
        "generate_text_with_messages",
        _fake_noop,
    )

    collector = LLMDiagnosticCollector()
    text = await summarize_module.summarize_history_messages(
        history_messages=[],
        model=SUMMARY_MODEL,
        temperature=0.4,
        max_tokens=1200,
        collector=collector,
    )

    assert "信息较少" in text
    assert not called
    assert len(collector.calls) == 0


# ======================== 阶段聚合测试 ========================


def test_phase_aggregation_correctly_groups_by_phase() -> None:
    """验证按阶段聚合的 token 统计正确。"""
    from komari_bot.plugins.llm_provider.base_client import UnifiedUsageSchema
    from komari_bot.plugins.llm_provider.diagnostic import LLMCallTrace

    collector = LLMDiagnosticCollector()

    collector.add_call(
        LLMCallTrace(
            call_id="a",
            phase="group_history_summary_plan_round_0",
            model="plan",
            finish_reason="tool_calls",
            duration_ms=100.0,
            usage=UnifiedUsageSchema(input_tokens=100, output_tokens=50, total_tokens=150),
        )
    )
    collector.add_call(
        LLMCallTrace(
            call_id="b",
            phase="group_history_summary_plan_round_1",
            model="plan",
            finish_reason="stop",
            duration_ms=50.0,
            usage=UnifiedUsageSchema(input_tokens=200, output_tokens=30, total_tokens=230),
        )
    )
    collector.add_call(
        LLMCallTrace(
            call_id="c",
            phase="group_history_summary_final",
            model="summary",
            finish_reason="stop",
            duration_ms=80.0,
            usage=UnifiedUsageSchema(input_tokens=500, output_tokens=100, total_tokens=600),
        )
    )

    plan_0 = collector.aggregate_phase("group_history_summary_plan_round_0")
    assert plan_0.input_tokens == 100
    assert plan_0.output_tokens == 50
    assert plan_0.call_count == 1

    plan_1 = collector.aggregate_phase("group_history_summary_plan_round_1")
    assert plan_1.input_tokens == 200
    assert plan_1.call_count == 1

    final = collector.aggregate_phase("group_history_summary_final")
    assert final.input_tokens == 500
    assert final.call_count == 1

    unknown = collector.aggregate_phase("nonexistent")
    assert unknown.input_tokens == 0
    assert unknown.call_count == 0

    overall = collector.aggregate_overall()
    assert overall.input_tokens == 800
    assert overall.call_count == 3
    assert overall.input_tokens_complete is True


def test_phase_aggregation_marks_incomplete_when_missing_usage() -> None:
    """验证部分调用缺少 usage 时标注为不完整。"""
    from komari_bot.plugins.llm_provider.diagnostic import LLMCallTrace

    collector = LLMDiagnosticCollector()
    collector.add_call(
        LLMCallTrace(
            call_id="a",
            phase="group_history_summary_plan_round_0",
            model="plan",
            usage=None,
        )
    )
    collector.add_call(
        LLMCallTrace(
            call_id="b",
            phase="group_history_summary_plan_round_0",
            model="plan",
            finish_reason="stop",
        )
    )

    agg = collector.aggregate_phase("group_history_summary_plan_round_0")
    assert agg.input_tokens_complete is False
    assert agg.call_count == 2
    assert agg.input_tokens == 0


# ======================== 锁与能力检查测试 ========================


@pytest.mark.asyncio
async def test_lock_busy_error_on_concurrent_same_group(monkeypatch: Any) -> None:
    """验证同群重复调用返回 SummaryBusyError。"""
    import asyncio

    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    monkeypatch.setattr(
        exec_module, "check_group_history_supported", _async_return_true
    )

    async def _slow_plan(*_args: Any, **_kwargs: Any) -> object:
        await asyncio.sleep(0.05)
        return SimpleNamespace(
            messages=[],
            tool_result=None,
            planner_note="",
            rounds_used=0,
        )

    monkeypatch.setattr(exec_module, "plan_summary_request", _slow_plan)

    config = _build_config()

    async def _run() -> None:
        await execute_group_summary(
            bot=cast("Any", SimpleNamespace()),
            group_id="same_group",
            bot_self_id="999",
            user_request="总结",
            config=config,
        )

    task = asyncio.create_task(_run())
    await asyncio.sleep(0.01)

    with pytest.raises(SummaryBusyError):
        await execute_group_summary(
            bot=cast("Any", SimpleNamespace()),
            group_id="same_group",
            bot_self_id="999",
            user_request="总结",
            config=config,
        )

    await task


@pytest.mark.asyncio
async def test_busy_error_while_first_request_checks_capability(
    monkeypatch: Any,
) -> None:
    """首个请求尚在能力检查时，同群第二个请求也必须立即报忙。"""
    import asyncio

    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    check_started = asyncio.Event()
    release_check = asyncio.Event()

    async def _slow_check(_bot: object) -> bool:
        check_started.set()
        await release_check.wait()
        return True

    async def _empty_plan(*_args: Any, **_kwargs: Any) -> object:
        return SimpleNamespace(
            messages=[],
            tool_result=None,
            planner_note="",
            rounds_used=0,
        )

    monkeypatch.setattr(exec_module, "check_group_history_supported", _slow_check)
    monkeypatch.setattr(exec_module, "plan_summary_request", _empty_plan)
    config = _build_config()

    first = asyncio.create_task(
        execute_group_summary(
            bot=cast("Any", SimpleNamespace()),
            group_id="capability-check-group",
            bot_self_id="999",
            user_request="总结",
            config=config,
        )
    )
    await check_started.wait()
    try:
        with pytest.raises(SummaryBusyError):
            await execute_group_summary(
                bot=cast("Any", SimpleNamespace()),
                group_id="capability-check-group",
                bot_self_id="999",
                user_request="总结",
                config=config,
            )
    finally:
        release_check.set()
        await first


@pytest.mark.asyncio
async def test_capability_not_supported_raises(monkeypatch: Any) -> None:
    """验证能力不支持时抛出 CapabilityNotSupportedError。"""
    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    monkeypatch.setattr(
        exec_module, "check_group_history_supported", _async_return_false
    )

    config = _build_config()

    with pytest.raises(CapabilityNotSupportedError):
        await execute_group_summary(
            bot=cast("Any", SimpleNamespace()),
            group_id="123",
            bot_self_id="999",
            user_request="总结",
            config=config,
        )


@pytest.mark.asyncio
async def test_lock_backend_failure_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """分布式锁后端不可用时不得降级为无锁执行。"""
    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    class _FailingLockManager:
        async def try_acquire(self, **_kwargs: object) -> None:
            raise ConnectionError

    capability_checked = False

    async def _track_capability(_bot: object) -> bool:
        nonlocal capability_checked
        capability_checked = True
        return True

    monkeypatch.setattr(exec_module, "_group_lock_manager", _FailingLockManager())
    monkeypatch.setattr(exec_module, "check_group_history_supported", _track_capability)

    with pytest.raises(SummaryServiceUnavailableError):
        await execute_group_summary(
            bot=cast("Any", SimpleNamespace()),
            group_id="lock-backend-failure",
            bot_self_id="999",
            user_request="总结",
            config=_build_config(),
        )

    assert capability_checked is False


@pytest.mark.asyncio
async def test_confirmed_capability_is_not_checked_twice(monkeypatch: Any) -> None:
    """入口已确认能力时，共享执行服务不再重复调用平台能力接口。"""
    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    async def _fail_capability_check(_bot: object) -> bool:
        raise AssertionError

    async def _empty_plan(*_args: Any, **_kwargs: Any) -> object:
        return SimpleNamespace(
            messages=[],
            tool_result=None,
            planner_note="",
            rounds_used=0,
        )

    monkeypatch.setattr(
        exec_module,
        "check_group_history_supported",
        _fail_capability_check,
    )
    monkeypatch.setattr(exec_module, "plan_summary_request", _empty_plan)

    result = await execute_group_summary(
        bot=cast("Any", SimpleNamespace()),
        group_id="capability-confirmed-group",
        bot_self_id="999",
        user_request="总结",
        config=_build_config(),
        history_capability_confirmed=True,
    )

    assert result.filtered_message_count == 0


# ======================== 正常 handler 端到端测试 ========================


@pytest.mark.asyncio
async def test_normal_handler_returns_image_for_non_empty_history(
    monkeypatch: Any,
) -> None:
    """验证正常执行路径返回图片 base64 且 collector 保留完整 trace。"""
    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    monkeypatch.setattr(
        exec_module, "check_group_history_supported", _async_return_true
    )

    messages = [
        _build_history_message(
            user_id="1001", nickname="阿明", content="hello", timestamp=100, message_seq=1
        ),
        _build_history_message(
            user_id="1002", nickname="小红", content="world", timestamp=200, message_seq=2
        ),
    ]

    async def _fake_plan(*_args: Any, **kwargs: Any) -> object:
        collector = kwargs.get("collector")
        if collector is not None:
            from komari_bot.plugins.llm_provider.base_client import UnifiedUsageSchema
            from komari_bot.plugins.llm_provider.diagnostic import LLMCallTrace

            collector.add_call(
                LLMCallTrace(
                    call_id="plan-1",
                    parent_call_id=kwargs.get("request_trace_id"),
                    phase="group_history_summary_plan_round_0",
                    round_index=0,
                    model=PLANNING_MODEL,
                    finish_reason="stop",
                    duration_ms=300.0,
                    usage=UnifiedUsageSchema(input_tokens=50, output_tokens=10, total_tokens=60),
                )
            )

        return SimpleNamespace(
            messages=messages,
            tool_result=SimpleNamespace(
                source="recent_group_messages",
                matched_count=2,
                messages=messages,
                filters={"count": 50, "include_bot_replies": False},
            ),
            planner_note="规划完成",
            rounds_used=1,
        )

    async def _fake_summarize(*_args: Any, **kwargs: Any) -> str:
        collector = kwargs.get("collector")
        if collector is not None:
            from komari_bot.plugins.llm_provider.base_client import UnifiedUsageSchema
            from komari_bot.plugins.llm_provider.diagnostic import LLMCallTrace

            collector.add_call(
                LLMCallTrace(
                    call_id="summary-1",
                    parent_call_id=kwargs.get("request_trace_id"),
                    phase="group_history_summary_final",
                    round_index=0,
                    model=SUMMARY_MODEL,
                    finish_reason="stop",
                    duration_ms=200.0,
                    usage=UnifiedUsageSchema(input_tokens=200, output_tokens=80, total_tokens=280),
                )
            )
        return "今天讨论了 hello 和 world。"

    monkeypatch.setattr(exec_module, "plan_summary_request", _fake_plan)
    monkeypatch.setattr(exec_module, "summarize_history_messages", _fake_summarize)

    collector = LLMDiagnosticCollector(request_id="e2e-test")
    config = _build_config()

    result = await execute_group_summary(
        bot=cast("Any", SimpleNamespace()),
        group_id="456",
        bot_self_id="999",
        user_request="总结",
        config=config,
        requested_count=50,
        collector=collector,
    )

    assert len(result.summary_text) > 0
    assert result.filtered_message_count == 2
    assert result.filter_label == "recent_group_messages"
    assert len(result.image_base64) > 0
    assert "hello" in result.summary_text or "world" in result.summary_text

    assert len(collector.calls) == 2
    phases = {c.phase for c in collector.calls}
    assert "group_history_summary_plan_round_0" in phases
    assert "group_history_summary_final" in phases

    overall = collector.aggregate_overall()
    assert overall.call_count == 2
    assert overall.input_tokens == 250


# ======================== 空历史与回退测试 ========================


@pytest.mark.asyncio
async def test_empty_history_returns_default_text_and_no_image(
    monkeypatch: Any,
) -> None:
    """验证空历史返回默认文本且不生成图片。"""
    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    monkeypatch.setattr(
        exec_module, "check_group_history_supported", _async_return_true
    )

    async def _fake_plan(*_args: Any, **_kwargs: Any) -> object:
        return SimpleNamespace(
            messages=[],
            tool_result=None,
            planner_note="无可用消息",
            rounds_used=0,
        )

    monkeypatch.setattr(exec_module, "plan_summary_request", _fake_plan)

    config = _build_config()
    collector = LLMDiagnosticCollector()

    result = await execute_group_summary(
        bot=cast("Any", SimpleNamespace()),
        group_id="789",
        bot_self_id="999",
        user_request="总结",
        config=config,
        collector=collector,
    )

    assert "文本记录太少" in result.summary_text
    assert result.filtered_message_count == 0
    assert result.image_base64 == ""
    assert len(collector.calls) == 0


# ======================== 规划回退保留部分 trace ========================


@pytest.mark.asyncio
async def test_planning_fallback_preserves_partial_trace(monkeypatch: Any) -> None:
    """验证规划回退时已完成调用仍保留在 collector 中。"""
    import komari_bot.plugins.group_history_summary.planner_service as planner_module

    completions = [
        _fake_completion(
            tool_calls=[_fake_tool_call()],
            finish_reason="tool_calls",
        ),
    ]
    call_index = 0

    async def _fake_gen(**_kwargs: Any) -> object:
        nonlocal call_index
        result = completions[min(call_index, len(completions) - 1)]
        call_index += 1
        return result

    messages = [_build_history_message(user_id="1001", nickname="test", content="fallback test", timestamp=1, message_seq=1)]

    async def _fake_fetch(_count: int = 50, **_kwargs: Any) -> list[HistoryMessage]:
        return messages

    monkeypatch.setattr(planner_module.llm_provider, "generate_messages_completion", _fake_gen)
    monkeypatch.setattr(planner_module, "_fetch_history_window", _fake_fetch)

    collector = LLMDiagnosticCollector()

    result = await planner_module.plan_summary_request(
        bot=cast("Any", SimpleNamespace()),
        group_id="123",
        bot_self_id="999",
        user_request="总结",
        planning_model=PLANNING_MODEL,
        planning_max_tokens=800,
        planning_round_limit=1,
        summary_default_count=30,
        min_summary_count=10,
        max_summary_count=200,
        summary_tool_scan_limit=300,
        fetch_batch_size=50,
        request_trace_id="fallback-trace",
        collector=collector,
    )

    assert result.tool_result is not None
    assert "上限" in result.planner_note or "回退" in result.planner_note
    # 至少 1 轮 LLM 调用被记录
    assert len(collector.calls) >= 1
    plan_phases = [c.phase for c in collector.calls if "plan_round" in c.phase]
    assert len(plan_phases) >= 1


# ======================== 总结失败保留部分 trace ========================


@pytest.mark.asyncio
async def test_summary_failure_preserves_plan_trace(
    monkeypatch: Any,
) -> None:
    """验证总结失败时规划阶段 trace 仍然保留。"""
    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    monkeypatch.setattr(
        exec_module, "check_group_history_supported", _async_return_true
    )

    messages = [_build_history_message(user_id="1001", nickname="test", content="hello", timestamp=1, message_seq=1)]

    async def _fake_plan(*_args: Any, **kwargs: Any) -> object:
        collector = kwargs.get("collector")
        if collector is not None:
            from komari_bot.plugins.llm_provider.diagnostic import LLMCallTrace

            collector.add_call(
                LLMCallTrace(
                    call_id="plan-1",
                    parent_call_id=kwargs.get("request_trace_id"),
                    phase="group_history_summary_plan_round_0",
                    round_index=0,
                    model=PLANNING_MODEL,
                    finish_reason="tool_calls",
                )
            )
        return SimpleNamespace(
            messages=messages,
            tool_result=SimpleNamespace(
                source="recent_group_messages",
                matched_count=1,
                messages=messages,
                filters={},
            ),
            planner_note="规划完成",
            rounds_used=1,
        )

    async def _fake_summarize(*_args: Any, **kwargs: Any) -> str:
        collector = kwargs.get("collector")
        if collector is not None:
            collector.add_error("summarize", "LLMError", "模拟总结失败")
        raise RuntimeError("模拟总结失败")

    monkeypatch.setattr(exec_module, "plan_summary_request", _fake_plan)
    monkeypatch.setattr(exec_module, "summarize_history_messages", _fake_summarize)

    config = _build_config()
    collector = LLMDiagnosticCollector()

    with pytest.raises(RuntimeError, match="模拟总结失败"):
        await execute_group_summary(
            bot=cast("Any", SimpleNamespace()),
            group_id="999",
            bot_self_id="999",
            user_request="总结",
            config=config,
            collector=collector,
        )

    assert len(collector.calls) >= 1
    assert any("plan_round" in c.phase for c in collector.calls)
    assert len(collector.errors) == 1
    assert collector.errors[0]["type"] == "LLMError"


@pytest.mark.asyncio
async def test_image_rendering_runs_outside_event_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PIL 渲染必须在线程中执行，不能阻塞主事件循环。"""
    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    messages = [
        _build_history_message(
            user_id="1001",
            nickname="test",
            content="hello",
            timestamp=1,
            message_seq=1,
        )
    ]

    async def _fake_plan(*_args: Any, **_kwargs: Any) -> object:
        return SimpleNamespace(
            messages=messages,
            tool_result=SimpleNamespace(
                source="recent_group_messages",
                history_fetch=None,
            ),
            planner_note="",
            rounds_used=1,
        )

    async def _fake_summarize(*_args: Any, **_kwargs: Any) -> str:
        return "总结内容"

    render_thread_id = 0

    def _fake_render(**_kwargs: Any) -> str:
        nonlocal render_thread_id
        render_thread_id = threading.get_ident()
        return "image-data"

    monkeypatch.setattr(
        exec_module, "check_group_history_supported", _async_return_true
    )
    monkeypatch.setattr(exec_module, "plan_summary_request", _fake_plan)
    monkeypatch.setattr(exec_module, "summarize_history_messages", _fake_summarize)
    monkeypatch.setattr(exec_module, "render_summary_image_base64", _fake_render)

    event_loop_thread_id = threading.get_ident()
    result = await execute_group_summary(
        bot=cast("Any", SimpleNamespace()),
        group_id="render-thread-group",
        bot_self_id="999",
        user_request="总结",
        config=_build_config(),
    )

    assert result.image_base64 == "image-data"
    assert render_thread_id != event_loop_thread_id


@pytest.mark.asyncio
async def test_allowed_partial_history_is_explicit_in_result_and_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """达到阈值的部分历史可继续总结，但必须明确展示缺页信息。"""
    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    messages = [
        _build_history_message(
            user_id="1001",
            nickname="test",
            content="hello",
            timestamp=1,
            message_seq=1,
        )
    ]
    metadata = HistoryFetchMetadata(
        status="partial",
        requested_count=50,
        retrieved_item_count=40,
        missing_count=10,
        completed_batches=2,
        failed_batch=3,
        failure_code="history_api_error",
    )

    async def _fake_plan(*_args: Any, **_kwargs: Any) -> object:
        return SimpleNamespace(
            messages=messages,
            tool_result=SimpleNamespace(
                source="recent_group_messages",
                history_fetch=metadata,
            ),
            planner_note="",
            rounds_used=1,
        )

    async def _fake_summarize(*_args: Any, **_kwargs: Any) -> str:
        return "总结内容"

    rendered_lines: list[str] = []

    def _fake_render(**kwargs: Any) -> str:
        rendered_lines.extend(kwargs["body_lines"])
        return "image-data"

    monkeypatch.setattr(
        exec_module, "check_group_history_supported", _async_return_true
    )
    monkeypatch.setattr(exec_module, "plan_summary_request", _fake_plan)
    monkeypatch.setattr(exec_module, "summarize_history_messages", _fake_summarize)
    monkeypatch.setattr(exec_module, "render_summary_image_base64", _fake_render)

    result = await execute_group_summary(
        bot=cast("Any", SimpleNamespace()),
        group_id="partial-history-group",
        bot_self_id="999",
        user_request="总结",
        config=_build_config(),
    )

    assert result.history_fetch is metadata
    assert result.summary_text.startswith("⚠ 历史第 3 批读取失败")
    assert any("约缺 10 条记录" in line for line in rendered_lines)
