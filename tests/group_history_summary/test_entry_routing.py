"""群总结入口的鉴权顺序、独占交接与传播路由测试。

TSK-187 起总结入口消费本次分发唯一的判定结果：真实判定、抽签与交接语义由
新判定引擎拥有。本文件只覆盖入口编排（门禁顺序、独占停止、复用同一份结果、
数量提取/范围校验）——真实端到端路由由
``tests/komari_chat/test_jev_entry_handoff.py`` 穿过实际 NoneBot 分发验证。

TSK-188 起判定技术失败经既有共享通知边界提交一次固定群提示（可选私聊诊断）：
本文件覆盖「停止传播先于通知、资格领取、开关/准入复检与安全投影字段」，通知
冷却与真实平台载荷由 ``tests/komari_chat/test_jev_entry_handoff.py`` 验证。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from komari_bot.decision.jev import DecisionResult

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from komari_bot.onebot import GroupTaskFailureNotification


class _Event:
    group_id = 10000
    message_id = 30000

    def __init__(self, text: str = "帮我总结一下今天群里聊了什么") -> None:
        self._text = text

    def get_plaintext(self) -> str:
        return self._text


class _Bot:
    self_id = "20000"

    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send(self, _event: object, message: object) -> None:
        self.sent.append(message)


class _FailureNotifier:
    def __init__(self) -> None:
        self.notifications: list[GroupTaskFailureNotification] = []
        self.block_at_notify: list[object] = []

    async def notify(
        self,
        *,
        bot: object,
        notification: GroupTaskFailureNotification,
    ) -> None:
        del bot
        self.notifications.append(notification)


class _Matcher:
    def __init__(self) -> None:
        self.block = False

    def stop_propagation(self) -> None:
        self.block = True


class _Dispatch:
    """判定载体的入口替身：记录评估/交接，不复制引擎语义。"""

    def __init__(
        self,
        result: DecisionResult,
        *,
        claim: bool = True,
        calls: list[str] | None = None,
        evaluation_error: BaseException | None = None,
        failure_claim: bool = True,
        on_evaluate: Callable[[], None] | None = None,
    ) -> None:
        self.result = result
        self.claim_result = claim
        self.failure_claim = failure_claim
        self.on_evaluate = on_evaluate
        self.calls = calls if calls is not None else []
        self.evaluation_error = evaluation_error
        self.evaluate_calls = 0
        self.claim_routes: list[str] = []

    async def evaluate(
        self,
        _engine: object,
        *,
        summary_candidate: bool,
    ) -> DecisionResult:
        self.calls.append("evaluate")
        assert summary_candidate is True, "总结入口只能以总结候选身份评估"
        self.evaluate_calls += 1
        if self.on_evaluate is not None:
            self.on_evaluate()
        if self.evaluation_error is not None:
            raise self.evaluation_error
        return self.result

    async def claim_handoff(self, route: str) -> bool:
        self.calls.append(f"claim:{route}")
        self.claim_routes.append(route)
        if route == "failure_notification":
            return self.failure_claim
        return self.claim_result


async def _handle(
    summary_module: Any,
    matcher: _Matcher,
    bot: _Bot,
    event: _Event,
    state: dict[Any, Any] | None = None,
) -> None:
    token = summary_module.current_matcher.set(cast("Any", matcher))
    try:
        await summary_module.handle_group_history_summary(
            cast("Any", bot),
            cast("Any", event),
            {} if state is None else state,
        )
    finally:
        summary_module.current_matcher.reset(token)


def _install_config(
    summary_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    **values: Any,
) -> Any:
    config_values = {
        "summary_enabled": True,
        "min_summary_count": 10,
        "max_summary_count": 200,
    }
    config_values.update(values)
    config = SimpleNamespace(**config_values)
    monkeypatch.setattr(
        summary_module,
        "config_manager",
        SimpleNamespace(get=lambda: config),
    )
    return config


def _install_permission(
    summary_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    allowed: bool,
    calls: list[str],
) -> None:
    def _adjudicate(_groups: object) -> object:
        calls.append("permission")
        qualification = (
            summary_module.AdmissionQualification.BUSINESS
            if allowed
            else summary_module.AdmissionQualification.REJECTED
        )
        return SimpleNamespace(qualification=qualification)

    monkeypatch.setattr(summary_module, "adjudicate", _adjudicate)


def _tracked_async_result(
    calls: list[str],
    name: str,
    *,
    result: object,
) -> Callable[..., Awaitable[object]]:
    async def _tracked(*_args: object, **_kwargs: object) -> object:
        calls.append(name)
        return result

    return _tracked


def _summary_result(
    *,
    status: str = "success",
    route: str = "summary",
    reason_code: str = "summary_matched",
) -> DecisionResult:
    return DecisionResult(
        decision_id="decision-1",
        status=status,  # type: ignore[arg-type]
        reason_code=reason_code,  # type: ignore[arg-type]
        route=route,  # type: ignore[arg-type]
        retain_recent=True,
        memory_candidate=None,
        summary_probability=0.91,
    )


def _install_dispatch(
    summary_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    calls: list[str],
    *,
    result: DecisionResult | None = None,
    claim: bool = True,
    evaluation_error: BaseException | None = None,
    engine: object | None = ...,
    failure_claim: bool = True,
    on_evaluate: Callable[[], None] | None = None,
) -> _Dispatch:
    dispatch = _Dispatch(
        result or _summary_result(),
        claim=claim,
        calls=calls,
        evaluation_error=evaluation_error,
        failure_claim=failure_claim,
        on_evaluate=on_evaluate,
    )
    monkeypatch.setattr(
        summary_module,
        "get_decision_engine",
        lambda: object() if engine is ... else engine,
    )
    monkeypatch.setattr(
        summary_module,
        "get_decision_dispatch",
        lambda *_args, **_kwargs: dispatch,
    )
    return dispatch


def _install_failure_notifier(
    summary_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    matcher: _Matcher | None = None,
) -> _FailureNotifier:
    notifier = _FailureNotifier()
    if matcher is not None:
        original_notify = notifier.notify

        async def _notify(
            *, bot: object, notification: GroupTaskFailureNotification
        ) -> None:
            # 停止传播必须先于任何通知 await（TSK-188 固定顺序）。
            notifier.block_at_notify.append(matcher.block)
            await original_notify(bot=bot, notification=notification)

        notifier.notify = _notify  # type: ignore[method-assign]
    monkeypatch.setattr(
        summary_module,
        "_classification_failure_notifier",
        notifier,
        raising=False,
    )
    return notifier


def _install_context_config(
    summary_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    error_notify_enabled: bool = True,
    error: BaseException | None = None,
) -> SimpleNamespace:
    """替身既有 config_manager factory 返回的 komari_context 通知策略资源。"""
    context_config = SimpleNamespace(error_notify_enabled=error_notify_enabled)

    def _get() -> object:
        if error is not None:
            raise error
        return context_config

    monkeypatch.setattr(
        summary_module,
        "_context_config_manager",
        SimpleNamespace(get=_get),
        raising=False,
    )
    return context_config


def _install_effect_admission(
    summary_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    allowed: bool,
) -> dict[str, bool]:
    """可控的通知前业务准入复检（不复制裁决实现）。"""
    flag = {"value": allowed}

    def _admitted(*, group_id: int) -> bool:
        del group_id
        return flag["value"]

    monkeypatch.setattr(summary_module, "effect_business_admitted", _admitted)
    return flag


@pytest.mark.asyncio
async def test_disabled_entry_returns_before_permission_and_remote_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import komari_bot.plugins.group_history_summary as summary_module

    calls: list[str] = []
    _install_config(summary_module, monkeypatch, summary_enabled=False)
    _install_permission(summary_module, monkeypatch, allowed=True, calls=calls)
    monkeypatch.setattr(
        summary_module,
        "check_group_history_supported",
        _tracked_async_result(calls, "capability", result=True),
    )
    dispatch = _install_dispatch(summary_module, monkeypatch, calls)
    matcher = _Matcher()

    await _handle(summary_module, matcher, _Bot(), _Event())

    assert calls == []
    assert dispatch.evaluate_calls == 0
    assert matcher.block is False
    assert summary_module.summary_matcher.block is False


@pytest.mark.asyncio
async def test_denied_entry_returns_before_capability_and_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import komari_bot.plugins.group_history_summary as summary_module

    calls: list[str] = []
    _install_config(summary_module, monkeypatch)
    _install_permission(summary_module, monkeypatch, allowed=False, calls=calls)
    monkeypatch.setattr(
        summary_module,
        "check_group_history_supported",
        _tracked_async_result(calls, "capability", result=True),
    )
    dispatch = _install_dispatch(summary_module, monkeypatch, calls)
    matcher = _Matcher()

    await _handle(summary_module, matcher, _Bot(), _Event())

    assert calls == ["permission"]
    assert dispatch.evaluate_calls == 0
    assert matcher.block is False


@pytest.mark.asyncio
async def test_unsupported_capability_returns_before_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import komari_bot.plugins.group_history_summary as summary_module

    calls: list[str] = []
    _install_config(summary_module, monkeypatch)
    _install_permission(summary_module, monkeypatch, allowed=True, calls=calls)
    monkeypatch.setattr(
        summary_module,
        "check_group_history_supported",
        _tracked_async_result(calls, "capability", result=False),
    )
    dispatch = _install_dispatch(summary_module, monkeypatch, calls)
    matcher = _Matcher()

    await _handle(summary_module, matcher, _Bot(), _Event())

    assert calls == ["permission", "capability"]
    assert dispatch.evaluate_calls == 0
    assert matcher.block is False


@pytest.mark.asyncio
async def test_summary_miss_keeps_propagation_open_for_shared_chat_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import komari_bot.plugins.group_history_summary as summary_module

    calls: list[str] = []
    _install_config(summary_module, monkeypatch)
    _install_permission(summary_module, monkeypatch, allowed=True, calls=calls)
    monkeypatch.setattr(
        summary_module,
        "check_group_history_supported",
        _tracked_async_result(calls, "capability", result=True),
    )
    _install_dispatch(
        summary_module,
        monkeypatch,
        calls,
        result=_summary_result(
            status="success", route="chat", reason_code="classified"
        ),
    )
    monkeypatch.setattr(
        summary_module,
        "execute_group_summary",
        _tracked_async_result(calls, "execute", result=None),
    )
    notifier = _install_failure_notifier(summary_module, monkeypatch)
    matcher = _Matcher()

    await _handle(summary_module, matcher, _Bot(), _Event())

    assert calls == ["permission", "capability", "evaluate"]
    assert matcher.block is False
    assert notifier.notifications == []


@pytest.mark.asyncio
async def test_unexpected_evaluation_failure_stops_both_paths_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """抛出的判定异常没有有效结果：只停止两条业务路径，绝不制造通知授权。"""
    import komari_bot.plugins.group_history_summary as summary_module

    calls: list[str] = []
    _install_config(summary_module, monkeypatch)
    _install_permission(summary_module, monkeypatch, allowed=True, calls=calls)
    monkeypatch.setattr(
        summary_module,
        "check_group_history_supported",
        _tracked_async_result(calls, "capability", result=True),
    )
    dispatch = _install_dispatch(
        summary_module,
        monkeypatch,
        calls,
        evaluation_error=RuntimeError("模拟判定服务失败"),
    )
    notifier = _install_failure_notifier(summary_module, monkeypatch)
    monkeypatch.setattr(
        summary_module,
        "execute_group_summary",
        _tracked_async_result(calls, "execute", result=None),
    )
    matcher = _Matcher()

    await _handle(summary_module, matcher, _Bot(), _Event("总结一下"))

    assert calls == ["permission", "capability", "evaluate"]
    assert dispatch.claim_routes == []
    assert matcher.block is True
    assert notifier.notifications == []
    assert "execute" not in calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason_code",
    [
        "request_timeout",
        "network_error",
        "rate_limited",
        "authentication_error",
        "provider_error",
        "protocol_error",
        "configuration_invalid",
        "prompt_unavailable",
        "input_unavailable",
        "storage_unavailable",
        "internal_error",
        "queue_full",
        "queue_timeout",
    ],
)
async def test_unavailable_summary_judgement_notifies_fixed_group_text_only(
    monkeypatch: pytest.MonkeyPatch,
    reason_code: str,
) -> None:
    """总结技术失败：先停止传播，再领取一次资格并提交固定群提示。"""
    import komari_bot.plugins.group_history_summary as summary_module

    calls: list[str] = []
    _install_config(summary_module, monkeypatch)
    _install_permission(summary_module, monkeypatch, allowed=True, calls=calls)
    _install_context_config(summary_module, monkeypatch, error_notify_enabled=True)
    _install_effect_admission(summary_module, monkeypatch, allowed=True)
    monkeypatch.setattr(
        summary_module,
        "check_group_history_supported",
        _tracked_async_result(calls, "capability", result=True),
    )
    dispatch = _install_dispatch(
        summary_module,
        monkeypatch,
        calls,
        result=_summary_result(status="failed", route="none", reason_code=reason_code),
    )
    matcher = _Matcher()
    notifier = _install_failure_notifier(summary_module, monkeypatch, matcher=matcher)
    monkeypatch.setattr(
        summary_module,
        "execute_group_summary",
        _tracked_async_result(calls, "execute", result=None),
    )
    bot = _Bot()

    await _handle(summary_module, matcher, bot, _Event("总结一下今天聊了什么"))

    assert calls == [
        "permission",
        "capability",
        "evaluate",
        "claim:failure_notification",
    ]
    assert dispatch.claim_routes == ["failure_notification"]
    assert matcher.block is True
    assert notifier.block_at_notify == [True]
    assert len(notifier.notifications) == 1
    notification = notifier.notifications[0]
    assert notification.group_id == 10000
    assert notification.message_id == 30000
    assert notification.group_text == summary_module.CLASSIFICATION_FAILURE_MESSAGE
    assert notification.task_kind == "group_history_summary"
    assert notification.stage == "decision"
    assert notification.reason_code == reason_code
    assert notification.request_trace_id == "decision-1"
    assert notification.summary is None
    assert notification.notify_superusers is True
    assert notification.image_diagnostic is None
    # 群内固定提示由共享通知边界投递，入口自身不发送任何消息。
    assert bot.sent == []
    assert "execute" not in calls


@pytest.mark.asyncio
async def test_cancelled_summary_judgement_stops_without_ordinary_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消/中断不是供应方故障：不领取通知资格、不提交任何通知。"""
    import komari_bot.plugins.group_history_summary as summary_module

    calls: list[str] = []
    _install_config(summary_module, monkeypatch)
    _install_permission(summary_module, monkeypatch, allowed=True, calls=calls)
    monkeypatch.setattr(
        summary_module,
        "check_group_history_supported",
        _tracked_async_result(calls, "capability", result=True),
    )
    dispatch = _install_dispatch(
        summary_module,
        monkeypatch,
        calls,
        result=_summary_result(
            status="cancelled", route="none", reason_code="interrupted"
        ),
    )
    matcher = _Matcher()
    notifier = _install_failure_notifier(summary_module, monkeypatch, matcher=matcher)
    monkeypatch.setattr(
        summary_module,
        "execute_group_summary",
        _tracked_async_result(calls, "execute", result=None),
    )

    await _handle(summary_module, matcher, _Bot(), _Event("总结一下"))

    assert matcher.block is True
    assert dispatch.claim_routes == []
    assert notifier.notifications == []
    assert "execute" not in calls


