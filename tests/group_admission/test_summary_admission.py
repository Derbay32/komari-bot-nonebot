"""TSK-229 群历史总结与调试总结逐效果准入红基线 (AC-1..AC-8).

每个用例都通过真实业务 seam ``execute_group_summary`` (规划/总结阶段、群历
史平台读取、图片处理) 或 ``komari_debug.reporting`` 的群公开结果 seam 驱动,
把 ``group_admission`` 顶层 ``adjudicate`` 换成三态脚本, 断言受限后下游 Fake
不再执行. 生产尚未接入逐效果准入, 因此相关用例以红态失败.

阶段准入顺序约定: 群历史平台读取 -> planning LLM -> summary LLM -> 图片渲染
-> 群输出; 规划轮数固定为 1, 使每阶段对应一个准入序列下标.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from komari_bot.plugins.agent_run_logger.diagnostic import LLMDiagnosticCollector
from komari_bot.plugins.group_history_summary.config_schema import (
    DynamicConfigSchema,
    LayoutParamsSchema,
)
from komari_bot.plugins.group_history_summary.execution_service import (
    execute_group_summary,
)
from tests.group_admission.chat_admission_support import (
    ScriptedAdjudicate,
    install_scripted_adjudicate,
)

pytestmark = pytest.mark.group_admission_acceptance

PLANNING_MODEL = "deepseek-chat"
SUMMARY_MODEL = "deepseek-chat"
GROUP_ID = "100"


@pytest.fixture(autouse=True)
def _inject_marker_prompt_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """注入 group_history_summary 完整 marker Prompt 快照 (避免 DB 依赖)."""
    import komari_bot.plugins.group_history_summary.planner_service as planner_module
    import komari_bot.plugins.group_history_summary.prompt_template as seam_module
    import komari_bot.plugins.group_history_summary.summarize_service as summarize_module
    from tests.config.prompt_field_contract import prompt_marker_values

    template = prompt_marker_values("group_history_summary")

    async def _marker_template() -> dict[str, str]:
        return dict(template)

    monkeypatch.setattr(seam_module, "get_template", _marker_template)
    monkeypatch.setattr(planner_module, "get_template", _marker_template)
    monkeypatch.setattr(summarize_module, "get_template", _marker_template)


class _FakeLease:
    """假群租约: ``run`` 直接执行操作, ``close`` 记录释放."""

    def __init__(self, manager: _FakeSummaryLockManager, group_id: str) -> None:
        self._manager = manager
        self._group_id = group_id

    async def run(self, operation: Any) -> Any:
        return await operation

    async def close(self) -> None:
        self._manager.released_groups.append(self._group_id)
        self._manager.running.discard(self._group_id)


class _FakeSummaryLockManager:
    """内存假群锁: 复用既有 running/释放语义, 断言不泄漏."""

    def __init__(self) -> None:
        self.running: set[str] = set()
        self.released_groups: list[str] = []

    async def try_acquire(
        self,
        *,
        group_id: str,
        redis_db: int,
        ttl_seconds: int,
    ) -> _FakeLease | None:
        assert redis_db >= 0
        assert ttl_seconds > 0
        if group_id in self.running:
            return None
        self.running.add(group_id)
        return _FakeLease(self, group_id)


@pytest.fixture(autouse=True)
def _use_in_memory_group_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    lock = _FakeSummaryLockManager()
    monkeypatch.setattr(exec_module, "_group_lock_manager", lock)
    return lock


class _FakeBot:
    """OneBot Fake: 仅实现 ``get_group_msg_history`` 平台读取."""

    self_id = "999"

    def __init__(self) -> None:
        self.history_reads: list[Any] = []

    async def call_api(self, api: str, **kwargs: Any) -> dict[str, Any]:
        if api == "get_group_msg_history":
            self.history_reads.append(kwargs.get("group_id"))
            return {"messages": self._history_items()}
        raise AssertionError(f"unexpected api: {api}")  # noqa: TRY003

    @staticmethod
    def _history_items() -> list[dict[str, Any]]:
        return [
            {
                "user_id": "1001",
                "time": 1,
                "message_seq": 1,
                "message_id": "m1",
                "message": [{"type": "text", "data": {"text": "hello"}}],
                "raw_message": "hello",
                "sender": {"nickname": "moe"},
            }
        ]


class _RecordingBot:
    """debug 报告投递用的记录型 OneBot Fake."""

    self_id = "12345"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_api(self, api: str, **kwargs: Any) -> bool:
        self.calls.append((api, dict(kwargs)))
        return True


def _fake_completion(
    content: str = "",
    tool_calls: list[object] | None = None,
    finish_reason: str = "stop",
) -> object:
    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls or [],
        finish_reason=finish_reason,
        usage=None,
        duration_ms=10.0,
        reasoning_content=None,
    )


def _fetch_tool_call() -> object:
    class _Func:
        name = "fetch_recent_group_messages"
        arguments = '{"count": 10, "include_bot_replies": false}'

    return SimpleNamespace(
        id="call-fetch",
        type="function",
        function=_Func(),
        raw_arguments='{"count": 10}',
        parsed_arguments={"count": 10, "include_bot_replies": False},
    )


class _ScriptedLlmProvider:
    """脚本化 LLM provider: 按 phase 区分 planning / summary 调用."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.planning_queue: list[object] = []
        self.summary_queue: list[object] = []

    def plan_final(self) -> None:
        self.planning_queue.append(_fake_completion(content="sao", finish_reason="stop"))

    def summary_result(self, text: str = "今天的总结文字") -> None:
        self.summary_queue.append(
            _fake_completion(content=f"<content>{text}</content>", finish_reason="stop")
        )

    async def generate_messages_completion(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        phase = str(kwargs.get("request_phase", ""))
        if phase == "group_history_summary_final":
            if not self.summary_queue:
                raise AssertionError("summary LLM 被调用: 受限时应不进入总结")  # noqa: TRY003
            return self.summary_queue.pop(0)
        if phase.startswith("group_history_summary_plan_round_"):
            if not self.planning_queue:
                raise AssertionError("planning LLM 被调用: 受限时应不进入规划")  # noqa: TRY003
            return self.planning_queue.pop(0)
        raise AssertionError(f"unexpected provider phase: {phase}")  # noqa: TRY003


def _count_planning(provider: _ScriptedLlmProvider) -> list[dict[str, Any]]:
    return [
        k for k in provider.calls
        if str(k.get("request_phase", "")).startswith("group_history_summary_plan_round_")
    ]


def _count_summary(provider: _ScriptedLlmProvider) -> list[dict[str, Any]]:
    return [
        k for k in provider.calls
        if str(k.get("request_phase", "")) == "group_history_summary_final"
    ]


def _install_stage_fakes(
    monkeypatch: pytest.MonkeyPatch,
    provider: _ScriptedLlmProvider,
    render_calls: list[int],
) -> None:
    """脚本 provider + 图片渲染 Fake 挂到真实 seam 上 (AC-7)."""
    import komari_bot.plugins.group_history_summary.execution_service as exec_mod
    from komari_bot.plugins import llm_provider

    provider_obj = cast("Any", provider)
    monkeypatch.setattr(
        llm_provider, "generate_messages_completion", provider_obj.generate_messages_completion
    )

    def _fake_render(**kwargs: Any) -> SimpleNamespace:
        del kwargs
        render_calls.append(1)
        return SimpleNamespace(
            images_base64=("page-a",),
            truncated=False,
            total_line_count=1,
            rendered_line_count=1,
        )

    monkeypatch.setattr(exec_mod, "render_summary_image_pages_base64", _fake_render)


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
        "summary_planning_round_limit": 1,
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
        "history_min_coverage_ratio": 0.8,
        "layout_params": LayoutParamsSchema(),
    }
    defaults.update(overrides)
    return DynamicConfigSchema(**defaults)  # type: ignore[call-arg]


