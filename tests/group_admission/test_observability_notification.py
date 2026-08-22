"""TSK-223 阶段 B：SUPERUSER 通知内存边界。

验收目标（TSK-217 §7 冻结，全程假 Bot / 假时钟，无真实 sleep）：

- 在线 Bot 枚举支持 Mapping / Iterable；合法正整数 SUPERUSER 稳定去重排序
  （非法值被过滤）；每 recipient 按 Bot 顺序 fallback，首个成功后停止，恰
  一次投递；单 Bot / 单 recipient 发送失败不冒泡且其他 recipient 继续；
- 无 Bot / 全部失败：保留 **当前** 故障摘要内存，后续 ``process_observability``
  重试；不建 PG/Redis/outbox（存储调用计数不变）；
- 离线期间故障开始后又恢复（ready 稳定 60 秒）：只保留/发送一张「期间发生
  且现已恢复」的合并卡，不先补旧起始卡再发恢复卡；可变 bots provider 证明
  恢复后上线恰一张/recipient；
- 正常拒绝、stale revision、CAS 冲突/校验错误绝不通知。
"""

from __future__ import annotations

import asyncio

import pytest

from tests.group_admission.management_support import (
    POLICY_PATH,
    WRITER_TOKEN,
    asgi_client,
    atomic_policy,
    prepare_control_plane,
    put_headers,
)
from tests.group_admission.observability_support import (
    EVENT_RUNTIME_FAILED,
    EVENT_RUNTIME_RECOVERED,
    EVENT_RUNTIME_REMINDER,
    RECOVERY_STABILITY_SECONDS,
    REMINDER_INTERVAL_SECONDS,
    FakeAdmissionBot,
    FakeUtcClock,
    MutableBotsProvider,
    MutableSuperusersProvider,
    SentPrivateMessage,
    assert_exact_card,
    build_runtime_kwargs,
    card_texts,
    deliver_snapshot_direct,
)
from tests.group_admission.runtime_support import (
    AdmissionStorageFake,
    start_runtime,
    stored_policy,
)

pytestmark = pytest.mark.group_admission_acceptance


def _cold_failure_kwargs(
    clock: FakeUtcClock,
    bots_provider: MutableBotsProvider,
    superusers_provider: MutableSuperusersProvider,
) -> dict[str, object]:
    return build_runtime_kwargs(
        clock=clock,
        bots_provider=bots_provider,
        superusers_provider=superusers_provider,
    )


async def test_per_recipient_bot_fallback_delivers_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """recipient 稳定去重排序；按 Bot 顺序 fallback，首个成功后恰一次。（anchor）"""
    clock = FakeUtcClock()
    bad_bot = FakeAdmissionBot("bot-bad", fail=True)
    good_bot = FakeAdmissionBot("bot-good")
    bots_provider = MutableBotsProvider((bad_bot, good_bot))
    superusers_provider = MutableSuperusersProvider(
        (669293859, 42, 42, "not-an-int", -1, 0, True, 3.5)
    )
    storage = AdmissionStorageFake(
        fetch_error=RuntimeError("pg cold start failure")
    )

    runtime, _manager = await start_runtime(
        monkeypatch,
        storage,
        runtime_kwargs=_cold_failure_kwargs(
            clock, bots_provider, superusers_provider
        ),
    )

    await runtime.process_observability()

    # 合法正整数 SUPERUSER 稳定去重排序：42 在前，669293859 在后
    assert [sent.user_id for sent in good_bot.sent] == [42, 669293859], (
        "必须按去重排序后的收件人顺序各恰一次投递"
    )
    assert bad_bot.sent == []
    assert bad_bot.attempts == 2, "每个收件人都应先尝试首个 Bot（fallback）"
    # 全部投递经首个成功 Bot 完成，没有在成功后继续尝试后续 Bot


async def test_bots_mapping_enumeration_is_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """在线 Bot 枚举支持 Mapping 形态（NoneBot get_bots() 形状）。"""
    clock = FakeUtcClock()
    bad_bot = FakeAdmissionBot("bot-bad", fail=True)
    good_bot = FakeAdmissionBot("bot-good")
    bots_provider = MutableBotsProvider((bad_bot, good_bot), as_mapping=True)
    superusers_provider = MutableSuperusersProvider((42,))
    storage = AdmissionStorageFake(
        fetch_error=RuntimeError("pg cold start failure")
    )

    runtime, _manager = await start_runtime(
        monkeypatch,
        storage,
        runtime_kwargs=_cold_failure_kwargs(
            clock, bots_provider, superusers_provider
        ),
    )

    await runtime.process_observability()

    assert [sent.user_id for sent in good_bot.sent] == [42]
    assert bad_bot.attempts == 1