@pytest.mark.asyncio
@pytest.mark.parametrize("reason_code", ["disabled", "admission_revoked"])
async def test_refused_failure_notification_claim_keeps_engine_eligibility(
    monkeypatch: pytest.MonkeyPatch,
    reason_code: str,
) -> None:
    """引擎拒绝资格（停用/撤权等既有静默终态）时什么都不发。"""
    import komari_bot.plugins.group_history_summary as summary_module

    calls: list[str] = []
    _install_config(summary_module, monkeypatch)
    _install_permission(summary_module, monkeypatch, allowed=True, calls=calls)
    _install_context_config(summary_module, monkeypatch, error_notify_enabled=True)
    _install_effect_admission(summary_module, monkeypatch, allowed=True)
    monkeypatch.setattr(
        summary_module,
        "check_group_history_supported",
        _tracked_async_result(calls, "capability", result=True),
    )
    dispatch = _install_dispatch(
        summary_module,
        monkeypatch,
        calls,
        result=_summary_result(status="failed", route="none", reason_code=reason_code),
        failure_claim=False,
    )
    matcher = _Matcher()
    notifier = _install_failure_notifier(summary_module, monkeypatch, matcher=matcher)

    await _handle(summary_module, matcher, _Bot(), _Event("总结一下"))

    assert dispatch.claim_routes == ["failure_notification"]
    assert notifier.notifications == []
    assert matcher.block is True


