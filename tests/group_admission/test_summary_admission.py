"""TSK-229/TSK-206 群历史总结逐效果准入红/绿基线。

每个用例通过真实业务 seam 驱动：``history_tools.fetch_history_window`` 的
平台读取、``DshSummaryService`` 的模型请求准入回调、``execute_group_summary``
的图片渲染，以及 handler 的逐条群输出。受限时下游 Fake 不得执行，且准入
拒绝保持正常静默控制流。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from komari_bot.agent import AgentTaskError
from komari_bot.plugins.group_history_summary.admission_gate import (
    SummaryAdmissionDeniedError,
)
from komari_bot.plugins.group_history_summary.config_schema import (
    DynamicConfigSchema,
    LayoutParamsSchema,
)
from komari_bot.plugins.group_history_summary.dsh_summary_service import (
    DshSummaryService,
)
from komari_bot.plugins.group_history_summary.execution_service import (
    execute_group_summary,
)
from komari_bot.plugins.group_history_summary.history_service import HistoryMessage
from komari_bot.plugins.group_history_summary.history_tools import fetch_history_window
from komari_bot.plugins.group_history_summary.summary_candidate import SummarySelection
from tests.group_admission.chat_admission_support import (
    ScriptedAdjudicate,
    install_scripted_adjudicate,
)

pytestmark = pytest.mark.group_admission_acceptance

GROUP_ID = "100"


class _FakeLease:
    """假群租约：``run`` 直接执行操作, ``close`` 记录释放。"""

    def __init__(self, manager: _FakeSummaryLockManager, group_id: str) -> None:
        self._manager = manager
        self._group_id = group_id

    async def run(self, operation: Any) -> Any:
        return await operation

    async def close(self) -> None:
        self._manager.released_groups.append(self._group_id)
        self._manager.running.discard(self._group_id)


class _FakeSummaryLockManager:
    """内存假群锁：复用既有 running/释放语义, 断言不泄漏。"""

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
    """OneBot Fake：仅实现 ``get_group_msg_history`` 平台读取。"""

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
    """debug 报告投递用的记录型 OneBot Fake。"""

    self_id = "12345"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_api(self, api: str, **kwargs: Any) -> bool:
        self.calls.append((api, dict(kwargs)))
        return True


class _FakeDshSummaryService:
    """可信 DSH 总结服务 Fake：直接返回给定选择，记录是否被调用。"""

    def __init__(self, selection: SummarySelection | None) -> None:
        self.selection = selection
        self.calls: list[dict[str, Any]] = []

    async def generate(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        return SimpleNamespace(
            summary_text="今天的总结文字",
            selection=self.selection,
        )


class _AdmissionDenyingHost:
    """Fake host：仅驱动模型请求准入回调并模拟准入终止。"""

    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self.calls = calls

    async def execute(self, request: Any, callbacks: dict[str, Any]) -> Any:
        self.calls.append({"request": request, "callbacks": callbacks})
        decision = await callbacks["admission"](
            SimpleNamespace(
                name="admission",
                data={"purpose": "conversation", "attempt": 1},
            )
        )
        assert decision == {"allow": False}
        raise AgentTaskError("agent_error")


def _selection(
    messages: list[HistoryMessage] | None = None,
) -> SummarySelection:
    return SummarySelection(
        generation=1,
        source="recent_group_messages",
        matched_count=len(messages or []),
        messages=tuple(messages or []),
        filters={"count": 10},
        history_fetch=None,
    )


def _message(seq: int = 1) -> HistoryMessage:
    return HistoryMessage(
        user_id="1001",
        nickname="moe",
        content="hello",
        timestamp=seq,
        message_seq=seq,
        message_id=f"m{seq}",
        reply_to_message_id=None,
    )


def _build_config(**overrides: Any) -> DynamicConfigSchema:
    defaults: dict[str, Any] = {
        "version": "1.0",
        "plugin_enable": True,
        "min_summary_count": 10,
        "max_summary_count": 200,
        "fetch_batch_size": 50,
        "summary_default_count": 50,
        "summary_planning_round_limit": 1,
        "summary_tool_scan_limit": 300,
        "history_min_coverage_ratio": 0.8,
        "layout_params": LayoutParamsSchema(),
    }
    defaults.update(overrides)
    return DynamicConfigSchema(**defaults)  # type: ignore[call-arg]


async def _execute(
    bot: _FakeBot,
    config: DynamicConfigSchema,
) -> tuple[Any, Any]:
    try:
        result = await execute_group_summary(
            bot=cast("Any", bot),
            group_id=GROUP_ID,
            bot_self_id="999",
            user_request="总结最近消息",
            config=config,
            request_trace_id="summary-admission",
            history_capability_confirmed=True,
        )
    except Exception as exc:
        return None, exc
    else:
        return result, None


# ---------------------------------------------------------------------------
# AC-1/AC-4: 获准全链路 + 群锁释放
# ---------------------------------------------------------------------------


async def test_summary_admitted_pipeline_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    _use_in_memory_group_lock: Any,
) -> None:
    """获准后渲染执行一次, 租约释放 (绿意图锁定)。"""
    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    scripted = ScriptedAdjudicate("admitted")
    install_scripted_adjudicate(monkeypatch, scripted)
    service = _FakeDshSummaryService(_selection([_message()]))
    monkeypatch.setattr(exec_module, "_dsh_summary_service", service)
    render_calls: list[int] = []

    def _fake_render(**_kwargs: Any) -> SimpleNamespace:
        render_calls.append(1)
        return SimpleNamespace(images_base64=("page-a",), truncated=False)

    monkeypatch.setattr(exec_module, "render_summary_image_pages_base64", _fake_render)
    lock = cast("_FakeSummaryLockManager", _use_in_memory_group_lock)

    result, exc = await _execute(_FakeBot(), _build_config())

    assert exc is None
    assert result is not None and result.image_base64 == "page-a"
    assert render_calls == [1]
    assert GROUP_ID in lock.released_groups
    assert lock.running == set()
    assert scripted.calls != []
    assert service.calls != []


# ---------------------------------------------------------------------------
# AC-1 anchor: 平台历史读取受限
# ---------------------------------------------------------------------------


async def test_history_read_restricted_blocks_platform_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """summary.history_read: 平台读取前裁决, 受限时不得 get_group_msg_history。"""
    scripted = ScriptedAdjudicate("restricted")
    install_scripted_adjudicate(monkeypatch, scripted)
    bot = _FakeBot()

    with pytest.raises(SummaryAdmissionDeniedError):
        await fetch_history_window(
            bot=cast("Any", bot),
            group_id=GROUP_ID,
            count=10,
            batch_size=50,
        )

    assert bot.history_reads == []
    assert scripted.calls != []


# ---------------------------------------------------------------------------
# AC-1 anchor: DSH 模型请求受限 = 正常静默控制流
# ---------------------------------------------------------------------------


async def test_model_request_admission_denied_is_normal_control_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """summary.dsh_model_request: 模型请求准入受限时终止且不渲染/不通知。"""
    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    scripted = ScriptedAdjudicate("restricted")
    install_scripted_adjudicate(monkeypatch, scripted)
    service_calls: list[dict[str, Any]] = []
    real_service = DshSummaryService(
        host_getter=lambda: cast("Any", _AdmissionDenyingHost(service_calls)),
    )
    monkeypatch.setattr(exec_module, "_dsh_summary_service", real_service)
    render_calls: list[int] = []

    def _fake_render(**_kwargs: Any) -> SimpleNamespace:
        render_calls.append(1)
        return SimpleNamespace(images_base64=("page-a",), truncated=False)

    monkeypatch.setattr(exec_module, "render_summary_image_pages_base64", _fake_render)

    result, exc = await _execute(_FakeBot(), _build_config())

    assert exc is None
    assert result is not None and result.summary_text == ""
    assert result.image_base64 == ""
    assert render_calls == []
    assert service_calls != []
    assert scripted.calls != []


# ---------------------------------------------------------------------------
# AC-1 anchor: 图片渲染受限
# ---------------------------------------------------------------------------


async def test_image_render_restricted_blocks_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """summary.image_render: DSH 成功后图片渲染受限则不渲染。"""
    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    scripted = ScriptedAdjudicate("restricted")
    install_scripted_adjudicate(monkeypatch, scripted)
    monkeypatch.setattr(
        exec_module,
        "_dsh_summary_service",
        _FakeDshSummaryService(_selection([_message()])),
    )
    render_calls: list[int] = []

    def _fake_render(**_kwargs: Any) -> SimpleNamespace:
        render_calls.append(1)
        return SimpleNamespace(images_base64=("page-a",), truncated=False)

    monkeypatch.setattr(exec_module, "render_summary_image_pages_base64", _fake_render)

    result, exc = await _execute(_FakeBot(), _build_config())

    assert exc is None
    assert result is not None and result.summary_text == ""
    assert render_calls == []
    assert scripted.calls != []


# ---------------------------------------------------------------------------
# AC-5: 准入拒绝不进入失败通知
# ---------------------------------------------------------------------------


async def test_summary_denial_does_not_trigger_failure_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC5: 准入拒绝不进入 GroupTaskFailureNotifier/总结失败通知与 retry。"""
    import komari_bot.plugins.group_history_summary.execution_service as exec_module
    from komari_bot.onebot.group_failure_notify import GroupTaskFailureNotifier

    scripted = ScriptedAdjudicate("restricted")
    install_scripted_adjudicate(monkeypatch, scripted)
    monkeypatch.setattr(
        exec_module,
        "_dsh_summary_service",
        _FakeDshSummaryService(_selection([_message()])),
    )

    async def _fail_notify(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise AssertionError(  # noqa: TRY003
            "准入拒绝不得进入 GroupTaskFailureNotifier/总结失败通知"
        )

    monkeypatch.setattr(GroupTaskFailureNotifier, "notify", _fail_notify)
    result, ns_exc = await _execute(_FakeBot(), _build_config())

    assert ns_exc is None, "准入拒绝是正常控制流: 不得以异常/失败通知收口"
    assert result is not None and not result.image_base64
    assert scripted.calls != []


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

    await build_and_send_diagnostic_report(
        bot=cast("Any", bot),
        user_id=999,
        request_id="debug-summary-1",
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

    return SummaryExecutionResult(
        summary_text="今天的总结文字",
        filtered_message_count=5,
        selection=_selection([_message()]),
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

    await _send_group_summary_outputs(
        bot=cast("Any", bot), event=cast("Any", event), result=result
    )

    assert len(bot.sent) == 1, "per-send 门控应只放行获准的那一条分段"
    assert "page-a" in str(bot.sent[0]), "获准分段应被发送"
    assert "page-b" not in "".join(str(m) for m in bot.sent), (
        "受限分段不得发送（不复活、不退出其余获准分段）"
    )
    assert len(scripted.calls) == 2, "per-send 门控应在每条分段前各裁决一次"
    _, intent_a = scripted.calls[0]
    assert getattr(intent_a, "value", "business") == "business"
