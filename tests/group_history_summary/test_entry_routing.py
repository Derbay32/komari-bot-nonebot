"""群总结入口的鉴权顺序与传播路由测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from komari_bot.decision import (
    SummaryRequestClassificationResult,
    SummaryRequestUnavailableReason,
)
from komari_bot.onebot import GroupTaskFailureNotification

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


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


async def _handle(
    summary_module: Any,
    matcher: _Matcher,
    bot: _Bot,
    event: _Event,
) -> None:
    token = summary_module.current_matcher.set(cast("Any", matcher))
    try:
        await summary_module.handle_group_history_summary(
            cast("Any", bot),
            cast("Any", event),
        )
    finally:
        summary_module.current_matcher.reset(token)


def _install_config(
    summary_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    **values: Any,
) -> Any:
    config_values = {
        "plugin_enable": True,
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


def _install_classifier(
    summary_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    calls: list[str],
    result: SummaryRequestClassificationResult,
) -> None:
    async def _classify(message_text: str) -> SummaryRequestClassificationResult:
        calls.append("classify")
        assert message_text
        return result

    monkeypatch.setattr(
        summary_module,
        "classify_summary_request",
        _classify,
        raising=False,
    )


def _install_failure_notifier(
    summary_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> _FailureNotifier:
    notifier = _FailureNotifier()
    monkeypatch.setattr(
        summary_module,
        "_classification_failure_notifier",
        notifier,
        raising=False,
    )
    return notifier


@pytest.mark.asyncio
async def test_disabled_entry_returns_before_permission_and_remote_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import komari_bot.plugins.group_history_summary as summary_module

    calls: list[str] = []
    _install_config(summary_module, monkeypatch, plugin_enable=False)
    _install_permission(summary_module, monkeypatch, allowed=True, calls=calls)
    monkeypatch.setattr(
        summary_module,
        "check_group_history_supported",
        _tracked_async_result(calls, "capability", result=True),
    )
    _install_classifier(
        summary_module,
        monkeypatch,
        calls,
        SummaryRequestClassificationResult.matched(),
    )
    matcher = _Matcher()

    await _handle(summary_module, matcher, _Bot(), _Event())

    assert calls == []
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
    _install_classifier(
        summary_module,
        monkeypatch,
        calls,
        SummaryRequestClassificationResult.matched(),
    )
    matcher = _Matcher()

    await _handle(summary_module, matcher, _Bot(), _Event())

    assert calls == ["permission"]
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
    _install_classifier(
        summary_module,
        monkeypatch,
        calls,
        SummaryRequestClassificationResult.matched(),
    )
    matcher = _Matcher()

    await _handle(summary_module, matcher, _Bot(), _Event())

    assert calls == ["permission", "capability"]
    assert matcher.block is False


@pytest.mark.asyncio
async def test_non_summary_classification_keeps_propagation_open(
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
    _install_classifier(
        summary_module,
        monkeypatch,
        calls,
        SummaryRequestClassificationResult.not_matched(),
    )
    monkeypatch.setattr(
        summary_module,
        "execute_group_summary",
        _tracked_async_result(calls, "execute", result=None),
    )
    notifier = _install_failure_notifier(summary_module, monkeypatch)
    matcher = _Matcher()

    await _handle(summary_module, matcher, _Bot(), _Event())

    assert calls == ["permission", "capability", "classify"]
    assert matcher.block is False
    assert notifier.notifications == []


@pytest.mark.asyncio
async def test_unexpected_classifier_failure_stops_and_uses_shared_notification(
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

    async def _fail_classification(
        _message_text: str,
    ) -> SummaryRequestClassificationResult:
        calls.append("classify")
        msg = "模拟 scene 服务失败"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        summary_module,
        "classify_summary_request",
        _fail_classification,
        raising=False,
    )
    notifier = _install_failure_notifier(summary_module, monkeypatch)
    matcher = _Matcher()

    await _handle(summary_module, matcher, _Bot(), _Event("总结一下"))

    assert calls == ["permission", "capability", "classify"]
    assert matcher.block is True
    assert len(notifier.notifications) == 1
    notification = notifier.notifications[0]
    assert notification.group_id == 10000
    assert notification.message_id == 30000
    assert notification.task_kind == "group_history_summary"
    assert notification.stage == "scene_classification"
    assert notification.reason_code == "unexpected_error"
    assert notification.notify_superusers is True
    assert notification.request_trace_id is None
    assert notification.summary is None
    assert notification.group_text == summary_module.CLASSIFICATION_FAILURE_MESSAGE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "notification_policy"),
    [
        (SummaryRequestUnavailableReason.DECISION_DISABLED, "silent"),
        (SummaryRequestUnavailableReason.RUNTIME_UNAVAILABLE, "notify"),
        (SummaryRequestUnavailableReason.SCENE_DATA_UNAVAILABLE, "notify"),
        (SummaryRequestUnavailableReason.EMBEDDING_UNAVAILABLE, "notify"),
        (SummaryRequestUnavailableReason.CONFIGURATION_INCOMPLETE, "notify"),
        (SummaryRequestUnavailableReason.RERANK_UNAVAILABLE, "silent"),
        (SummaryRequestUnavailableReason.RERANK_FAILURE_BUDGET_EXHAUSTED, "notify"),
        (SummaryRequestUnavailableReason.FAILURE_BUDGET_UNAVAILABLE, "notify"),
    ],
)
async def test_unavailable_classification_uses_safe_reason_notification_policy(
    monkeypatch: pytest.MonkeyPatch,
    reason: SummaryRequestUnavailableReason,
    notification_policy: str,
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
    _install_classifier(
        summary_module,
        monkeypatch,
        calls,
        SummaryRequestClassificationResult.unavailable(reason),
    )
    notifier = _install_failure_notifier(summary_module, monkeypatch)
    monkeypatch.setattr(
        summary_module,
        "execute_group_summary",
        _tracked_async_result(calls, "execute", result=None),
    )
    matcher = _Matcher()

    await _handle(summary_module, matcher, _Bot(), _Event("总结一下今天聊了什么"))

    assert calls == ["permission", "capability", "classify"]
    assert matcher.block is True
    assert len(notifier.notifications) == 1
    notification = notifier.notifications[0]
    assert notification == GroupTaskFailureNotification(
        group_id=10000,
        message_id=30000,
        group_text=summary_module.CLASSIFICATION_FAILURE_MESSAGE,
        task_kind="group_history_summary",
        stage="scene_classification",
        reason_code=reason.value,
        notify_superusers=notification_policy == "notify",
        request_trace_id=None,
        summary=None,
    )
    forbidden = (
        "scene_group_history_summary",
        "score",
        "threshold",
        "exception",
        "模拟",
    )
    group_text = notification.group_text
    assert group_text is not None
    assert all(value not in group_text for value in forbidden)


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
    _install_classifier(
        summary_module,
        monkeypatch,
        calls,
        SummaryRequestClassificationResult.matched(),
    )
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

    assert calls == ["permission", "capability", "classify", "execute"]
    assert matcher.block is True
    assert len(bot.sent) == 1