@pytest.mark.asyncio
async def test_failure_notification_switch_off_mutes_private_but_keeps_group_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """error_notify_enabled=false 只静默私聊诊断，固定群提示仍然提交。"""
    import komari_bot.plugins.group_history_summary as summary_module

    calls: list[str] = []
    _install_config(summary_module, monkeypatch)
    _install_permission(summary_module, monkeypatch, allowed=True, calls=calls)
    _install_context_config(summary_module, monkeypatch, error_notify_enabled=False)
    _install_effect_admission(summary_module, monkeypatch, allowed=True)
    monkeypatch.setattr(
        summary_module,
        "check_group_history_supported",
        _tracked_async_result(calls, "capability", result=True),
    )
    _install_dispatch(
        summary_module,
        monkeypatch,
        calls,
        result=_summary_result(status="failed", route="none"),
    )
    matcher = _Matcher()
    notifier = _install_failure_notifier(summary_module, monkeypatch, matcher=matcher)

    await _handle(summary_module, matcher, _Bot(), _Event("总结一下"))

    assert len(notifier.notifications) == 1
    assert notifier.notifications[0].notify_superusers is False
    assert (
        notifier.notifications[0].group_text
        == summary_module.CLASSIFICATION_FAILURE_MESSAGE
    )


@pytest.mark.asyncio
async def test_failure_notification_switch_read_failure_only_mutes_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """读取通知开关失败：静默私聊且不打印原始异常，群内固定提示保留。"""
    import komari_bot.plugins.group_history_summary as summary_module

    calls: list[str] = []
    _install_config(summary_module, monkeypatch)
    _install_permission(summary_module, monkeypatch, allowed=True, calls=calls)
    _install_context_config(
        summary_module,
        monkeypatch,
        error=RuntimeError("模拟配置读取失败"),
    )
    _install_effect_admission(summary_module, monkeypatch, allowed=True)
    monkeypatch.setattr(
        summary_module,
        "check_group_history_supported",
        _tracked_async_result(calls, "capability", result=True),
    )
    _install_dispatch(
        summary_module,
        monkeypatch,
        calls,
        result=_summary_result(status="failed", route="none"),
    )
    matcher = _Matcher()
    notifier = _install_failure_notifier(summary_module, monkeypatch, matcher=matcher)

    await _handle(summary_module, matcher, _Bot(), _Event("总结一下"))

    assert len(notifier.notifications) == 1
    assert notifier.notifications[0].notify_superusers is False
    assert (
        notifier.notifications[0].group_text
        == summary_module.CLASSIFICATION_FAILURE_MESSAGE
    )


