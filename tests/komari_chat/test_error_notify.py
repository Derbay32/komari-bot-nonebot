"""聊天回复失败通知迁移的用户可观察契约。"""

from __future__ import annotations

import asyncio
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest

from komari_bot.onebot import ImageFailureDiagnostic, image_failure_reason_code

message_handler_module = import_module(
    "komari_bot.plugins.komari_chat.handlers.message_handler"
)
shared_notify_module = import_module("komari_bot.onebot.group_failure_notify")
proactive_reservation_module = import_module(
    "komari_bot.plugins.komari_chat.services.proactive_reservation"
)
ReservationDenied = proactive_reservation_module.ReservationDenied
ReservationDeniedReason = proactive_reservation_module.ReservationDeniedReason

MessageHandler = message_handler_module.MessageHandler
ReplyFailureInfo = message_handler_module.ReplyFailureInfo

GROUP_ERROR_TEXT = "啊、啊呜……对不起，脑袋里刚才突然乱成一团了……"


class _FakeBot:
    """记录群消息与私聊投递，并可注入边界失败。"""

    def __init__(
        self,
        *,
        group_error: BaseException | None = None,
        private_error_user_ids: set[int] | None = None,
    ) -> None:
        self.group_error = group_error
        self.private_error_user_ids = private_error_user_ids or set()
        self.call_api_calls: list[dict[str, object]] = []
        self.send_private_msg_calls: list[dict[str, object]] = []

    async def call_api(self, api: str, **kwargs: object) -> dict[str, object]:
        self.call_api_calls.append({"api": api, **kwargs})
        if self.group_error is not None:
            raise self.group_error
        return {"message_id": 1}

    async def send_private_msg(
        self,
        *,
        user_id: int,
        message: str,
    ) -> None:
        self.send_private_msg_calls.append(
            {"user_id": user_id, "message": message}
        )
        if user_id in self.private_error_user_ids:
            msg = "模拟私聊投递失败"
            raise RuntimeError(msg)


class _FakeEvent:
    group_id = 12345
    message_id = 99999


class _FakeRedis:
    """只模拟 Redis NX 可观察语义，不暴露命令调用细节。"""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self._keys: set[str] = set()

    async def set(
        self,
        key: str,
        _value: str,
        *,
        nx: bool,
        ex: int,
    ) -> bool | None:
        del nx, ex
        if self.error is not None:
            raise self.error
        if key in self._keys:
            return None
        self._keys.add(key)
        return True


class _FakeRedisManager:
    def __init__(self, redis: _FakeRedis) -> None:
        self.redis = redis


def _build_handler(redis: _FakeRedis | None = None) -> MessageHandler:
    handler = MessageHandler.__new__(MessageHandler)
    handler.redis = _FakeRedisManager(redis or _FakeRedis())
    return handler


def _failure(
    *,
    reaction_sent: bool = True,
    reason_code: str = "EmptyReplyError",
    summary: str | None = "模型返回空回复",
    image_diagnostic: ImageFailureDiagnostic | None = None,
) -> object:
    return ReplyFailureInfo(
        stage="generate",
        error_type=reason_code,
        summary=summary,
        request_trace_id="trace-abc",
        reaction_sent=reaction_sent,
        image_diagnostic=image_diagnostic,
    )


def _image_diagnostic() -> ImageFailureDiagnostic:
    return ImageFailureDiagnostic(
        mode="delegated",
        failed_count=1,
        stages=("vision",),
        error_types=("vision_failed",),
    )


def _configure_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool = True,
    superusers: set[str] | None = None,
) -> SimpleNamespace:
    config = SimpleNamespace(
        error_notify_enabled=enabled,
        bot_nickname="小鞠",
        bot_aliases=[],
    )
    monkeypatch.setattr(message_handler_module, "get_memory_config", lambda: config)
    driver = SimpleNamespace(
        config=SimpleNamespace(superusers=superusers or {"10001"})
    )
    monkeypatch.setattr(shared_notify_module, "get_driver", lambda: driver)
    return config


def _assert_group_reply(bot: _FakeBot) -> None:
    assert bot.call_api_calls == [
        {
            "api": "send_group_msg",
            "group_id": 12345,
            "message": [
                {"type": "reply", "data": {"id": "99999"}},
                {"type": "text", "data": {"text": GROUP_ERROR_TEXT}},
            ],
        }
    ]


