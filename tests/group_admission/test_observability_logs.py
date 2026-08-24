"""TSK-223 阶段 B：结构化日志精确事件 / 级别 / 字段。

验收目标（TSK-217 §6 冻结，捕获 NoneBot/Loguru 真实 record，按
``extra.event`` 过滤）：

- ``runtime_failed`` ERROR；``runtime_degraded`` WARNING；``runtime_reminder``
  与当前状态同级（failed→ERROR / degraded→WARNING）；``runtime_recovered``
  INFO；``attribution_unavailable`` WARNING；``stale_revision_ignored``
  WARNING；``policy_published`` INFO；
- 运行时类日志 extra 键精确白名单：``event/status/problem_code/
  configured_revision/effective_revision/using_last_known_good/
  duration_seconds/occurrence_count/suppressed_count``；归属日志额外允许
  ``event_family/window_seconds``；策略发布日志只允许 ``event/old_revision/
  new_revision/policy_fingerprint/source``（source 封闭 ``startup/watcher/
  management``）；
- 不得记录原异常 / 异常类型 / 策略正文 / 群号 / 消息 ID / trace；
- stale revision：窗口首条日志、窗口内抑制、5 分钟后安全摘要（不通知、不降级）。
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.group_admission.management_support import (
    POLICY_PATH,
    READER_TOKEN,
    WRITER_TOKEN,
    asgi_client,
    atomic_policy,
    auth_headers,
    canonical_policy_fingerprint,
    normalized_policy,
    prepare_control_plane,
    put_headers,
)
from tests.group_admission.observability_support import (
    ATTRIBUTION_WINDOW_SECONDS,
    EVENT_POLICY_PUBLISHED,
    EVENT_RUNTIME_DEGRADED,
    EVENT_RUNTIME_FAILED,
    EVENT_RUNTIME_RECOVERED,
    EVENT_RUNTIME_REMINDER,
    EVENT_STALE_REVISION_IGNORED,
    POLICY_EXTRA_KEYS,
    REMINDER_INTERVAL_SECONDS,
    RUNTIME_EXTRA_WHITELIST,
    FakeAdmissionBot,
    FakeUtcClock,
    MutableBotsProvider,
    MutableSuperusersProvider,
    assert_runtime_extra_whitelist,
    build_runtime_kwargs,
    business_extra,
    capture_admission_logs,
    deliver_snapshot_direct,
    runtime_log_projection,
)
from tests.group_admission.runtime_support import (
    AdmissionStorageFake,
    stored_policy,
)
from tests.group_admission.sensitive_canary import build_extended_canary_bundle

pytestmark = pytest.mark.group_admission_acceptance


def _level(record: Any) -> str:
    return str(record["level"].name)


async def test_policy_published_logs_startup_watcher_management_with_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """policy_published INFO：三种 source 各证明一次，指纹为规范化 SHA-256。"""
    clock = FakeUtcClock()
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, _runtime, _manager = await prepare_control_plane(
        monkeypatch,
        storage,
        runtime_kwargs=build_runtime_kwargs(clock=clock),
    )

    with capture_admission_logs() as capture:
        storage.deliver(stored_policy(2, atomic_policy("whitelist", [900, 800, 900])))

        async with asgi_client(app) as client:
            response = await client.put(
                POLICY_PATH,
                headers=put_headers(WRITER_TOKEN, if_match='"2"'),
                json=atomic_policy("blacklist", []),
            )
            assert response.status_code == 200, response.text

    records = capture.by_event(EVENT_POLICY_PUBLISHED)
    assert len(records) == 2, (
        f"watcher/management 各应产生一条 policy_published: {capture.event_names()}"
    )
    # startup 发布发生在 capture 挂载前，单独验证总数与内容经后续记录补齐：
    # 三条发布的 old→new 链必须连贯（1→2 与 2→3）。
    # 按 source 映射，不依赖记录顺序（顺序本身不可观测）。
    by_source = {record["extra"]["source"]: record for record in records}
    assert set(by_source) == {"watcher", "management"}, (
        f"policy_published source 集合应为 {{watcher, management}}: {sorted(by_source)}"
    )
    for record in records:
        assert _level(record) == "INFO"
        extra = business_extra(record)
        assert set(extra) == POLICY_EXTRA_KEYS, (
            f"policy_published extra 键集不精确: {sorted(extra)}"
        )
    watcher = by_source["watcher"]
    assert watcher["extra"]["old_revision"] == 1
    assert watcher["extra"]["new_revision"] == 2
    assert watcher["extra"]["policy_fingerprint"] == canonical_policy_fingerprint(
        normalized_policy("whitelist", [900, 800, 900])
    )
    management = by_source["management"]
    assert management["extra"]["old_revision"] == 2
    assert management["extra"]["new_revision"] == 3
    assert management["extra"]["policy_fingerprint"] == canonical_policy_fingerprint(
        normalized_policy("blacklist", [])
    )


async def test_policy_published_fingerprint_matches_shared_canonical_for_multigroup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3：多群号策略的 policy_published 指纹必须与 CLI 共享 canonical 指纹一致。

    当前运行时 ``_canonical_policy_fingerprint`` 取升序形态摘要，而 CLI 共享
    ``policy_fingerprint`` 取去重降序形态摘要 → 多群号分叉（红）。
    """
    from komari_bot.admission_policy import policy_fingerprint as shared_fingerprint

    raw = {"mode": "whitelist", "group_ids": [900, 800, 900, 700]}
    clock = FakeUtcClock()
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    _app, _runtime, _manager = await prepare_control_plane(
        monkeypatch,
        storage,
        runtime_kwargs=build_runtime_kwargs(clock=clock),
    )

    with capture_admission_logs() as capture:
        storage.deliver(
            stored_policy(2, atomic_policy("whitelist", [900, 800, 900, 700]))
        )

    records = capture.by_event(EVENT_POLICY_PUBLISHED)
    assert len(records) == 1, capture.event_names()
    record = records[0]
    assert record["extra"]["source"] == "watcher"
    assert record["extra"]["policy_fingerprint"] == shared_fingerprint(raw), (
        "policy_published 指纹必须与 CLI 共享 canonical 指纹一致"
    )