async def test_send_failure_does_not_bubble_and_pending_retries_next_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全部发送失败不冒泡；保留当前故障摘要，后续 process 重试恰一次。"""
    clock = FakeUtcClock()
    failing_bot = FakeAdmissionBot("bot-failing", fail=True)
    bots_provider = MutableBotsProvider((failing_bot,))
    superusers_provider = MutableSuperusersProvider((42,))
    storage = AdmissionStorageFake(
        fetch_error=RuntimeError("pg cold start failure")
    )

    runtime, manager = await start_runtime(
        monkeypatch,
        storage,
        runtime_kwargs=_cold_failure_kwargs(
            clock, bots_provider, superusers_provider
        ),
    )

    fetch_before = storage.fetch_calls
    cas_before = len(storage.cas_calls)

    # 全部失败：不冒泡，也不得触碰存储（无 outbox/PG/Redis）
    await runtime.process_observability()
    await runtime.process_observability()
    assert failing_bot.sent == []
    assert storage.fetch_calls == fetch_before
    assert len(storage.cas_calls) == cas_before

    # Bot 恢复后：重试投递当前故障摘要，恰一张（不重复累积）
    good_bot = FakeAdmissionBot("bot-recovered")
    bots_provider.set_bots(good_bot)
    await runtime.process_observability()

    cards = card_texts((good_bot,))
    assert len(cards) == 1, "重试只投递当前故障摘要一次"
    user_id, text = cards[0]
    assert user_id == 42
    assert "failed" in text
    assert "storage_unavailable" in text
    del manager


async def test_offline_fault_then_recovery_sends_single_combined_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """离线期间故障开始又恢复：只发一张「期间发生且现已恢复」合并卡。（anchor）"""
    clock = FakeUtcClock()
    bots_provider = MutableBotsProvider()  # 故障开始时完全离线
    superusers_provider = MutableSuperusersProvider((42,))
    storage = AdmissionStorageFake(
        fetch_error=RuntimeError("pg cold start failure")
    )

    runtime, _manager = await start_runtime(
        monkeypatch,
        storage,
        runtime_kwargs=_cold_failure_kwargs(
            clock, bots_provider, superusers_provider
        ),
    )

    # 离线期间：故障开始 → 经 watcher 恢复 ready → 稳定 60 秒结束 episode
    storage.deliver(stored_policy(1, {"mode": "blacklist", "group_ids": []}))
    assert runtime.get_state().status.value == "ready"
    clock.advance(seconds=60)
    await runtime.process_observability()  # episode 结束，但仍无 Bot

    # 恢复后 Bot 上线：只补一张合并卡，不先补旧起始卡再发恢复卡
    good_bot = FakeAdmissionBot("bot-online")
    bots_provider.set_bots(good_bot)
    await runtime.process_observability()

    cards = card_texts((good_bot,))
    assert len(cards) == 1, (
        f"离线故障+恢复只允许一张合并卡，实际 {len(cards)} 张"
    )
    user_id, text = cards[0]
    assert user_id == 42
    assert "storage_unavailable" in text, "合并卡必须保留故障身份"
    assert "ready" in text, "合并卡必须表达现已恢复"

    # 再次 process 不重复投递
    await runtime.process_observability()
    assert len(card_texts((good_bot,))) == 1


async def test_notification_delivery_is_ordered_and_independent_per_recipient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单收件人失败不影响其他收件人；每个收件人独立恰一次。"""
    clock = FakeUtcClock()

    class FlakyFirstRecipientBot(FakeAdmissionBot):
        """对第一个收件人恒失败、对其余收件人正常的 Bot。"""

        def __init__(self) -> None:
            super().__init__("bot-flaky")
            self.flaky_recipient: int | None = None

        async def send_private_msg(self, *, user_id: int, message: object) -> None:
            self.attempts += 1
            if user_id == self.flaky_recipient:
                msg = "flaky recipient path unavailable"
                raise RuntimeError(msg)
            self.sent.append(
                SentPrivateMessage(user_id=user_id, message=message)
            )

    flaky_bot = FlakyFirstRecipientBot()
    flaky_bot.flaky_recipient = 42
    backup_bot = FakeAdmissionBot("bot-backup")
    bots_provider = MutableBotsProvider((flaky_bot, backup_bot))
    superusers_provider = MutableSuperusersProvider((42, 669293859))
    storage = AdmissionStorageFake(
        fetch_error=RuntimeError("pg cold start failure")
    )

    runtime, _manager = await start_runtime(
        monkeypatch,
        storage,
        runtime_kwargs=_cold_failure_kwargs(
            clock, bots_provider, superusers_provider
        ),
    )

    await runtime.process_observability()

    # 42 在首个 Bot 失败后经备份 Bot 送达；669293859 由首个 Bot 直接送达
    assert [sent.user_id for sent in flaky_bot.sent] == [669293859]
    assert [sent.user_id for sent in backup_bot.sent] == [42]