@pytest.mark.asyncio
async def test_report_reply_failure_uses_shared_observable_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """聊天善后保留群引用文案与 SUPERUSER 安全诊断字段。"""
    _configure_runtime(monkeypatch)
    bot = _FakeBot()
    summary = (
        "模型连接失败 client_secret=hunter2 https://secret.invalid/prompt"
        "\nprompt=不应进入诊断卡"
    )

    await _build_handler().report_reply_failure(
        bot=bot,
        event=_FakeEvent(),
        failure=_failure(summary=summary),
        reason="at",
    )

    _assert_group_reply(bot)
    assert len(bot.send_private_msg_calls) == 1
    card = str(bot.send_private_msg_calls[0]["message"])
    assert "任务:" in card
    assert "群: 12345" in card
    assert "阶段: generate" in card
    assert "原因: EmptyReplyError" in card
    assert "trace: trace-abc" in card
    assert "摘要: 模型连接失败" in card
    assert "hunter2" not in card
    assert "secret.invalid" not in card
    assert "不应进入诊断卡" not in card


@pytest.mark.asyncio
async def test_group_reply_requires_reaction_but_private_card_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未贴生成中表情时不发群提示，但仍上报诊断。"""
    _configure_runtime(monkeypatch)
    bot = _FakeBot()

    await _build_handler().report_reply_failure(
        bot=bot,
        event=_FakeEvent(),
        failure=_failure(reaction_sent=False),
        reason="direct_call",
    )

    assert bot.call_api_calls == []
    assert len(bot.send_private_msg_calls) == 1


@pytest.mark.asyncio
async def test_notification_switch_is_read_at_each_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """动态开关只静默私聊，不影响每次失败对应的群回执。"""
    config = _configure_runtime(monkeypatch, enabled=False)
    handler = _build_handler()
    disabled_bot = _FakeBot()

    await handler.report_reply_failure(
        bot=disabled_bot,
        event=_FakeEvent(),
        failure=_failure(reason_code="DisabledError"),
        reason="at",
    )

    _assert_group_reply(disabled_bot)
    assert disabled_bot.send_private_msg_calls == []

    config.error_notify_enabled = True
    enabled_bot = _FakeBot()
    await handler.report_reply_failure(
        bot=enabled_bot,
        event=_FakeEvent(),
        failure=_failure(reason_code="EnabledError"),
        reason="at",
    )

    _assert_group_reply(enabled_bot)
    assert len(enabled_bot.send_private_msg_calls) == 1


@pytest.mark.asyncio
async def test_config_read_failure_only_mutes_private_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """运行时配置读取失败时仍保留群回执，并静默私聊诊断。"""

    def _raise_config_error() -> None:
        msg = "模拟运行时配置读取失败"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        message_handler_module,
        "get_memory_config",
        _raise_config_error,
    )
    bot = _FakeBot()

    await _build_handler().report_reply_failure(
        bot=bot,
        event=_FakeEvent(),
        failure=_failure(),
        reason="at",
    )

    _assert_group_reply(bot)
    assert bot.send_private_msg_calls == []


@pytest.mark.asyncio
async def test_shared_cooldown_deduplicates_same_reason_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同群同原因五分钟去重，不同原因互不串扰。"""
    _configure_runtime(monkeypatch)
    handler = _build_handler()
    bot = _FakeBot()

    for reason_code in ("TimeoutError", "TimeoutError", "EmptyReplyError"):
        await handler.report_reply_failure(
            bot=bot,
            event=_FakeEvent(),
            failure=_failure(reason_code=reason_code),
            reason="at",
        )

    assert len(bot.call_api_calls) == 3
    assert len(bot.send_private_msg_calls) == 2
    cards = [str(call["message"]) for call in bot.send_private_msg_calls]
    assert "原因: TimeoutError" in cards[0]
    assert "原因: EmptyReplyError" in cards[1]


