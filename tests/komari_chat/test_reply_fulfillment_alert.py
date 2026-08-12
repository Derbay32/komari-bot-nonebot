"""回复履约去重告警的领域验收测试。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any

from komari_bot.plugins.komari_chat.services.reply_fulfillment_alert import (
    ReplyFulfillmentAlertService,
)


class _AlertRepository:
    """持久化告警转换的内存替身；多个服务实例共享同一去重事实。"""

    def __init__(self) -> None:
        self.pending: list[dict[str, Any]] = []
        self.dispositions: list[dict[str, Any]] = []
        self._claimed_pending: set[str] = set()
        self._claimed_dispositions: set[tuple[str, str]] = set()
        self._lock = asyncio.Lock()

    def seed_pending(self, fulfillment_id: str, **extra: object) -> None:
        self.pending.append(
            {
                "fulfillment_id": fulfillment_id,
                "status": "pending_confirmation",
                "commitment_type": None,
                "error_code": None,
                **extra,
            }
        )

    def seed_disposition(
        self,
        fulfillment_id: str,
        *,
        commitment_type: str,
        error_code: str,
        **extra: object,
    ) -> None:
        self.dispositions.append(
            {
                "fulfillment_id": fulfillment_id,
                "status": "needs_disposition",
                "commitment_type": commitment_type,
                "error_code": error_code,
                **extra,
            }
        )

    async def claim_pending_confirmation_alerts(
        self,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        async with self._lock:
            claimed: list[dict[str, Any]] = []
            for row in self.pending:
                fulfillment_id = str(row["fulfillment_id"])
                if fulfillment_id in self._claimed_pending:
                    continue
                self._claimed_pending.add(fulfillment_id)
                claimed.append(deepcopy(row))
                if len(claimed) >= limit:
                    break
            return claimed

    async def claim_commitment_disposition_alerts(
        self,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        async with self._lock:
            claimed: list[dict[str, Any]] = []
            for row in self.dispositions:
                identity = (
                    str(row["fulfillment_id"]),
                    str(row["commitment_type"]),
                )
                if identity in self._claimed_dispositions:
                    continue
                self._claimed_dispositions.add(identity)
                claimed.append(deepcopy(row))
                if len(claimed) >= limit:
                    break
            return claimed

    def reset_disposition_claim(
        self,
        fulfillment_id: str,
        commitment_type: str,
    ) -> None:
        """模拟人工续跑的代际重置：仓储清除该子项的告警标记。

        只重置指定承诺；兄弟承诺的去重事实不受影响（与真实仓储的
        ``resume_failed_commitment`` 语义一致）。
        """
        self._claimed_dispositions.discard((fulfillment_id, commitment_type))


class _RecordingBot:
    def __init__(self, *, fail_users: set[int] | None = None) -> None:
        self.fail_users = fail_users or set()
        self.private_calls: list[dict[str, object]] = []

    async def send_private_msg(self, *, user_id: int, message: str) -> None:
        self.private_calls.append({"user_id": user_id, "message": message})
        if user_id in self.fail_users:
            raise RuntimeError("私聊投递失败且不得进入告警正文")


class _RecordingLogger:
    def __init__(self) -> None:
        self.bound: list[dict[str, object]] = []
        self.messages: list[str] = []

    def bind(self, **fields: object) -> _RecordingLogger:
        self.bound.append(fields)
        return self

    def warning(self, message: str, *_args: object, **_kwargs: object) -> None:
        self.messages.append(message)


async def test_first_transitions_alert_once_across_restart() -> None:
    """两类首次转换各告警一次，服务重建与重复观察都不会重发。"""
    repository = _AlertRepository()
    repository.seed_pending("reply-pending")
    repository.seed_disposition(
        "reply-disposition",
        commitment_type="favorability_adjustment",
        error_code="service_unavailable",
    )
    bot = _RecordingBot()
    logger = _RecordingLogger()

    first = ReplyFulfillmentAlertService(
        repository,
        bots_provider=lambda: [bot],
        superusers_provider=lambda: {"10002", "10001"},
        logger=logger,
    )
    restarted = ReplyFulfillmentAlertService(
        repository,
        bots_provider=lambda: [bot],
        superusers_provider=lambda: {"10002", "10001"},
        logger=logger,
    )

    assert await first.recover_alerts() == 2
    assert await restarted.recover_alerts() == 0

    assert len(logger.bound) == 2
    assert len(bot.private_calls) == 4
    assert [call["user_id"] for call in bot.private_calls] == [
        10001,
        10002,
        10001,
        10002,
    ]


async def test_concurrent_workers_only_dispatch_one_alert() -> None:
    """并发 worker 通过持久化领取竞争，同一转换只有一个胜者。"""
    repository = _AlertRepository()
    repository.seed_pending("reply-race")
    bot = _RecordingBot()
    logger = _RecordingLogger()
    services = [
        ReplyFulfillmentAlertService(
            repository,
            bots_provider=lambda: [bot],
            superusers_provider=lambda: {"10001"},
            logger=logger,
        )
        for _ in range(2)
    ]

    results = await asyncio.gather(*(service.recover_alerts() for service in services))

    assert sorted(results) == [0, 1]
    assert len(logger.bound) == 1
    assert len(bot.private_calls) == 1


async def test_resumed_commitment_alert_new_generation_after_reexhaustion() -> None:
    """人工续跑后再次耗尽必须产生新一代告警，兄弟承诺不连带重置。

    第一次耗尽告警一次并被持久去重抑制；运维续跑清除该子项告警
    标记后再次耗尽，同一（履约, 承诺）身份必须再次告警；兄弟承诺
    的告警代际不受续跑影响，仍只告警一次。
    """
    repository = _AlertRepository()
    repository.seed_disposition(
        "reply-resume",
        commitment_type="favorability_adjustment",
        error_code="service_unavailable",
    )
    repository.seed_disposition(
        "reply-resume",
        commitment_type="assistant_reply_history",
        error_code="service_unavailable",
    )
    bot = _RecordingBot()
    logger = _RecordingLogger()
    service = ReplyFulfillmentAlertService(
        repository,
        bots_provider=lambda: [bot],
        superusers_provider=lambda: {"10001"},
        logger=logger,
    )

    # 第一轮：两项待处置各告警一次，重复观察被抑制
    assert await service.recover_alerts() == 2
    assert await service.recover_alerts() == 0
    assert len(bot.private_calls) == 2

    # 人工续跑 favorability（只重置该子项告警代际）后再次耗尽
    repository.reset_disposition_claim("reply-resume", "favorability_adjustment")
    assert await service.recover_alerts() == 1
    assert len(bot.private_calls) == 3

    # 新一代同样被去重；兄弟承诺全程不重复告警
    assert await service.recover_alerts() == 0
    assert len(bot.private_calls) == 3


async def test_no_online_bot_still_records_minimal_structured_alert() -> None:
    """无在线 Bot 时结构化告警仍成立，私聊通道只做尽力投递。"""
    repository = _AlertRepository()
    repository.seed_pending(
        "reply-no-bot",
        reply_content="绝密回复正文",
        payload={"prompt": "绝密提示"},
    )
    logger = _RecordingLogger()
    service = ReplyFulfillmentAlertService(
        repository,
        bots_provider=list,
        superusers_provider=lambda: {"10001"},
        logger=logger,
    )

    assert await service.recover_alerts() == 1

    assert logger.bound == [
        {
            "fulfillment_id": "reply-no-bot",
            "status": "pending_confirmation",
            "commitment_type": None,
            "error_code": None,
        }
    ]
    rendered_log = "\n".join(logger.messages)
    assert "fulfillment_id=reply-no-bot" in rendered_log
    assert "status=pending_confirmation" in rendered_log
    assert "commitment_type=-" in rendered_log
    assert "error_code=-" in rendered_log
    assert "绝密回复正文" not in repr(logger.bound)
    assert "绝密回复正文" not in rendered_log
    assert "绝密提示" not in repr(logger.bound)
    assert "绝密提示" not in rendered_log


async def test_private_delivery_is_best_effort_and_uses_allowlisted_fields() -> None:
    """单个收件人失败不阻断其他人，告警卡只渲染最小白名单字段。"""
    repository = _AlertRepository()
    repository.seed_disposition(
        "reply-safe-card",
        commitment_type="interaction_history",
        error_code="invalid_payload",
        reply_content="绝密回复正文",
        group_history="绝密群历史",
        payload={"reasoning": "绝密推理"},
        url="https://internal.example/secret",
        credential="sk-THIS-MUST-NOT-LEAK",
        raw_error="原始异常正文",
    )
    bot = _RecordingBot(fail_users={10001})
    logger = _RecordingLogger()
    service = ReplyFulfillmentAlertService(
        repository,
        bots_provider=lambda: [bot],
        superusers_provider=lambda: {
            "invalid",
            "0",
            "-1",
            "10001",
            10001,
            "10002",
        },
        logger=logger,
    )

    assert await service.recover_alerts() == 1

    assert [call["user_id"] for call in bot.private_calls] == [10001, 10002]
    text = str(bot.private_calls[-1]["message"])
    assert "reply-safe-card" in text
    assert "needs_disposition" in text
    assert "interaction_history" in text
    assert "invalid_payload" in text
    for forbidden in (
        "绝密回复正文",
        "绝密群历史",
        "绝密推理",
        "internal.example",
        "THIS-MUST-NOT-LEAK",
        "原始异常正文",
    ):
        assert forbidden not in text
        assert forbidden not in repr(logger.bound)


async def test_transient_retry_does_not_create_disposition_alert() -> None:
    """普通自动重试没有待处置转换，只保留执行器诊断而不制造告警。"""
    repository = _AlertRepository()
    bot = _RecordingBot()
    logger = _RecordingLogger()
    service = ReplyFulfillmentAlertService(
        repository,
        bots_provider=lambda: [bot],
        superusers_provider=lambda: {"10001"},
        logger=logger,
    )

    assert await service.recover_alerts() == 0
    assert logger.bound == []
    assert bot.private_calls == []