@pytest.mark.asyncio
async def test_failure_notification_rechecks_summary_switch_before_notify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """判定结束后总结插件被停用：通知在最后复检处被抑制。"""
    import komari_bot.plugins.group_history_summary as summary_module

    calls: list[str] = []
    config = _install_config(summary_module, monkeypatch)
    _install_permission(summary_module, monkeypatch, allowed=True, calls=calls)
    _install_context_config(summary_module, monkeypatch, error_notify_enabled=True)
    _install_effect_admission(summary_module, monkeypatch, allowed=True)
    monkeypatch.setattr(
        summary_module,
        "check_group_history_supported",
        _tracked_async_result(calls, "capability", result=True),
    )
    _install_dispatch(
        summary_module,
        monkeypatch,
        calls,
        result=_summary_result(status="failed", route="none"),
        on_evaluate=lambda: setattr(config, "summary_enabled", False),
    )
    matcher = _Matcher()
    notifier = _install_failure_notifier(summary_module, monkeypatch, matcher=matcher)

    await _handle(summary_module, matcher, _Bot(), _Event("总结一下"))

    assert notifier.notifications == []
    assert matcher.block is True


@pytest.mark.asyncio
async def test_failure_notification_rechecks_effect_admission_before_notify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """判定结束到通知之间观察到的撤权：抑制通知，不留补做路径。"""
    import komari_bot.plugins.group_history_summary as summary_module

    calls: list[str] = []
    _install_config(summary_module, monkeypatch)
    _install_permission(summary_module, monkeypatch, allowed=True, calls=calls)
    _install_context_config(summary_module, monkeypatch, error_notify_enabled=True)
    admission = _install_effect_admission(summary_module, monkeypatch, allowed=True)
    monkeypatch.setattr(
        summary_module,
        "check_group_history_supported",
        _tracked_async_result(calls, "capability", result=True),
    )
    _install_dispatch(
        summary_module,
        monkeypatch,
        calls,
        result=_summary_result(status="failed", route="none"),
        on_evaluate=lambda: admission.update(value=False),
    )
    matcher = _Matcher()
    notifier = _install_failure_notifier(summary_module, monkeypatch, matcher=matcher)

    await _handle(summary_module, matcher, _Bot(), _Event("总结一下"))

    assert notifier.notifications == []
    assert matcher.block is True