async def test_no_notifications_for_normal_denials_stale_revisions_or_cas_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正常拒绝 / stale revision / CAS 冲突与校验错误绝不通知。"""
    clock = FakeUtcClock()
    bot = FakeAdmissionBot("bot-quiet")
    bots_provider = MutableBotsProvider((bot,))
    superusers_provider = MutableSuperusersProvider((42,))
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))

    app, runtime, _manager = await prepare_control_plane(
        monkeypatch,
        storage,
        runtime_kwargs=_cold_failure_kwargs(
            clock, bots_provider, superusers_provider
        ),
    )

    runtime.adjudicate([200])  # policy_restricted
    runtime.record_private_input_rejected(event_family="message")
    # stale：经运行时快照 listener 接缝直接驱动（共享配置层会先丢弃非严格更高
    # revision，运行时自身观察到乱序快照时也必须保持零通知）
    deliver_snapshot_direct(
        runtime,
        revision=1,
        policy=atomic_policy("blacklist", []),
        updated_at=clock.now,
    )

    async with asgi_client(app) as client:
        conflict = await client.put(
            POLICY_PATH,
            headers=put_headers(WRITER_TOKEN, if_match='"9"'),
            json=atomic_policy("blacklist", [300]),
        )
        assert conflict.status_code == 409
        invalid = await client.put(
            POLICY_PATH,
            headers=put_headers(WRITER_TOKEN, if_match='"1"'),
            json={"mode": "graylist", "group_ids": []},
        )
        assert invalid.status_code == 422

    await runtime.process_observability()

    assert bot.sent == [], "正常拒绝/stale/CAS 冲突/校验错误不得通知"
    assert bot.attempts == 0


async def test_concurrent_process_observability_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并发 process_observability 不重复投递：同一 episode 只产一张卡。

    两个 process 任务在同一时刻竞争；生产必须对 flush/投递加并发守卫，
    避免同一故障摘要被重复发送。
    """
    clock = FakeUtcClock()
    bot = FakeAdmissionBot("bot-concurrent")
    bots_provider = MutableBotsProvider((bot,))
    superusers_provider = MutableSuperusersProvider((42,))
    storage = AdmissionStorageFake(
        fetch_error=RuntimeError("pg cold start failure")
    )

    runtime, _manager = await start_runtime(
        monkeypatch,
        storage,
        runtime_kwargs=_cold_failure_kwargs(
            clock, bots_provider, superusers_provider
        ),
    )

    # 并发：两个 process 任务竞争同一 episode 的 flush
    await asyncio.gather(
        runtime.process_observability(),
        runtime.process_observability(),
    )

    cards = card_texts((bot,))
    assert len(cards) == 1, f"并发 process 不得重复投递，实际 {len(cards)} 张"


async def test_failed_recipient_retry_does_not_duplicate_succeeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """部分收件人失败：重试只补失败者，已成功者不重复收卡。"""
    clock = FakeUtcClock()

    class FirstRecipientFlakyBot(FakeAdmissionBot):
        """对首个排序收件人恒失败、其余正常的 Bot。"""

        def __init__(self) -> None:
            super().__init__("bot-partial-fail")

        async def send_private_msg(self, *, user_id: int, message: object) -> None:
            self.attempts += 1
            if self.fail and user_id == 42:
                msg = "recipient 42 path unavailable"
                raise RuntimeError(msg)
            self.sent.append(
                SentPrivateMessage(user_id=user_id, message=message)
            )

    flaky_bot = FirstRecipientFlakyBot()
    flaky_bot.fail = True  # 首收件人必须先失败（默认 fail=False），后续再恢复
    bots_provider = MutableBotsProvider((flaky_bot,))
    superusers_provider = MutableSuperusersProvider((42, 669293859))
    storage = AdmissionStorageFake(
        fetch_error=RuntimeError("pg cold start failure")
    )

    runtime, _manager = await start_runtime(
        monkeypatch,
        storage,
        runtime_kwargs=_cold_failure_kwargs(
            clock, bots_provider, superusers_provider
        ),
    )

    # 首次 process：42 在单一 Bot 上失败（无 fallback Bot）→ 保留为待重试；
    # 669293859 由该 Bot 直接送达（已成功）
    await runtime.process_observability()
    assert [sent.user_id for sent in flaky_bot.sent] == [669293859]

    # Bot 恢复（fail=False）后重试：只补失败者 42，已成功的 669293859
    # 不得因重试再次收到卡
    flaky_bot.fail = False
    await runtime.process_observability()

    delivered_user_ids = [sent.user_id for sent in flaky_bot.sent]
    assert delivered_user_ids.count(669293859) == 1, (
        "重试不得向已成功收件人重复投递"
    )
    assert delivered_user_ids.count(42) == 1, (
        "重试必须补投此前失败的收件人"
    )