def _setup_harness(
    monkeypatch: pytest.MonkeyPatch,
    scripted: ScriptedAdjudicate,
) -> SimpleNamespace:
    install_scripted_adjudicate(monkeypatch, scripted)
    bot = _FakeBot()
    provider = _ScriptedLlmProvider()
    # planning 轮 0 返回带 fetch 工具调用的 completion，触发真实群历史平台读取。
    provider.planning_queue.append(
        _fake_completion(tool_calls=[_fetch_tool_call()], finish_reason="tool_calls")
    )
    provider.plan_final()
    provider.summary_result()
    render_calls: list[int] = []
    _install_stage_fakes(monkeypatch, provider, render_calls)
    collector = LLMDiagnosticCollector(request_id="summary-admission")
    return SimpleNamespace(
        bot=bot,
        provider=provider,
        render=render_calls,
        config=_build_config(),
        collector=collector,
    )


async def _execute(bot: _FakeBot, config: DynamicConfigSchema, *, collector: Any) -> tuple[Any, Any]:
    try:
        result = await execute_group_summary(
            bot=cast("Any", bot),
            group_id=GROUP_ID,
            bot_self_id="999",
            user_request="总结最近消息",
            config=config,
            collector=collector,
            history_capability_confirmed=True,
        )
    except Exception as exc:
        return None, exc
    else:
        return result, None


