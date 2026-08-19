"""OneBot 群任务失败通知的公开契约测试。"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from komari_bot.onebot import (
    GroupTaskFailureNotification,
    GroupTaskFailureNotifier,
    ImageFailureDiagnostic,
    InMemoryFailureNotificationCooldown,
    RedisFailureNotificationCooldown,
    image_failure_reason_code,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable


class _RecordingBot:
    """记录群消息与私聊，并允许在平台边界注入失败。"""

    def __init__(
        self,
        *,
        fail_group: bool = False,
        fail_private_users: set[int] | None = None,
    ) -> None:
        self.fail_group = fail_group
        self.fail_private_users = fail_private_users or set()
        self.group_calls: list[dict[str, object]] = []
        self.private_calls: list[dict[str, object]] = []

    async def call_api(self, api: str, **kwargs: object) -> dict[str, int]:
        self.group_calls.append({"api": api, **kwargs})
        if self.fail_group:
            raise RuntimeError("群消息投递失败")
        return {"message_id": 1}

    async def send_private_msg(self, user_id: int, message: str) -> None:
        self.private_calls.append({"user_id": user_id, "message": message})
        if user_id in self.fail_private_users:
            raise RuntimeError("私聊投递失败")


class _BrokenRedis:
    async def set(self, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError


def _notification(
    *,
    task_kind: str = "chat_reply",
    reason_code: str = "empty_reply",
    group_text: str | None = "固定群内失败提示",
    notify_superusers: bool = True,
    summary: str | None = "上游返回空回复",
    image_diagnostic: ImageFailureDiagnostic | None = None,
) -> GroupTaskFailureNotification:
    return GroupTaskFailureNotification(
        group_id=12345,
        message_id=99999,
        group_text=group_text,
        task_kind=task_kind,
        stage="generate",
        reason_code=reason_code,
        notify_superusers=notify_superusers,
        request_trace_id="trace-abc",
        summary=summary,
        image_diagnostic=image_diagnostic,
    )


def _notifier(
    *,
    superusers: Callable[[], Iterable[object]] = lambda: {"10001"},
    cooldown: object | None = None,
) -> GroupTaskFailureNotifier:
    return GroupTaskFailureNotifier(
        superusers_provider=superusers,
        cooldown=cooldown,
    )


@pytest.mark.asyncio
async def test_public_interface_sends_reply_and_safe_diagnostic_card() -> None:
    """公开接口同时投递引用原消息的固定文案与白名单诊断字段。"""
    bot = _RecordingBot()
    notifier = _notifier(superusers=lambda: {"10002", "10001"})

    await notifier.notify(bot=bot, notification=_notification())  # type: ignore[arg-type]

    assert bot.group_calls == [
        {
            "api": "send_group_msg",
            "group_id": 12345,
            "message": [
                {"type": "reply", "data": {"id": "99999"}},
                {"type": "text", "data": {"text": "固定群内失败提示"}},
            ],
        }
    ]
    assert [call["user_id"] for call in bot.private_calls] == [10001, 10002]
    text = str(bot.private_calls[0]["message"])
    assert "任务: chat_reply" in text
    assert "群: 12345" in text
    assert "阶段: generate" in text
    assert "原因: empty_reply" in text
    assert "trace: trace-abc" in text
    assert "摘要: 上游返回空回复" in text


@pytest.mark.asyncio
async def test_group_message_contains_only_caller_fixed_text() -> None:
    """诊断摘要与内部字段不会混入群内固定反馈。"""
    bot = _RecordingBot()
    notification = _notification(
        summary="异常正文 https://internal.example/api token=secret-value"
    )

    await _notifier().notify(bot=bot, notification=notification)  # type: ignore[arg-type]

    message = bot.group_calls[0]["message"]
    assert isinstance(message, list)
    rendered = str(message)
    assert "固定群内失败提示" in rendered
    assert "异常正文" not in rendered
    assert "internal.example" not in rendered
    assert "secret-value" not in rendered
    assert "trace-abc" not in rendered


@pytest.mark.asyncio
async def test_superuser_policy_does_not_disable_group_feedback() -> None:
    """调用方关闭 SUPERUSER 私聊时仍发送群内引用提示。"""
    bot = _RecordingBot()

    await _notifier().notify(  # type: ignore[arg-type]
        bot=bot,
        notification=_notification(notify_superusers=False),
    )

    assert len(bot.group_calls) == 1
    assert bot.private_calls == []


@pytest.mark.asyncio
async def test_none_group_text_only_sends_superuser_diagnostic() -> None:
    """调用方可只投递诊断卡，不向群内发送消息。"""
    bot = _RecordingBot()

    await _notifier().notify(  # type: ignore[arg-type]
        bot=bot,
        notification=_notification(group_text=None),
    )

    assert bot.group_calls == []
    assert len(bot.private_calls) == 1


@pytest.mark.asyncio
async def test_memory_cooldown_isolates_task_group_and_reason() -> None:
    """同任务、同群、同原因被去重，其他任务或原因互不串扰。"""
    bot = _RecordingBot()
    notifier = _notifier(cooldown=InMemoryFailureNotificationCooldown())

    await notifier.notify(bot=bot, notification=_notification())  # type: ignore[arg-type]
    await notifier.notify(bot=bot, notification=_notification())  # type: ignore[arg-type]
    await notifier.notify(  # type: ignore[arg-type]
        bot=bot,
        notification=_notification(reason_code="provider_timeout"),
    )
    await notifier.notify(  # type: ignore[arg-type]
        bot=bot,
        notification=_notification(task_kind="group_history_summary"),
    )

    assert len(bot.private_calls) == 3
    assert len(bot.group_calls) == 4


@pytest.mark.asyncio
async def test_redis_cooldown_failure_fails_open() -> None:
    """真实 Redis adapter 的外部调用失败时仍尽力发送诊断卡。"""
    bot = _RecordingBot()
    notifier = _notifier(
        cooldown=RedisFailureNotificationCooldown(_BrokenRedis())  # type: ignore[arg-type]
    )

    await notifier.notify(bot=bot, notification=_notification())  # type: ignore[arg-type]

    assert len(bot.private_calls) == 1


@pytest.mark.asyncio
async def test_runtime_superuser_noise_and_delivery_failures_never_escape() -> None:
    """无效收件人及单个投递失败不会阻断其他收件人。"""
    bot = _RecordingBot(fail_group=True, fail_private_users={10001})
    notifier = _notifier(superusers=lambda: {"invalid", "", "10001", "10002"})

    await notifier.notify(bot=bot, notification=_notification())  # type: ignore[arg-type]

    assert [call["user_id"] for call in bot.private_calls] == [10001, 10002]


@pytest.mark.asyncio
async def test_superuser_enumeration_failure_never_escapes() -> None:
    """NoneBot 运行时 SUPERUSER 枚举异常只记日志，不制造第二次故障。"""
    bot = _RecordingBot()

    def _raise() -> Iterable[object]:
        raise RuntimeError("运行时配置不可用")

    await _notifier(superusers=_raise).notify(  # type: ignore[arg-type]
        bot=bot,
        notification=_notification(),
    )

    assert len(bot.group_calls) == 1
    assert bot.private_calls == []


@pytest.mark.asyncio
async def test_summary_projection_removes_secrets_urls_and_extra_lines() -> None:
    """摘要会统一脱敏、移除链接、单行化并限制长度。"""
    bot = _RecordingBot()
    secret = "sk-" + "A" * 40
    first_line = (
        f"Authorization: Bearer {'B' * 32} {secret} "
        "postgresql://user:password@db.example/komari "
        "https://internal.example/path data:image/png;base64,AAAA "
        + "超" * 240
    )
    summary = (
        first_line
        + "\n消息正文=绝密消息 prompt=绝密提示 reasoning=绝密推理 "
        "tool_arguments={'url': 'https://tool.example'}"
    )

    await _notifier().notify(  # type: ignore[arg-type]
        bot=bot,
        notification=_notification(summary=summary),
    )

    text = str(bot.private_calls[0]["message"])
    assert secret not in text
    assert "Bearer " not in text
    assert "password@db.example" not in text
    assert "https://" not in text
    assert "data:image" not in text
    assert "绝密消息" not in text
    assert "绝密提示" not in text
    assert "绝密推理" not in text
    assert "tool_arguments" not in text
    assert "超" * 121 not in text


@pytest.mark.asyncio
async def test_image_diagnostic_card_renders_strict_whitelist_fields() -> None:
    """图片失败汇总卡只渲染白名单字段，不出现 message_id/URL/base64/正文。

    该卡替代“摘要卡 + 失败卡”的两张卡合并体：调用方显式传
    ``image_diagnostic`` 时只渲染任务/群/trace/图片模式/失败阶段/失败数量/
    失败类型，generic 卡片格式（任务/群/阶段/原因/trace/摘要）保持不变。
    """
    bot = _RecordingBot()
    diagnostic = ImageFailureDiagnostic(
        mode="delegated",
        failed_count=1,
        stages=("vision",),
        error_types=("vision_failed",),
    )
    await _notifier().notify(  # type: ignore[arg-type]
        bot=bot,
        notification=_notification(
            group_text=None,
            reason_code=image_failure_reason_code(diagnostic),
            summary=None,
            image_diagnostic=diagnostic,
        ),
    )

    assert bot.group_calls == []
    assert len(bot.private_calls) == 1
    text = str(bot.private_calls[0]["message"])
    assert "任务: chat_reply" in text
    assert "群: 12345" in text
    assert "trace: trace-abc" in text
    assert "图片模式: delegated" in text
    assert "失败阶段: vision" in text
    assert "失败数量: 1" in text
    assert "失败类型: vision_failed" in text
    # 白名单：不渲染 generic 阶段/原因/摘要字段与 message_id
    assert "99999" not in text
    assert "阶段: generate" not in text
    assert "原因: empty_reply" not in text
    assert "摘要:" not in text
    # 绝不出现可能泄漏的 URL/base64/正文/视觉描述
    assert "https://" not in text
    assert "base64" not in text
    assert "data:image" not in text
    assert "一只猫" not in text


@pytest.mark.asyncio
async def test_image_diagnostic_sorts_stages_and_error_types() -> None:
    """图片卡按字典序渲染去重后的失败阶段与错误类型。"""
    bot = _RecordingBot()
    diagnostic = ImageFailureDiagnostic(
        mode="native",
        failed_count=3,
        stages=("vision", "download", "download"),
        error_types=("image_unavailable", "vision_failed", "image_unavailable"),
    )
    await _notifier().notify(  # type: ignore[arg-type]
        bot=bot,
        notification=_notification(
            group_text=None,
            reason_code=image_failure_reason_code(diagnostic),
            summary=None,
            image_diagnostic=diagnostic,
        ),
    )

    text = str(bot.private_calls[0]["message"])
    assert "图片模式: native" in text
    assert "失败阶段: download, vision" in text
    assert "失败数量: 3" in text
    assert "失败类型: image_unavailable, vision_failed" in text


def test_image_failure_reason_code_is_stable_and_deterministic() -> None:
    """图片 reason_code 由模式与排序后的错误类型组成，跨任务稳定。"""
    assert (
        image_failure_reason_code(
            ImageFailureDiagnostic(
                mode="delegated",
                failed_count=2,
                stages=("vision", "download"),
                error_types=("vision_failed", "image_unavailable"),
            )
        )
        == "image_delegated_image_unavailable_vision_failed"
    )
    assert image_failure_reason_code(
        ImageFailureDiagnostic(
            mode="native",
            failed_count=1,
            stages=("download",),
            error_types=("image_unavailable",),
        )
    ) == "image_native_image_unavailable"
    # 错误类型顺序无关，结果确定性相同
    assert image_failure_reason_code(
        ImageFailureDiagnostic(
            mode="delegated",
            failed_count=1,
            stages=("vision",),
            error_types=("vision_failed",),
        )
    ) == "image_delegated_vision_failed"


def test_notification_contract_rejects_business_payload_fields() -> None:
    """窄契约不接收消息正文、prompt、reasoning、图片或工具参数。"""
    base = {
        "group_id": 12345,
        "message_id": 99999,
        "group_text": "固定群内失败提示",
        "task_kind": "chat_reply",
        "stage": "generate",
        "reason_code": "empty_reply",
        "notify_superusers": True,
    }

    for field in (
        "message_body",
        "prompt",
        "reasoning",
        "images",
        "tool_arguments",
        "business_payload",
    ):
        with pytest.raises(TypeError):
            GroupTaskFailureNotification(**base, **{field: "绝密内容"})  # type: ignore[arg-type]


def test_onebot_shared_layer_does_not_import_business_plugins() -> None:
    """共享 OneBot 边界不得反向依赖任一业务插件。"""
    onebot_dir = Path(__file__).resolve().parents[2] / "komari_bot" / "onebot"
    forbidden_prefix = "komari_bot.plugins"
    violations: list[str] = []

    for path in sorted(onebot_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                violations.extend(
                    f"{path.name}:{alias.name}"
                    for alias in node.names
                    if alias.name.startswith(forbidden_prefix)
                )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith(forbidden_prefix):
                    violations.append(f"{path.name}:{module}")

    assert violations == []