async def test_policy_published_startup_source_on_successful_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """start 成功接纳首个修订：source=startup、old_revision=None。"""
    clock = FakeUtcClock()
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [])))

    with capture_admission_logs() as capture:
        await prepare_control_plane(
            monkeypatch,
            storage,
            runtime_kwargs=build_runtime_kwargs(clock=clock),
        )

    records = capture.by_event(EVENT_POLICY_PUBLISHED)
    assert len(records) == 1
    record = records[0]
    assert _level(record) == "INFO"
    assert set(business_extra(record)) == POLICY_EXTRA_KEYS
    assert record["extra"]["source"] == "startup"
    assert record["extra"]["old_revision"] is None
    assert record["extra"]["new_revision"] == 1
    assert record["extra"]["policy_fingerprint"] == canonical_policy_fingerprint(
        normalized_policy("blacklist", [])
    )


async def test_runtime_failed_log_on_cold_start_storage_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """冷启动存储失败：恰一条 runtime_failed ERROR，字段白名单内。"""
    clock = FakeUtcClock()
    storage = AdmissionStorageFake(
        fetch_error=RuntimeError("pg cold start failure")
    )

    with capture_admission_logs() as capture:
        await prepare_control_plane(
            monkeypatch,
            storage,
            runtime_kwargs=build_runtime_kwargs(
                clock=clock,
                bots_provider=MutableBotsProvider(),
                superusers_provider=MutableSuperusersProvider(),
            ),
        )

    records = capture.by_event(EVENT_RUNTIME_FAILED)
    assert len(records) == 1, capture.event_names()
    record = records[0]
    assert _level(record) == "ERROR"
    extra = assert_runtime_extra_whitelist(
        record, whitelist=RUNTIME_EXTRA_WHITELIST
    )
    assert extra["status"] == "failed"
    assert extra["problem_code"] == "storage_unavailable"
    assert extra["configured_revision"] is None
    assert extra["effective_revision"] is None
    assert extra["using_last_known_good"] is False


async def test_runtime_degraded_log_on_persistent_refresh_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """持久刷新失败进入同一观测链：恰一条 runtime_degraded WARNING。"""
    clock = FakeUtcClock()
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, _runtime, _manager = await prepare_control_plane(
        monkeypatch,
        storage,
        runtime_kwargs=build_runtime_kwargs(clock=clock),
    )

    with capture_admission_logs() as capture:
        storage.fetch_error = RuntimeError("pg refresh failure")
        async with asgi_client(app) as client:
            assert (
                await client.get(POLICY_PATH, headers=auth_headers(READER_TOKEN))
            ).status_code == 503

            # 重复失败只累计：不再产生新的降级日志
            assert (
                await client.get(POLICY_PATH, headers=auth_headers(READER_TOKEN))
            ).status_code == 503

    records = capture.by_event(EVENT_RUNTIME_DEGRADED)
    assert len(records) == 1, capture.event_names()
    record = records[0]
    assert _level(record) == "WARNING"
    extra = assert_runtime_extra_whitelist(
        record, whitelist=RUNTIME_EXTRA_WHITELIST
    )
    assert extra["status"] == "degraded"
    assert extra["problem_code"] == "storage_unavailable"
    assert extra["configured_revision"] == 1
    assert extra["effective_revision"] == 1
    assert extra["using_last_known_good"] is True