# ---------------------------------------------------------------------------
# AC-1/AC-4: 获准全链路 + 群锁/连接/collector 清理
# ---------------------------------------------------------------------------


async def test_summary_admitted_pipeline_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    _use_in_memory_group_lock: Any,
) -> None:
    """获准后各阶段 Fake 均执行一次, 租约释放, 诊断收尾一次 (绿意图锁定)."""
    scripted = ScriptedAdjudicate("admitted")
    ns = _setup_harness(monkeypatch, scripted)
    lock = cast("Any", _use_in_memory_group_lock)

    import komari_bot.plugins.group_history_summary.execution_service as exec_mod

    real_finalize = exec_mod.agent_run_logger_plugin.finalize_collector
    finalize_calls: list[dict[str, Any]] = []

    async def _wrapped(coll: Any, **kw: Any) -> Any:
        finalize_calls.append(kw)
        return await real_finalize(coll, **kw)

    monkeypatch.setattr(exec_mod.agent_run_logger_plugin, "finalize_collector", _wrapped)

    result, exc = await _execute(ns.bot, ns.config, collector=ns.collector)
    assert exc is None
    assert result is not None and result.image_base64 == "page-a", "获准后应产出图片"
    assert ns.bot.history_reads, "获准后群历史平台读取应发生"
    assert len(_count_planning(ns.provider)) >= 1
    assert len(_count_summary(ns.provider)) >= 1
    assert len(ns.render) == 1
    assert GROUP_ID in lock.released_groups
    assert lock.running == set(), "群锁不得泄漏"
    assert len(finalize_calls) == 1, "诊断收尾最多一次"
    assert ns.collector.status == "success"
    assert scripted.calls != [], "获准路径效果前未调用 adjudicate"


# ---------------------------------------------------------------------------
# AC-2/AC-5: 入口受限则全部阶段不执行, 且不落失败
# ---------------------------------------------------------------------------


async def test_summary_restricted_blocks_all_stages(
    monkeypatch: pytest.MonkeyPatch,
    _use_in_memory_group_lock: Any,
) -> None:
    """永久受限 (入口): 全部下游 Fake 不得执行, 异常不记录为失败 (AC-2/AC-5)."""
    scripted = ScriptedAdjudicate("restricted")
    ns = _setup_harness(monkeypatch, scripted)

    result, ns_exc = await _execute(ns.bot, ns.config, collector=ns.collector)
    assert ns.provider.calls == [], "planning/summary LLM 不得被调用"
    assert ns.bot.history_reads == [], "群历史平台读取不得发生"
    assert ns.render == [], "图片渲染不得发生"
    assert ns.collector.errors == [], "准入拒绝不得记录为失败"
    _ = result or ns_exc  # 结果或终止异常都不影响: 准入拒绝视为正常控制流