async def test_cold_failure_start_reminder_recovery_cards_match_exact_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """冷故障 episode：start → 1800s 提醒 → ready 稳定 60s 恢复，三卡逐张精确。

    场景（bot42 全程在线）：process 投递 fault_start；clock +1800 再 process 投递
    reminder；storage.deliver ready@rev1 后 clock +60 再 process 投递 recovered。
    依次用 ``assert_exact_card`` 断言每张卡：字段集必须是 §7 冻结白名单子集、
    不得含 ``occurrence_count``，且 event/status/problem_code/duration_seconds/
    started_at（恢复含 resolved_at）全部等于预期值。
    """
    clock = FakeUtcClock()
    t0 = clock.now
    bot = FakeAdmissionBot("bot42")
    storage = AdmissionStorageFake(
        fetch_error=RuntimeError("pg cold start failure")
    )

    runtime, _manager = await start_runtime(
        monkeypatch,
        storage,
        runtime_kwargs=_cold_failure_kwargs(
            clock,
            MutableBotsProvider((bot,)),
            MutableSuperusersProvider((42,)),
        ),
    )

    # 1) 冷故障 start 卡
    await runtime.process_observability()
    cards = card_texts((bot,))
    assert len(cards) == 1, f"首次 process 必须恰一张起始卡: {len(cards)}"
    user_id, text = cards[0]
    assert user_id == 42
    assert_exact_card(
        text,
        expected={
            "event": EVENT_RUNTIME_FAILED,
            "status": "failed",
            "problem_code": "storage_unavailable",
            "configured_revision": None,
            "effective_revision": None,
            "using_last_known_good": False,
            "duration_seconds": 0,
            "started_at": t0.isoformat(),
        },
    )

    # 2) +1800 秒提醒卡
    clock.advance(seconds=REMINDER_INTERVAL_SECONDS)
    await runtime.process_observability()
    cards = card_texts((bot,))
    assert len(cards) == 2, f"1800 秒必须投递提醒卡，实际 {len(cards)}"
    user_id, text = cards[1]
    assert user_id == 42
    assert_exact_card(
        text,
        expected={
            "event": EVENT_RUNTIME_REMINDER,
            "status": "failed",
            "problem_code": "storage_unavailable",
            "configured_revision": None,
            "effective_revision": None,
            "using_last_known_good": False,
            "duration_seconds": REMINDER_INTERVAL_SECONDS,
            "started_at": t0.isoformat(),
        },
    )

    # 3) 存储就绪；ready 稳定 60 秒后恢复卡
    storage.deliver(stored_policy(1, {"mode": "blacklist", "group_ids": []}))
    assert runtime.get_state().status.value == "ready", "deliver ready@rev1 后必须立即 READY"
    t_recovery = clock.advance(seconds=RECOVERY_STABILITY_SECONDS)
    await runtime.process_observability()
    cards = card_texts((bot,))
    assert len(cards) == 3, f"恢复后必须恰三张卡，实际 {len(cards)}"
    user_id, text = cards[2]
    assert user_id == 42
    assert_exact_card(
        text,
        resolved=True,
        expected={
            "event": EVENT_RUNTIME_RECOVERED,
            "status": "ready",
            "problem_code": "storage_unavailable",
            "configured_revision": 1,
            "effective_revision": 1,
            "using_last_known_good": False,
            "duration_seconds": int((t_recovery - t0).total_seconds()),
            "started_at": t0.isoformat(),
            "resolved_at": t_recovery.isoformat(),
        },
    )
    assert int((t_recovery - t0).total_seconds()) == (
        REMINDER_INTERVAL_SECONDS + RECOVERY_STABILITY_SECONDS
    )