async def test_reminder_level_follows_current_runtime_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """runtime_reminder 与当前状态同级：degraded→WARNING，failed→ERROR。"""
    # degraded episode
    clock = FakeUtcClock()
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, runtime, _manager = await prepare_control_plane(
        monkeypatch,
        storage,
        runtime_kwargs=build_runtime_kwargs(clock=clock),
    )
    storage.fetch_error = RuntimeError("pg refresh failure")
    async with asgi_client(app) as client:
        assert (
            await client.get(POLICY_PATH, headers=auth_headers(READER_TOKEN))
        ).status_code == 503

    with capture_admission_logs() as capture:
        clock.advance(seconds=REMINDER_INTERVAL_SECONDS)
        await runtime.process_observability()

    degraded_reminders = capture.by_event(EVENT_RUNTIME_REMINDER)
    assert len(degraded_reminders) == 1, capture.event_names()
    assert _level(degraded_reminders[0]) == "WARNING"
    extra = assert_runtime_extra_whitelist(
        degraded_reminders[0], whitelist=RUNTIME_EXTRA_WHITELIST
    )
    assert extra["status"] == "degraded"
    assert extra["occurrence_count"] == 1

    # failed episode（冷启动失败）
    clock = FakeUtcClock()
    storage = AdmissionStorageFake(
        fetch_error=RuntimeError("pg cold start failure")
    )
    _app, runtime, _manager = await prepare_control_plane(
        monkeypatch,
        storage,
        runtime_kwargs=build_runtime_kwargs(clock=clock),
    )
    with capture_admission_logs() as failed_capture:
        clock.advance(seconds=REMINDER_INTERVAL_SECONDS)
        await runtime.process_observability()

    failed_reminders = failed_capture.by_event(EVENT_RUNTIME_REMINDER)
    assert len(failed_reminders) == 1, failed_capture.event_names()
    assert _level(failed_reminders[0]) == "ERROR"
    assert failed_reminders[0]["extra"]["status"] == "failed"


