"""TSK-223 阶段 B：运行时故障期（fault episode）生命周期。

验收目标（TSK-217 §4 冻结，全程假时钟、无真实 sleep）：

- 首次进入 failed / degraded：立即固定日志 + 排队通知，对应问题码
  occurrence +1；重复刷新失败只累计，不逐次日志/卡、不再 +1；
- 每持续 30 分钟一条提醒：1799 秒无、1800 秒有；同一时点重复 process 无；
  下一 30 分钟再有；
- storage ready@rev1 后持久刷新失败 → DEGRADED/storage_unavailable+LKG；
  成功同 revision 刷新 → 状态立即 READY；ready 连续 59 秒无恢复，60 秒恰一
  次 ``runtime_recovered`` 日志 + 恢复卡，并清 ``problem_since``；
- 60 秒内再失败仍同一 episode（无新 start 日志/卡/occurrence），稳定期取
  消；再次 ready + 60 秒才恢复；恢复后下一次失败是新 episode（新日志/卡、
  occurrence +1）；
- 更高非法 persisted policy → DEGRADED/stored_policy_invalid；persisted 未
  发布 → DEGRADED/snapshot_publish_failed；occurrence maps 对应；
- ``policy_restricted`` / ``effective_policy_unavailable`` 不另发事件卡。

API 触发的存储失败/成功与快照发布失败经同一运行时状态/观测链，不另造
telemetry。
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.group_admission.management_support import (
    POLICY_PATH,
    READER_TOKEN,
    STATUS_PATH,
    WRITER_TOKEN,
    asgi_client,
    assert_whitelist_detail,
    atomic_policy,
    auth_headers,
    prepare_control_plane,
    put_headers,
)
from tests.group_admission.observability_support import (
    EVENT_RUNTIME_DEGRADED,
    EVENT_RUNTIME_FAILED,
    EVENT_RUNTIME_RECOVERED,
    EVENT_RUNTIME_REMINDER,
    REMINDER_INTERVAL_SECONDS,
    FakeAdmissionBot,
    FakeUtcClock,
    MutableBotsProvider,
    MutableSuperusersProvider,
    assert_rfc3339_utc,
    build_runtime_kwargs,
    capture_admission_logs,
    card_texts,
)
from tests.group_admission.runtime_support import (
    AdmissionStorageFake,
    detach_runtime_listener,
    stored_policy,
)

pytestmark = pytest.mark.group_admission_acceptance

_SUPERUSER_ID = 42


def _episode_kwargs(
    clock: FakeUtcClock,
    bot: FakeAdmissionBot,
) -> dict[str, object]:
    return build_runtime_kwargs(
        clock=clock,
        bots_provider=MutableBotsProvider((bot,)),
        superusers_provider=MutableSuperusersProvider((_SUPERUSER_ID,)),
    )


async def _status_body(client: Any) -> dict[str, Any]:
    response = await client.get(STATUS_PATH, headers=auth_headers(READER_TOKEN))
    assert response.status_code == 200, response.text
    return response.json()


async def test_cold_start_failure_starts_failed_episode_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """冷启动失败：恰一条 runtime_failed + occurrence 1；重复失败只累计。（anchor）"""
    clock = FakeUtcClock()
    t0 = clock.now
    bot = FakeAdmissionBot("bot-cold")
    storage = AdmissionStorageFake(
        fetch_error=RuntimeError("pg cold start failure")
    )

    with capture_admission_logs() as capture:
        app, runtime, _manager = await prepare_control_plane(
            monkeypatch, storage, runtime_kwargs=_episode_kwargs(clock, bot)
        )

        failed_logs = capture.by_event(EVENT_RUNTIME_FAILED)
        assert len(failed_logs) == 1, capture.event_names()
        assert bot.sent == [], "episode 通知必须排队到 process 投递"

        # 重复刷新失败只累计：无新日志、无新卡、不再计 occurrence
        async with asgi_client(app) as client:
            assert (
                await client.get(POLICY_PATH, headers=auth_headers(READER_TOKEN))
            ).status_code == 503
            assert (
                await client.get(POLICY_PATH, headers=auth_headers(READER_TOKEN))
            ).status_code == 503

            body = await _status_body(client)
            assert assert_rfc3339_utc(
                body["problem_since"], field_name="problem_since"
            ) == t0
            assert body["telemetry"]["runtime_problem_occurrences"] == {
                "storage_unavailable": 1,
                "stored_policy_invalid": 0,
                "snapshot_publish_failed": 0,
                "internal_error": 0,
            }

            await runtime.process_observability()
            assert len(capture.by_event(EVENT_RUNTIME_FAILED)) == 1

    cards = card_texts((bot,))
    assert len(cards) == 1, "首次故障只投递一张起始卡"
    _user_id, text = cards[0]
    assert "failed" in text
    assert "storage_unavailable" in text


async def test_repeated_failures_accumulate_into_reminder_occurrence_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """episode 内重复失败计入 occurrence_count：30 分钟提醒携带精确值。"""
    clock = FakeUtcClock()
    bot = FakeAdmissionBot("bot-occurrence")
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, runtime, _manager = await prepare_control_plane(
        monkeypatch, storage, runtime_kwargs=_episode_kwargs(clock, bot)
    )

    storage.fetch_error = RuntimeError("pg refresh failure")
    async with asgi_client(app) as client:
        for _index in range(3):
            assert (
                await client.get(POLICY_PATH, headers=auth_headers(READER_TOKEN))
            ).status_code == 503

    with capture_admission_logs() as capture:
        clock.advance(seconds=REMINDER_INTERVAL_SECONDS)
        await runtime.process_observability()

    reminders = capture.by_event(EVENT_RUNTIME_REMINDER)
    assert len(reminders) == 1, capture.event_names()
    assert reminders[0]["extra"]["occurrence_count"] == 3, (
        "提醒必须携带 episode 内累计的失败次数（含首次）"
    )
    assert reminders[0]["extra"]["status"] == "degraded"
    duration = reminders[0]["extra"]["duration_seconds"]
    assert isinstance(duration, int) and duration >= REMINDER_INTERVAL_SECONDS


async def test_reminders_every_30_minutes_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1799 秒无、1800 秒有；同一时点重复 process 无；下一 30 分钟再有。"""
    clock = FakeUtcClock()
    bot = FakeAdmissionBot("bot-reminder")
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, runtime, _manager = await prepare_control_plane(
        monkeypatch, storage, runtime_kwargs=_episode_kwargs(clock, bot)
    )

    storage.fetch_error = RuntimeError("pg refresh failure")
    async with asgi_client(app) as client:
        assert (
            await client.get(POLICY_PATH, headers=auth_headers(READER_TOKEN))
        ).status_code == 503

    with capture_admission_logs() as capture:
        clock.advance(seconds=REMINDER_INTERVAL_SECONDS - 1)
        await runtime.process_observability()
        assert capture.by_event(EVENT_RUNTIME_REMINDER) == [], (
            "1799 秒不得发送提醒"
        )

        clock.advance(seconds=1)
        await runtime.process_observability()
        assert len(capture.by_event(EVENT_RUNTIME_REMINDER)) == 1

        # 同一时点重复 process 不重复提醒
        await runtime.process_observability()
        assert len(capture.by_event(EVENT_RUNTIME_REMINDER)) == 1

        clock.advance(seconds=REMINDER_INTERVAL_SECONDS)
        await runtime.process_observability()
        reminders = capture.by_event(EVENT_RUNTIME_REMINDER)
        assert len(reminders) == 2, "下一个 30 分钟必须再有提醒"

    # 起始卡 + 两张提醒卡，逐次恰一投递
    assert len(card_texts((bot,))) == 3