@pytest.mark.asyncio
async def test_missing_engine_stops_summary_and_chat_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import komari_bot.plugins.group_history_summary as summary_module

    calls: list[str] = []
    _install_config(summary_module, monkeypatch)
    _install_permission(summary_module, monkeypatch, allowed=True, calls=calls)
    monkeypatch.setattr(
        summary_module,
        "check_group_history_supported",
        _tracked_async_result(calls, "capability", result=True),
    )
    _install_dispatch(summary_module, monkeypatch, calls, engine=None)
    notifier = _install_failure_notifier(summary_module, monkeypatch)
    matcher = _Matcher()

    await _handle(summary_module, matcher, _Bot(), _Event("总结一下"))

    assert calls == ["permission", "capability"]
    assert matcher.block is True
    assert notifier.notifications == []


@pytest.mark.asyncio
async def test_missing_dispatch_carrier_stops_both_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import komari_bot.plugins.group_history_summary as summary_module

    calls: list[str] = []
    _install_config(summary_module, monkeypatch)
    _install_permission(summary_module, monkeypatch, allowed=True, calls=calls)
    monkeypatch.setattr(
        summary_module,
        "check_group_history_supported",
        _tracked_async_result(calls, "capability", result=True),
    )
    dispatch = _install_dispatch(summary_module, monkeypatch, calls)
    monkeypatch.setattr(summary_module, "get_decision_engine", lambda: object())

    def _fail(*_args: object, **_kwargs: object) -> object:
        raise summary_module.DecisionDispatchUnavailableError

    monkeypatch.setattr(summary_module, "get_decision_dispatch", _fail)
    notifier = _install_failure_notifier(summary_module, monkeypatch)
    matcher = _Matcher()

    await _handle(summary_module, matcher, _Bot(), _Event("总结一下"))

    assert dispatch.evaluate_calls == 0
    assert dispatch.claim_routes == []
    assert matcher.block is True
    assert notifier.notifications == []