@pytest.mark.asyncio
async def test_redis_failure_keeps_private_notification_fail_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis 冷却不可用时仍发送 SUPERUSER 私聊。"""
    _configure_runtime(monkeypatch)
    redis_error = RuntimeError("Redis 不可用")
    bot = _FakeBot()

    await _build_handler(_FakeRedis(error=redis_error)).report_reply_failure(
        bot=bot,
        event=_FakeEvent(),
        failure=_failure(),
        reason="at",
    )

    _assert_group_reply(bot)
    assert len(bot.send_private_msg_calls) == 1


@pytest.mark.asyncio
async def test_delivery_failures_do_not_escape_or_stop_other_private_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """群投递和单个私聊失败均不制造第二次业务故障。"""
    _configure_runtime(monkeypatch, superusers={"10001", "10002"})
    bot = _FakeBot(
        group_error=RuntimeError("群投递失败"),
        private_error_user_ids={10001},
    )

    await _build_handler().report_reply_failure(
        bot=bot,
        event=_FakeEvent(),
        failure=_failure(),
        reason="at",
    )

    assert [call["user_id"] for call in bot.send_private_msg_calls] == [10001, 10002]


@pytest.mark.asyncio
async def test_cancelled_error_from_delivery_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任务取消不被聊天善后路径吞掉。"""
    _configure_runtime(monkeypatch)
    bot = _FakeBot(group_error=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await _build_handler().report_reply_failure(
            bot=bot,
            event=_FakeEvent(),
            failure=_failure(),
            reason="at",
        )


@pytest.mark.asyncio
async def test_image_failure_summary_reuses_shared_notifier_with_no_group_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全部失败图片汇总：不先发独立汇总卡再发失败卡——只经共享 notifier
    提交一次 SUPERUSER 图片汇总卡，group_text=None 无群消息；reason_code
    使用稳定的图片 reason_code，stage 与 summary 不进入卡片。"""
    _configure_runtime(monkeypatch)
    bot = _FakeBot()
    diagnostic = _image_diagnostic()

    await _build_handler().report_reply_failure(
        bot=bot,
        event=_FakeEvent(),
        failure=_failure(
            reaction_sent=False,
            reason_code="ImageUnderstandingFailureError",
            summary="图片理解失败（mode=delegated）",
            image_diagnostic=diagnostic,
        ),
        reason="at",
    )

    assert bot.call_api_calls == []
    assert len(bot.send_private_msg_calls) == 1
    card = str(bot.send_private_msg_calls[0]["message"])
    assert "任务: chat_reply" in card
    assert "群: 12345" in card
    assert "trace: trace-abc" in card
    assert "图片模式: delegated" in card
    assert "失败阶段: vision" in card
    assert "失败数量: 1" in card
    assert "失败类型: vision_failed" in card
    # 白名单：不泄漏 message_id / URL / base64 / 正文 / 视觉描述
    assert "99999" not in card
    assert "https://" not in card
    assert "base64" not in card
    assert "data:image" not in card
    assert "一只猫" not in card


@pytest.mark.asyncio
async def test_image_failure_with_reaction_sends_group_apology_and_one_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """贴过表情的图片失败：群内固定错误文本 + 一条图片汇总卡（不是两条）。"""
    _configure_runtime(monkeypatch)
    bot = _FakeBot()

    await _build_handler().report_reply_failure(
        bot=bot,
        event=_FakeEvent(),
        failure=_failure(
            reaction_sent=True,
            reason_code="ImageUnderstandingFailureError",
            summary="图片理解失败（mode=delegated）",
            image_diagnostic=_image_diagnostic(),
        ),
        reason="at",
    )

    _assert_group_reply(bot)
    assert len(bot.send_private_msg_calls) == 1
    card = str(bot.send_private_msg_calls[0]["message"])
    assert "图片模式: delegated" in card
    assert "失败类型: vision_failed" in card


@pytest.mark.asyncio
async def test_image_failure_switch_off_mutes_private_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """error_notify_enabled=false 只静默 SUPERUSER 私聊；贴过表情的群回执仍发。"""
    _configure_runtime(monkeypatch, enabled=False)
    bot = _FakeBot()

    await _build_handler().report_reply_failure(
        bot=bot,
        event=_FakeEvent(),
        failure=_failure(
            reaction_sent=True,
            image_diagnostic=_image_diagnostic(),
        ),
        reason="at",
    )

    _assert_group_reply(bot)
    assert bot.send_private_msg_calls == []


@pytest.mark.asyncio
async def test_image_failure_cooldown_dedupes_same_group_and_reason_across_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一群+同一图片 reason_code 跨任务去重；不同 reason_code 不串扰。"""
    _configure_runtime(monkeypatch)
    handler = _build_handler()
    bot = _FakeBot()
    diagnostic = _image_diagnostic()

    for _ in range(2):
        await handler.report_reply_failure(
            bot=bot,
            event=_FakeEvent(),
            failure=_failure(
                reaction_sent=False,
                image_diagnostic=diagnostic,
            ),
            reason="at",
        )

    assert len(bot.send_private_msg_calls) == 1

    other = ImageFailureDiagnostic(
        mode="native",
        failed_count=2,
        stages=("download", "vision"),
        error_types=("image_unavailable", "vision_failed"),
    )
    await handler.report_reply_failure(
        bot=bot,
        event=_FakeEvent(),
        failure=_failure(
            reaction_sent=False,
            image_diagnostic=other,
        ),
        reason="at",
    )
    assert len(bot.send_private_msg_calls) == 2


@pytest.mark.asyncio
async def test_image_failure_redis_fail_open_keeps_private_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis 冷却不可用仍发送图片汇总私聊。"""
    _configure_runtime(monkeypatch)
    bot = _FakeBot()

    await _build_handler(
        _FakeRedis(error=RuntimeError("Redis 不可用"))
    ).report_reply_failure(
        bot=bot,
        event=_FakeEvent(),
        failure=_failure(
            reaction_sent=False,
            image_diagnostic=_image_diagnostic(),
        ),
        reason="at",
    )

    assert bot.call_api_calls == []
    assert len(bot.send_private_msg_calls) == 1
    card = str(bot.send_private_msg_calls[0]["message"])
    assert "图片模式: delegated" in card


@pytest.mark.asyncio
async def test_process_message_success_with_image_failures_notifies_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功任务带图片失败摘要：process_message 经共享 notifier 提交一次
    group_text=None 的 SUPERUSER 汇总（debug 路径绝不真实通知）。"""
    _configure_runtime(monkeypatch)
    handler = _build_handler()
    handler.memory = SimpleNamespace()
    handler.decision_engine = SimpleNamespace(
        evaluate=_evaluate_should_reply,
    )
    handler.reply_fulfillment = SimpleNamespace(
        is_duplicate_event=_is_duplicate_event
    )

    notifications: list[object] = []

    class _RecordingNotifier:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        async def notify(
            self, *, bot: object, notification: object
        ) -> None:
            del bot
            notifications.append(notification)

    monkeypatch.setattr(
        message_handler_module, "GroupTaskFailureNotifier", _RecordingNotifier
    )
    monkeypatch.setattr(
        message_handler_module,
        "RedisFailureNotificationCooldown",
        lambda _redis: object(),
    )

    diagnostic = _image_diagnostic()
    reply_result = message_handler_module.ReplyResult(
        content="回复内容",
        interaction_history={"event": "看图", "result": "描述", "emotion": "平静"},
        favorability_delta=0,
        favorability_reason="无变化",
        image_diagnostic=diagnostic,
    )
    pending = message_handler_module.PendingReply(
        reply="回复内容",
        reply_to_message_id="99999",
        message=SimpleNamespace(
            user_id="user-1",
            group_id="12345",
            content="看图",
            message_id="99999",
        ),
        reply_result=reply_result,
        force_reply=True,
        bot_nickname="小鞠",
        bot_self_id="bot-1",
        adapter_name="OneBot V11",
        reason="at",
        reply_score=None,
        fulfillment_id="fid-1",
        request_trace_id="trace-img",
        reply_timestamp=1.0,
    )

    async def _fake_attempt_reply(**kwargs: object) -> tuple[object, bool, None]:
        del kwargs
        return pending, False, None

    monkeypatch.setattr(handler, "_attempt_reply", _fake_attempt_reply)

    bot = _FakeBot()
    event = _ProcessEvent()
    result = await handler.process_message(
        bot=bot,
        event=event,
        reply_allowed=True,
    )

    assert result is not None
    assert len(notifications) == 1
    notification = notifications[0]
    assert notification.group_text is None
    assert notification.task_kind == "chat_reply"
    assert notification.reason_code == image_failure_reason_code(diagnostic)
    assert notification.image_diagnostic == diagnostic
    assert notification.notify_superusers is True
    assert notification.group_id == 12345
    assert notification.message_id == 99999


class _ProcessEvent:
    """process_message 所需的最小事件替身（无图片、非 @ 触发）。"""

    user_id = 10000
    group_id = 12345
    message_id = 99999
    self_id = "bot-1"
    to_me = False
    reply = None
    sender = SimpleNamespace(nickname="测试用户", card="")
    message: list[object] = []

    @staticmethod
    def get_plaintext() -> str:
        return "看图"


async def _evaluate_should_reply(**_kwargs: object) -> object:
    from komari_bot.decision import DecisionOutcome, DecisionRuntimeStatus

    return DecisionOutcome(
        memory_action="store",
        should_reply=True,
        force_reply=True,
        reply_reason="at",
        forced_reply_reason="at",
        reply_score=None,
        alias_hit=None,
        call_intent="none",
        call_margin=None,
        best_scene_id=None,
        scene_score=None,
        timing_score=None,
        noise_score=None,
        meaningful_score=None,
        call_direct_score=None,
        call_mention_score=None,
        filter_reason=None,
        timing_breakdown=None,
        runtime_status=DecisionRuntimeStatus.READY,
        runtime_reason="测试运行时已就绪",
    )


async def _is_duplicate_event(_fulfillment_id: str) -> bool:
    return False


@pytest.mark.parametrize("status", ["cooldown", "rate_limited", "duplicate"])
def test_normal_reservation_control_flow_is_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
    status: ReservationDeniedReason,
) -> None:
    """主动回复正常控制状态不产生失败诊断。"""

    class _ReservationService:
        """租约形态 fake：正常频控拒绝返回真实 ReservationDenied 对象。"""

        @staticmethod
        async def reserve(
            group_id: str, reservation_id: str
        ) -> ReservationDenied:
            return ReservationDenied(
                group_id=group_id,
                reservation_id=reservation_id,
                reason=status,
            )

    handler = MessageHandler.__new__(MessageHandler)
    handler.proactive_reservation = _ReservationService()
    monkeypatch.setattr(
        message_handler_module,
        "get_config",
        lambda: SimpleNamespace(proactive_enabled=True),
    )
    monkeypatch.setattr(
        message_handler_module,
        "get_memory_config",
        lambda: SimpleNamespace(),
    )
    message_schema = import_module(
        "komari_bot.plugins.komari_memory"
    ).MessageSchema
    message = message_schema(
        user_id="10000",
        user_nickname="测试用户",
        group_id="12345",
        content="测试消息",
        timestamp=1.0,
        message_id="99999",
    )

    pending, stored, failure = asyncio.run(
        handler._attempt_reply(
            bot_self_id="bot-1",
            adapter_name="OneBot V11",
            message=message,
            reply_to_message_id="99999",
            image_urls=None,
            reply_context=None,
            reply_context_requested=False,
            reply_context_refetched=False,
            force_reply=False,
            reason="score",
            reply_score=0.5,
            store_current=False,
        )
    )

    assert (pending, stored, failure) == (None, False, None)


def test_chat_internal_notification_module_is_removed() -> None:
    """聊天插件不保留旧通知入口、兼容转发或 fallback。"""
    project_root = Path(__file__).resolve().parents[2]
    old_module = (
        project_root
        / "komari_bot"
        / "plugins"
        / "komari_chat"
        / "services"
        / "error_notify.py"
    )
    assert not old_module.exists()

    message_handler_source = (
        project_root
        / "komari_bot"
        / "plugins"
        / "komari_chat"
        / "handlers"
        / "message_handler.py"
    ).read_text(encoding="utf-8")
    plugin_entry_source = (
        project_root / "komari_bot" / "plugins" / "komari_chat" / "__init__.py"
    ).read_text(encoding="utf-8")
    combined_source = message_handler_source + plugin_entry_source
    assert "services.error_notify" not in combined_source
    assert "notify_superusers_reply_failure" not in combined_source
    assert "send_group_reply_error_text" not in combined_source
    assert "one_line_summary" not in combined_source