async def test_recovery_needs_60s_stability_and_failure_within_keeps_episode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """60 秒内再失败仍同一 episode；再次 ready+60 才恢复；恢复后是新 episode。"""
    clock = FakeUtcClock()
    bot = FakeAdmissionBot("bot-stability")
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, runtime, _manager = await prepare_control_plane(
        monkeypatch, storage, runtime_kwargs=_episode_kwargs(clock, bot)
    )
    t0 = clock.now

    storage.fetch_error = RuntimeError("pg refresh failure")
    async with asgi_client(app) as client:
        assert (
            await client.get(POLICY_PATH, headers=auth_headers(READER_TOKEN))
        ).status_code == 503  # episode 开始 @t0

        storage.fetch_error = None
        clock.advance(seconds=10)
        assert (
            await client.get(POLICY_PATH, headers=auth_headers(READER_TOKEN))
        ).status_code == 200  # ready @t0+10

        # 50 秒稳定后再失败：同一 episode，无新起始日志/卡/occurrence
        clock.advance(seconds=50)
        storage.fetch_error = RuntimeError("pg flap within stability")
        assert (
            await client.get(POLICY_PATH, headers=auth_headers(READER_TOKEN))
        ).status_code == 503

        body = await _status_body(client)
        assert assert_rfc3339_utc(
            body["problem_since"], field_name="problem_since"
        ) == t0, "60 秒内再失败必须仍属原 episode"
        assert body["telemetry"]["runtime_problem_occurrences"][
            "storage_unavailable"
        ] == 1

        storage.fetch_error = None
        clock.advance(seconds=5)
        assert (
            await client.get(POLICY_PATH, headers=auth_headers(READER_TOKEN))
        ).status_code == 200  # ready @t0+65，稳定期重新计算

    with capture_admission_logs() as capture:
        clock.advance(seconds=59)  # ready 后 59 秒
        await runtime.process_observability()
        assert capture.by_event(EVENT_RUNTIME_RECOVERED) == []

        clock.advance(seconds=1)  # ready 后 60 秒
        await runtime.process_observability()
        recovered = capture.by_event(EVENT_RUNTIME_RECOVERED)
        assert len(recovered) == 1, capture.event_names()

    async with asgi_client(app) as client:
        body = await _status_body(client)
        assert body["problem_since"] is None

    # 恢复后下一次失败是新 episode：新日志、新卡、occurrence +1
    with capture_admission_logs() as new_capture:
        storage.fetch_error = RuntimeError("pg new episode failure")
        async with asgi_client(app) as client:
            assert (
                await client.get(POLICY_PATH, headers=auth_headers(READER_TOKEN))
            ).status_code == 503
            assert len(new_capture.by_event(EVENT_RUNTIME_DEGRADED)) == 1
            body = await _status_body(client)
            assert body["telemetry"]["runtime_problem_occurrences"][
                "storage_unavailable"
            ] == 2
            await runtime.process_observability()

    # 起始卡 1 + 恢复卡 1 + 新 episode 起始卡 1
    assert len(card_texts((bot,))) == 3


