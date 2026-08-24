"""TSK-248 正式周期任务驱动可观测性验收（AC5）。

生产 seam：nonebot-plugin-apscheduler 公开 scheduler job registry / 可替换
scheduler 接缝。经**确定性调用**注册的周期 job 函数验证
``process_observability`` 覆盖：

- 5 分钟归属窗口（首条告警 + 满窗安全摘要 + pending 通知投递）；
- 30 分钟故障提醒（重复刷新失败累计后，job 在 1800 秒边界输出提醒卡）；
- READY 连续稳定 60 秒恢复（job 恰一次输出恢复日志 + 恢复卡并结束 episode）；
- pending SUPERUSER 通知重试（无在线 Bot 时整批保留，Bot 上线后下一周期重试
  投递）。

全程假时钟 / 假 Bot / 假 SUPERUSER，无真实 sleep；运行时只经真实 driver
startup hook 启动，周期 job 只经 scheduler 接缝注册并确定性调用。

红基线：当前生产代码尚未装配 scheduler job，``ctx.scheduler.jobs`` 为空，
本文件用例因无 job 可调用而失败（red）。
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.group_admission.lifecycle_support import (
    invoke_hook,
    lifecycle_context,
    require_single_startup_hook,
)
from tests.group_admission.observability_support import (
    ATTRIBUTION_EXTRA_WHITELIST,
    EVENT_ATTRIBUTION_UNAVAILABLE,
    EVENT_RUNTIME_FAILED,
    EVENT_RUNTIME_RECOVERED,
    EVENT_RUNTIME_REMINDER,
    RECOVERY_STABILITY_SECONDS,
    REMINDER_INTERVAL_SECONDS,
    FakeAdmissionBot,
    FakeUtcClock,
    MutableBotsProvider,
    MutableSuperusersProvider,
    assert_runtime_extra_whitelist,
    build_runtime_kwargs,
    capture_admission_logs,
    card_texts,
)
from tests.group_admission.runtime_support import (
    AdmissionStorageFake,
    stored_policy,
)

pytestmark = pytest.mark.group_admission_acceptance

_SUPERUSER_ID = 42
BLACKLIST_EMPTY: dict[str, object] = {"mode": "blacklist", "group_ids": []}


def _observability_kwargs(
    clock: FakeUtcClock,
    bots_provider: MutableBotsProvider,
    superusers_provider: MutableSuperusersProvider,
) -> dict[str, object]:
    return build_runtime_kwargs(
        clock=clock,
        bots_provider=bots_provider,
        superusers_provider=superusers_provider,
    )


def _only_job_func(jobs: list[dict[str, Any]]) -> Any:
    """断言恰一个周期 job 并返回其可确定性调用的函数。"""
    assert len(jobs) == 1, f"生产必须注册恰一个周期 job，实际 {len(jobs)}"
    return jobs[0]["func"]


async def test_scheduler_job_drives_attribution_window_and_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC5：周期 job 驱动 5 分钟归属窗口摘要与 pending 通知投递。

    首条归属失败立即日志并排队卡；满 300 秒后的 job 输出窗口安全摘要并把
    已排队的卡投递给 SUPERUSER；同窗内后续只累计。全程经 job 函数调用，不
    直接调用 ``process_observability``。
    """
    clock = FakeUtcClock()
    bot = FakeAdmissionBot("bot-window")
    bots_provider = MutableBotsProvider((bot,))
    superusers_provider = MutableSuperusersProvider((_SUPERUSER_ID,))
    storage = AdmissionStorageFake(stored_policy(1, BLACKLIST_EMPTY))

    async with lifecycle_context(
        monkeypatch,
        storage,
        runtime_kwargs=_observability_kwargs(
            clock, bots_provider, superusers_provider
        ),
    ) as ctx:
        await invoke_hook(require_single_startup_hook(ctx))
        job_func = _only_job_func(ctx.scheduler.jobs)

        with capture_admission_logs() as capture:
            # 首条归属失败：立即日志 + 排队卡（不投递）。
            ctx.runtime.adjudicate(["not-a-group"], event_family="message")
            assert len(capture.by_event(EVENT_ATTRIBUTION_UNAVAILABLE)) == 1
            assert bot.sent == []

            # 窗口内后续只累计。
            clock.advance(seconds=100)
            ctx.runtime.adjudicate(["bad"], event_family="message")
            clock.advance(seconds=199)  # t0+299：窗口内最后一秒
            ctx.runtime.adjudicate([None], event_family="message")
            assert len(capture.by_event(EVENT_ATTRIBUTION_UNAVAILABLE)) == 1

            # 周期 job 在 t0+299（窗口未满）：只投递首卡，无摘要。
            await job_func()
            assert len(capture.by_event(EVENT_ATTRIBUTION_UNAVAILABLE)) == 1
            assert len(bot.sent) == 1

            # 周期 job 在 t0+300：输出窗口安全摘要（suppressed_count 精确）。
            clock.advance(seconds=1)
            await job_func()
            records = capture.by_event(EVENT_ATTRIBUTION_UNAVAILABLE)
            assert len(records) == 2, capture.event_names()
            summary_extra = assert_runtime_extra_whitelist(
                records[1], whitelist=ATTRIBUTION_EXTRA_WHITELIST
            )
            assert summary_extra["suppressed_count"] == 2, (
                "窗口摘要必须精确报告窗口内被抑制的后续次数"
            )
            assert summary_extra["event_family"] == "message"

        # 卡片只含安全字段。
        cards = card_texts((bot,))
        assert len(cards) == 1
        user_id, text = cards[0]
        assert user_id == _SUPERUSER_ID
        assert "group_attribution_unavailable" in text
        assert "message" in text
        assert "3" in text, "卡必须携带窗口内观察总次数"