# ---------------------------------------------------------------------------
# AC-1 anchor: planning LLM 受限
# ---------------------------------------------------------------------------


async def test_planning_llm_restricted_blocks_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """summary.planning_llm: 规划 LLM 前裁决, 受限时 provider 不调用."""
    scripted = ScriptedAdjudicate("restricted")
    ns = _setup_harness(monkeypatch, scripted)
    await _execute(ns.bot, ns.config, collector=ns.collector)
    assert ns.provider.calls == [], "planning LLM 受限时不得调用 provider"
    assert scripted.calls != [], "planning LLM 效果前未调用 adjudicate"


async def test_history_read_restricted_blocks_platform_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """summary.history_read: 规划获选但平台读取受限时, get_group_msg_history 不发生."""
    scripted = ScriptedAdjudicate("admitted")
    scripted.set_sequence("admitted", "restricted", "restricted", "restricted")
    ns = _setup_harness(monkeypatch, scripted)
    await _execute(ns.bot, ns.config, collector=ns.collector)
    assert len(_count_planning(ns.provider)) >= 1, "规划已获选则应发生 planning"
    assert ns.bot.history_reads == [], "history 平台读取受限时不得发生 get_group_msg_history"
    assert scripted.calls != [], "history_read 效果前未调用裁决"


# ---------------------------------------------------------------------------
# AC-1/AC-3: summary LLM 受限 & 图片渲染受限（已开始阶段可完成）
# ---------------------------------------------------------------------------


async def test_summary_llm_restricted_blocks_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """summary.summary_llm: 规划+读取获选后, summary LLM 受限则不得调用."""
    scripted = ScriptedAdjudicate("admitted")
    scripted.set_sequence("admitted", "admitted", "restricted", "restricted")
    ns = _setup_harness(monkeypatch, scripted)
    await _execute(ns.bot, ns.config, collector=ns.collector)
    assert ns.bot.history_reads, "规划+读取获选则历史应被读取(已开始阶段可完成)"
    assert _count_summary(ns.provider) == [], "summary LLM 受限时不得调用 provider"
    assert ns.render == [], "summary 受限时更后的图片渲染不得发生"
    assert scripted.calls != [], "summary LLM 效果前未调用裁决"


async def test_image_render_restricted_blocks_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """summary.image_render: 规划+读取+总结获选后, 图片渲染受限则不渲染."""
    scripted = ScriptedAdjudicate("admitted")
    scripted.set_sequence("admitted", "admitted", "admitted", "restricted")
    ns = _setup_harness(monkeypatch, scripted)
    await _execute(ns.bot, ns.config, collector=ns.collector)
    assert len(_count_planning(ns.provider)) >= 1
    assert len(_count_summary(ns.provider)) >= 1, "总结获选则 summary LLM 已完成"
    assert ns.render == [], "图片渲染受限时不得渲染"
    assert scripted.calls != [], "image_render 效果前未调用裁决"


# ---------------------------------------------------------------------------
# AC-6: debug 完整私聊诊断（SUPERUSER 运维类别）不受群限制; 群公开结果受目标群 BUSINESS
# ---------------------------------------------------------------------------


async def test_debug_public_restricted_blocks_group_keeps_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from komari_bot.plugins.komari_debug.reporting import (
        build_and_send_diagnostic_report,
    )

    scripted = ScriptedAdjudicate("restricted")
    install_scripted_adjudicate(monkeypatch, scripted)
    bot = _RecordingBot()
    collector = LLMDiagnosticCollector(request_id="debug-summary-1")

    await build_and_send_diagnostic_report(
        bot=cast("Any", bot),
        user_id=999,
        collector=collector,
        result_type="summary",
        succeeded=True,
        public_group_id=int(GROUP_ID),
    )

    group_sends = [c for c in bot.calls if c[0].startswith("send_group_")]
    assert group_sends == [], "群公开结果(public_group_id)受限时不得发送"
    assert any(c[0] == "send_private_forward_msg" for c in bot.calls), (
        "完整私聊诊断仍须投递给 SUPERUSER(运维类别不受群限制)"
    )
    assert scripted.calls != [], "debug 群公开结果前未调用 adjudicate"