async def test_invalid_higher_revision_episode_degrades_with_stored_policy_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """更高非法 persisted policy：DEGRADED/stored_policy_invalid episode。"""
    clock = FakeUtcClock()
    bot = FakeAdmissionBot("bot-invalid")
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, runtime, _manager = await prepare_control_plane(
        monkeypatch, storage, runtime_kwargs=_episode_kwargs(clock, bot)
    )

    with capture_admission_logs() as capture:
        storage.deliver(stored_policy(2, {"mode": "graylist", "group_ids": []}))

    degraded = capture.by_event(EVENT_RUNTIME_DEGRADED)
    assert len(degraded) == 1, capture.event_names()
    assert degraded[0]["extra"]["problem_code"] == "stored_policy_invalid"
    assert degraded[0]["extra"]["using_last_known_good"] is True

    async with asgi_client(app) as client:
        body = await _status_body(client)
        assert body["telemetry"]["runtime_problem_occurrences"][
            "stored_policy_invalid"
        ] == 1

    # 合法更高修订恢复 → READY；60 秒稳定后 episode 结束并通知恢复
    with capture_admission_logs() as recovery_capture:
        storage.deliver(stored_policy(3, atomic_policy("blacklist", [])))
        assert runtime.get_state().status.value == "ready"
        clock.advance(seconds=60)
        await runtime.process_observability()
        assert len(recovery_capture.by_event(EVENT_RUNTIME_RECOVERED)) == 1

    await runtime.process_observability()
    assert len(card_texts((bot,))) == 2  # 起始卡 + 恢复卡