async def test_scheduler_job_drives_reminder_and_stable_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC5：周期 job 驱动 30 分钟提醒与 READY 60 秒稳定恢复。

    冷启动失败经 driver hook 收敛 failed（episode 开始）；job 在 0 秒投递
    起始卡、1800 秒边界输出提醒卡；存储恢复并投递合法快照后状态立即 READY，
    但 episode 需连续稳定 60 秒后由 job 恰一次结束（恢复卡 + ``runtime_recovered``
    日志）。
    """
    clock = FakeUtcClock()
    bot = FakeAdmissionBot("bot-episode")
    bots_provider = MutableBotsProvider((bot,))
    superusers_provider = MutableSuperusersProvider((_SUPERUSER_ID,))
    storage = AdmissionStorageFake(
        fetch_error=RuntimeError("pg cold start failure")
    )

    async with lifecycle_context(
        monkeypatch,
        storage,
        runtime_kwargs=_observability_kwargs(
            clock, bots_provider, superusers_provider
        ),
    ) as ctx:
        with capture_admission_logs() as capture:
            # startup 经 driver hook 收敛 failed 并在 capture 内发出
            # ``runtime_failed`` 起始日志；job 在 t0 投递起始卡（无提醒）。
            await invoke_hook(require_single_startup_hook(ctx))
            job_func = _only_job_func(ctx.scheduler.jobs)
            await job_func()
            assert len(capture.by_event(EVENT_RUNTIME_FAILED)) == 1
            assert len(bot.sent) == 1

            # 1799 秒：仍无提醒。
            clock.advance(seconds=REMINDER_INTERVAL_SECONDS - 1)
            await job_func()
            assert len(capture.by_event(EVENT_RUNTIME_REMINDER)) == 0
            assert len(bot.sent) == 1

            # 1800 秒边界：job 输出提醒卡。
            clock.advance(seconds=1)
            await job_func()
            assert len(capture.by_event(EVENT_RUNTIME_REMINDER)) == 1
            assert len(bot.sent) == 2

            # 存储恢复：投递合法快照 → 状态立即 READY，episode 未结束。
            storage.fetch_error = None
            storage.deliver(stored_policy(1, BLACKLIST_EMPTY))
            assert ctx.runtime.get_state().is_ready is True

            # 稳定 59 秒：job 不输出恢复。
            clock.advance(seconds=RECOVERY_STABILITY_SECONDS - 1)
            await job_func()
            assert len(capture.by_event(EVENT_RUNTIME_RECOVERED)) == 0
            assert len(bot.sent) == 2

            # 稳定满 60 秒：job 恰一次输出恢复日志 + 恢复卡并结束 episode。
            clock.advance(seconds=1)
            await job_func()
            assert len(capture.by_event(EVENT_RUNTIME_RECOVERED)) == 1
            assert len(bot.sent) == 3

        cards = card_texts((bot,))
        assert len(cards) == 3
        # 起始 → 提醒 → 恢复 顺序固定。
        assert "failed" in cards[0][1] and "storage_unavailable" in cards[0][1]
        assert "reminder" in cards[1][1]
        assert "recovered" in cards[2][1]


async def test_scheduler_job_retries_pending_notification_when_bots_online(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC5：pending SUPERUSER 通知重试由周期 job 驱动。

    无在线 Bot 时起始卡整批保留；Bot 上线后下一周期 job 重新投递；已成功投
    递的收件人不重复。
    """
    clock = FakeUtcClock()
    bot = FakeAdmissionBot("bot-retry")
    bots_provider = MutableBotsProvider()
    superusers_provider = MutableSuperusersProvider((_SUPERUSER_ID,))
    storage = AdmissionStorageFake(
        fetch_error=RuntimeError("pg cold start failure")
    )

    async with lifecycle_context(
        monkeypatch,
        storage,
        runtime_kwargs=_observability_kwargs(
            clock, bots_provider, superusers_provider
        ),
    ) as ctx:
        await invoke_hook(require_single_startup_hook(ctx))
        job_func = _only_job_func(ctx.scheduler.jobs)

        # 无在线 Bot：起始卡排队但整批保留（不投递）。
        await job_func()
        assert bot.sent == [], "无在线 Bot 时不得投递"

        # Bot 上线：下一周期 job 重试投递恰一次。
        bots_provider.set_bots(bot)
        await job_func()
        assert len(bot.sent) == 1, "Bot 上线后周期 job 必须重试投递 pending 卡"
        user_id = bot.sent[0].user_id
        text = str(bot.sent[0].message)
        assert user_id == _SUPERUSER_ID
        assert "failed" in text and "storage_unavailable" in text

        # 已投递：再跑一个周期不重复。
        await job_func()
        assert len(bot.sent) == 1