# ---------------------------------------------------------------------------
# 强制补项 AC-1: handler 层 per-send 群输出门控（摘要.group_output）
# ---------------------------------------------------------------------------


class _GroupSendingBot:
    """记录型群发送 bot：仅实现 ``send(event, message)``。"""

    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, event: Any, message: Any) -> None:
        del event
        self.sent.append(message)


def _build_output_result(
    *,
    image_base64: str,
    image_pages: tuple[str, ...],
) -> Any:
    """构造一个真实 SummaryExecutionResult（供 handler 输出 seam 使用）。"""
    from komari_bot.plugins.group_history_summary.execution_service import (
        SummaryExecutionResult,
    )
    from komari_bot.plugins.group_history_summary.planner_service import (
        SummaryPlanResult,
    )

    return SummaryExecutionResult(
        summary_text="今天的总结文字",
        filtered_message_count=5,
        plan_result=SummaryPlanResult(
            messages=[],
            tool_result=None,
            planner_note="",
            rounds_used=1,
        ),
        image_base64=image_base64,
        image_pages_base64=image_pages,
        image_truncated=False,
        filter_label="最近消息",
        time_range="08-01 00:00 - 08-02 00:00",
        history_fetch=None,
    )


async def test_handler_group_output_restricted_blocks_per_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """handler 逐条群输出门控：第一条获准发送，第二条受限即丢弃（摘要.group_output）。"""
    from komari_bot.plugins.group_history_summary.__init__ import (
        _send_group_summary_outputs,
    )

    scripted = ScriptedAdjudicate("admitted")
    # 两条图片分段：第一条获准、第二条受限 -> 只发送第一条。
    scripted.set_sequence("admitted", "restricted")
    install_scripted_adjudicate(monkeypatch, scripted)

    bot = _GroupSendingBot()
    event = SimpleNamespace(group_id=int(GROUP_ID))
    result = _build_output_result(
        image_base64="page-a",
        image_pages=("page-a", "page-b"),
    )

    await _send_group_summary_outputs(bot=cast("Any", bot), event=cast("Any", event), result=result)

    assert len(bot.sent) == 1, "per-send 门控应只放行获准的那一条分段"
    assert "page-a" in str(bot.sent[0]), "获准分段应被发送"
    assert "page-b" not in "".join(str(m) for m in bot.sent), (
        "受限分段不得发送（不复活、不退出其余获准分段）"
    )
    assert len(scripted.calls) == 2, "per-send 门控应在每条分段前各裁决一次"
    _, intent_a = scripted.calls[0]
    assert getattr(intent_a, "value", "business") == "business"


async def test_summary_denial_does_not_trigger_failure_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC5: 准入拒绝不进入 GroupTaskFailureNotifier/总结失败通知与 retry。"""
    from komari_bot.onebot.group_failure_notify import GroupTaskFailureNotifier

    scripted = ScriptedAdjudicate("restricted")
    ns = _setup_harness(monkeypatch, scripted)

    async def _fail_notify(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise AssertionError(  # noqa: TRY003
            "准入拒绝不得进入 GroupTaskFailureNotifier/总结失败通知"
        )

    monkeypatch.setattr(
        GroupTaskFailureNotifier,
        "notify",
        _fail_notify,
    )
    # 若失败通知/异常进入 handler 的失败收口，会以 retry 重发；denial 是
    # 正常控制流，不得抛出、不得通知、不得记录失败。
    result, ns_exc = await _execute(ns.bot, ns.config, collector=ns.collector)
    assert ns_exc is None, "准入拒绝是正常控制流: 不得以异常/失败通知收口"
    assert result is not None and not result.image_base64, "受限时不得产出图片"
    assert ns.collector.errors == [], "准入拒绝不得记录为失败"
    assert ns.provider.calls == [], "受限时 provider 不得被调用"
    _ = ns_exc