@pytest.mark.asyncio
async def test_refused_summary_claim_never_falls_through_to_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import komari_bot.plugins.group_history_summary as summary_module

    calls: list[str] = []
    _install_config(summary_module, monkeypatch)
    _install_permission(summary_module, monkeypatch, allowed=True, calls=calls)
    monkeypatch.setattr(
        summary_module,
        "check_group_history_supported",
        _tracked_async_result(calls, "capability", result=True),
    )
    dispatch = _install_dispatch(summary_module, monkeypatch, calls, claim=False)
    monkeypatch.setattr(
        summary_module,
        "execute_group_summary",
        _tracked_async_result(calls, "execute", result=None),
    )
    bot = _Bot()
    matcher = _Matcher()

    await _handle(summary_module, matcher, bot, _Event("总结一下"))

    assert calls == ["permission", "capability", "evaluate", "claim:summary"]
    assert dispatch.claim_routes == ["summary"]
    assert matcher.block is True
    assert bot.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message_text", "expected_count"),
    [
        ("帮我总结一下今天群里聊了什么", None),
        ("总结过去 50 条", 50),
    ],
)
async def test_confirmed_summary_stops_propagation_and_reuses_capability_check(
    monkeypatch: pytest.MonkeyPatch,
    message_text: str,
    expected_count: int | None,
) -> None:
    import komari_bot.plugins.group_history_summary as summary_module

    calls: list[str] = []
    config = _install_config(summary_module, monkeypatch)
    _install_permission(summary_module, monkeypatch, allowed=True, calls=calls)
    monkeypatch.setattr(
        summary_module,
        "check_group_history_supported",
        _tracked_async_result(calls, "capability", result=True),
    )
    dispatch = _install_dispatch(summary_module, monkeypatch, calls)
    matcher = _Matcher()

    async def _execute(**kwargs: object) -> object:
        calls.append("execute")
        assert matcher.block is True
        assert kwargs["config"] is config
        assert kwargs["history_capability_confirmed"] is True
        assert kwargs["requested_count"] == expected_count
        return SimpleNamespace(image_base64="aW1hZ2U=", summary_text="总结正文")

    monkeypatch.setattr(summary_module, "execute_group_summary", _execute)
    bot = _Bot()

    await _handle(summary_module, matcher, bot, _Event(message_text))

    # 数量总结同样先经过判定，命中并成功领取后才执行总结。
    assert calls == ["permission", "capability", "evaluate", "claim:summary", "execute"]
    assert dispatch.claim_routes == ["summary"]
    assert matcher.block is True
    assert len(bot.sent) == 1