async def test_runtime_recovered_log_is_info_and_emitted_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ready 稳定 60 秒后：恰一条 runtime_recovered INFO，重复 process 不重复。"""
    clock = FakeUtcClock()
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, runtime, _manager = await prepare_control_plane(
        monkeypatch,
        storage,
        runtime_kwargs=build_runtime_kwargs(clock=clock),
    )

    storage.fetch_error = RuntimeError("pg refresh failure")
    async with asgi_client(app) as client:
        assert (
            await client.get(POLICY_PATH, headers=auth_headers(READER_TOKEN))
        ).status_code == 503
        storage.fetch_error = None
        assert (
            await client.get(POLICY_PATH, headers=auth_headers(READER_TOKEN))
        ).status_code == 200

    with capture_admission_logs() as capture:
        clock.advance(seconds=59)
        await runtime.process_observability()
        assert capture.by_event(EVENT_RUNTIME_RECOVERED) == [], (
            "59 秒稳定期未满不得写恢复日志"
        )

        clock.advance(seconds=1)
        await runtime.process_observability()
        recovered = capture.by_event(EVENT_RUNTIME_RECOVERED)
        assert len(recovered) == 1, capture.event_names()
        assert _level(recovered[0]) == "INFO"
        extra = assert_runtime_extra_whitelist(
            recovered[0], whitelist=RUNTIME_EXTRA_WHITELIST
        )
        assert extra["status"] == "ready"
        assert extra["problem_code"] is None
        assert extra["occurrence_count"] == 1

        # 重复 process 不得再次写恢复日志
        clock.advance(seconds=300)
        await runtime.process_observability()
        assert len(capture.by_event(EVENT_RUNTIME_RECOVERED)) == 1


async def test_stale_revision_first_log_window_suppression_and_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stale revision：首条 WARNING，窗口内抑制，5 分钟摘要；不通知不降级。

    乱序/重复快照经运行时快照 listener 接缝直接驱动（真实 ConfigManager 在共
    享层丢弃非严格更高 revision，水远到不了运行时，见 ``deliver_snapshot_direct``）。
    """
    clock = FakeUtcClock()
    bot = FakeAdmissionBot("bot-stale")
    storage = AdmissionStorageFake(stored_policy(2, atomic_policy("blacklist", [200])))
    _app, runtime, _manager = await prepare_control_plane(
        monkeypatch,
        storage,
        runtime_kwargs=build_runtime_kwargs(
            clock=clock,
            bots_provider=MutableBotsProvider((bot,)),
            superusers_provider=MutableSuperusersProvider((42,)),
        ),
    )

    with capture_admission_logs() as capture:
        deliver_snapshot_direct(
            runtime,
            revision=1,
            policy=atomic_policy("blacklist", []),
            updated_at=clock.now,
        )
        first_records = capture.by_event(EVENT_STALE_REVISION_IGNORED)
        assert len(first_records) == 1, "窗口首次乱序修订必须立即记一条日志"
        assert _level(first_records[0]) == "WARNING"
        extra = assert_runtime_extra_whitelist(
            first_records[0], whitelist=RUNTIME_EXTRA_WHITELIST
        )
        assert extra["configured_revision"] == 2
        assert extra["effective_revision"] == 2

        # 窗口内后续乱序/重复修订只抑制
        deliver_snapshot_direct(
            runtime,
            revision=1,
            policy=atomic_policy("blacklist", []),
            updated_at=clock.now,
        )
        deliver_snapshot_direct(
            runtime,
            revision=2,
            policy=atomic_policy("whitelist", [700]),
            updated_at=clock.now,
        )
        assert len(capture.by_event(EVENT_STALE_REVISION_IGNORED)) == 1

        # 未满 5 分钟：不摘要
        clock.advance(seconds=ATTRIBUTION_WINDOW_SECONDS - 1)
        await runtime.process_observability()
        assert len(capture.by_event(EVENT_STALE_REVISION_IGNORED)) == 1

        # 5 分钟后：安全摘要（suppressed_count 精确）
        clock.advance(seconds=1)
        await runtime.process_observability()
        records = capture.by_event(EVENT_STALE_REVISION_IGNORED)
        assert len(records) == 2, capture.event_names()
        assert _level(records[1]) == "WARNING"
        summary_extra = assert_runtime_extra_whitelist(
            records[1], whitelist=RUNTIME_EXTRA_WHITELIST
        )
        assert summary_extra["suppressed_count"] == 2

        # 下一窗口重新首条
        deliver_snapshot_direct(
            runtime,
            revision=1,
            policy=atomic_policy("blacklist", []),
            updated_at=clock.now,
        )
        assert len(capture.by_event(EVENT_STALE_REVISION_IGNORED)) == 3

    # 不通知、不降级
    assert bot.sent == []
    assert bot.attempts == 0
    state = runtime.get_state()
    assert state.status.value == "ready"
    assert state.configured_revision == 2
    assert state.effective_revision == 2


async def test_runtime_logs_never_carry_exception_or_dynamic_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """恶意异常正文/类型不得进入任何准入日志的 message 或 extra。"""
    bundle = build_extended_canary_bundle()
    malicious = " ".join(
        token.value for token in bundle.tokens if isinstance(token.value, str)
    )
    clock = FakeUtcClock()
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, runtime, _manager = await prepare_control_plane(
        monkeypatch,
        storage,
        runtime_kwargs=build_runtime_kwargs(clock=clock),
    )

    with capture_admission_logs() as capture:
        storage.fetch_error = RuntimeError(malicious)
        async with asgi_client(app) as client:
            assert (
                await client.get(POLICY_PATH, headers=auth_headers(READER_TOKEN))
            ).status_code == 503
        storage.deliver(stored_policy(2, {"mode": "graylist", "group_ids": []}))
        clock.advance(seconds=REMINDER_INTERVAL_SECONDS)
        await runtime.process_observability()

    assert capture.records, "应当已捕获降级/提醒日志"
    for record in capture.records:
        projection = runtime_log_projection(record)
        bundle.assert_no_leaks(
            projection, context=f"准入日志 {record['extra'].get('event')}"
        )
        extra_keys = set(record["extra"])
        for forbidden_key in ("error", "exception", "traceback", "exc_info"):
            assert forbidden_key not in extra_keys, (
                f"日志 extra 不得携带异常字段: {forbidden_key}"
            )
        assert record["exception"] is None, "准入日志不得附带异常对象"