async def test_persisted_but_unpublished_starts_snapshot_publish_failed_episode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已持久化未发布：进入同一观测链，problem code 为 snapshot_publish_failed。"""
    clock = FakeUtcClock()
    bot = FakeAdmissionBot("bot-unpublished")
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, runtime, manager = await prepare_control_plane(
        monkeypatch, storage, runtime_kwargs=_episode_kwargs(clock, bot)
    )
    detach_runtime_listener(manager, runtime)

    with capture_admission_logs() as capture:
        async with asgi_client(app) as client:
            response = await client.put(
                POLICY_PATH,
                headers=put_headers(WRITER_TOKEN, if_match='"1"'),
                json=atomic_policy("whitelist", [700]),
            )

    assert_whitelist_detail(
        response,
        503,
        "snapshot_publish_failed",
        configured_revision=2,
        effective_revision=1,
        persisted=True,
    )
    degraded = capture.by_event(EVENT_RUNTIME_DEGRADED)
    assert len(degraded) == 1, capture.event_names()
    assert degraded[0]["extra"]["problem_code"] == "snapshot_publish_failed"

    async with asgi_client(app) as client:
        body = await _status_body(client)
        assert body["telemetry"]["runtime_problem_occurrences"][
            "snapshot_publish_failed"
        ] == 1
        assert body["problem_since"] is not None

    await runtime.process_observability()
    assert len(card_texts((bot,))) == 1


async def test_stale_revision_snapshot_does_not_change_problem_occurrences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """更高非法修订进入 degraded episode 后，stale revision 刷新不改变 occurrence。

    已接纳 rev2（非法 → stored_policy_invalid episode，occurrence=1）后，再
    投递低于已接纳修订的 stale revision（rev1）快照：运行时按严格更高修订
    守卫忽略，episode 与 problem occurrence 计数均保持不变。
    """
    clock = FakeUtcClock()
    bot = FakeAdmissionBot("bot-stale-occurrence")
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, _runtime, _manager = await prepare_control_plane(
        monkeypatch, storage, runtime_kwargs=_episode_kwargs(clock, bot)
    )

    # 更高非法修订：进入 stored_policy_invalid episode，occurrence = 1
    storage.deliver(stored_policy(2, {"mode": "graylist", "group_ids": []}))
    async with asgi_client(app) as client:
        body = await _status_body(client)
        assert body["telemetry"]["runtime_problem_occurrences"][
            "stored_policy_invalid"
        ] == 1

    # stale revision（低于已接纳的 rev2）：运行时忽略，episode 与 occurrence 不变
    storage.deliver(stored_policy(1, atomic_policy("blacklist", [200])))
    async with asgi_client(app) as client:
        body = await _status_body(client)
    assert body["status"] == "degraded"
    assert body["configured_revision"] == 2
    assert body["telemetry"]["runtime_problem_occurrences"][
        "stored_policy_invalid"
    ] == 1, "stale revision 刷新不得改变 problem occurrence"


async def test_normal_denials_do_not_open_episodes_or_send_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """policy_restricted / effective unavailable 不另发事件卡，也不开 episode。"""
    clock = FakeUtcClock()
    bot = FakeAdmissionBot("bot-no-episode")
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    _app, runtime, _manager = await prepare_control_plane(
        monkeypatch, storage, runtime_kwargs=_episode_kwargs(clock, bot)
    )

    with capture_admission_logs() as capture:
        runtime.adjudicate([200])  # policy_restricted
        runtime.adjudicate([200, 300])
        await runtime.close()
        runtime.adjudicate([100])  # effective_policy_unavailable
        await runtime.process_observability()
        assert capture.records == [], capture.event_names()

    assert bot.sent == []
    assert bot.attempts == 0