@pytest.mark.asyncio
async def test_out_of_range_summary_is_rejected_only_after_successful_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import komari_bot.plugins.group_history_summary as summary_module

    calls: list[str] = []
    _install_config(summary_module, monkeypatch)
    _install_permission(summary_module, monkeypatch, allowed=True, calls=calls)
    monkeypatch.setattr(
        summary_module,
        "check_group_history_supported",
        _tracked_async_result(calls, "capability", result=True),
    )
    dispatch = _install_dispatch(summary_module, monkeypatch, calls)
    monkeypatch.setattr(
        summary_module,
        "execute_group_summary",
        _tracked_async_result(calls, "execute", result=None),
    )
    sent: list[object] = []

    async def _finish(message: object = None, **_kwargs: object) -> None:
        calls.append("finish")
        sent.append(message)
        raise summary_module.FinishedException

    monkeypatch.setattr(summary_module.summary_matcher, "finish", _finish)
    matcher = _Matcher()

    with pytest.raises(summary_module.FinishedException):
        await _handle(summary_module, matcher, _Bot(), _Event("总结过去 500 条"))

    assert dispatch.claim_routes == ["summary"]
    assert "execute" not in calls
    assert calls[-1] == "finish"
    assert str(sent[0]) == summary_module.OUT_OF_RANGE_MESSAGE


@pytest.mark.asyncio
async def test_incomplete_history_keeps_the_existing_dedicated_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HistoryIncompleteError must still reach the ordinary dedicated notice."""
    import komari_bot.plugins.group_history_summary as summary_module
    from komari_bot.plugins.group_history_summary.history_service import (
        HistoryFetchMetadata,
        HistoryIncompleteError,
    )

    calls: list[str] = []
    _install_config(summary_module, monkeypatch)
    _install_permission(summary_module, monkeypatch, allowed=True, calls=calls)
    monkeypatch.setattr(
        summary_module,
        "check_group_history_supported",
        _tracked_async_result(calls, "capability", result=True),
    )
    _install_dispatch(summary_module, monkeypatch, calls)
    metadata = HistoryFetchMetadata(
        status="partial",
        requested_count=50,
        retrieved_item_count=10,
        missing_count=40,
        completed_batches=1,
        failed_batch=2,
        failure_code="history_api_error",
    )

    async def _incomplete(**_kwargs: object) -> object:
        raise HistoryIncompleteError(metadata)

    monkeypatch.setattr(summary_module, "execute_group_summary", _incomplete)
    sent: list[object] = []

    async def _finish(message: object = None, **_kwargs: object) -> None:
        sent.append(message)
        from nonebot.exception import FinishedException

        raise FinishedException

    monkeypatch.setattr(summary_module.summary_matcher, "finish", _finish)
    matcher = _Matcher()

    from nonebot.exception import FinishedException

    with pytest.raises(FinishedException):
        await _handle(summary_module, matcher, _Bot(), _Event("总结一下今天聊了什么"))

    assert matcher.block is True
    assert len(sent) == 1
    assert str(sent[0]) == "群历史记录没能完整取回，暂时不能可靠地总结……"
