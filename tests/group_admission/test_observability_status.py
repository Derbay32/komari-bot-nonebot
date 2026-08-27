"""TSK-223 阶段 B：``GET /status`` 完整固定投影（精确键集、无 I/O）。

验收目标（TSK-217 §5 冻结）：

- status 顶层键集精确 = 5 个运行时字段 + 5 个时间字段 + ``telemetry``；
  telemetry 键集精确 = ``started_at/adjudications_total/by_reason_code/
  by_intent/attribution_failures_by_event_family/runtime_problem_occurrences``；
- 时间为 UTC RFC 3339，未发生为 ``null``；``configured_updated_at`` 来自
  ConfigSnapshot（持久修订的存储写入时间），``effective_loaded_at`` /
  ``last_refresh_attempt_at`` / ``last_storage_success_at`` / ``problem_since``
  来自私有可控时钟；
- 未启动 failed：attempt/success/problem_since 与快照时间均 ``null``；start
  成功：attempt/success/loaded 有值且 problem_since 为 ``null``；更高非法修
  订与存储失败正确保留 effective loaded 与 configured updated，problem_since
  稳定在故障开始时刻；同 revision 存储恢复后运行时状态立即 READY，但故障
  episode 到 ready 连续稳定 60 秒才结束（期间 problem_since 保留，60 秒后的
  首次 ``process_observability`` 清 ``null``）；
- 存储方法调用即抛时 status 仍 200 且存储调用计数不变；响应不出现
  policy/mode/group ids/fingerprint/异常/重试计划（精确键集已排除）。

全部时间经注入的 ``FakeUtcClock`` 确定性控制，无真实 sleep。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from komari_bot.plugins.config_manager import manager as manager_module
from tests.group_admission.management_support import (
    POLICY_PATH,
    READER_TOKEN,
    STATUS_PATH,
    WRITER_TOKEN,
    asgi_client,
    atomic_policy,
    auth_headers,
    management_credentials,
    prepare_control_plane,
    put_headers,
)
from tests.group_admission.observability_support import (
    RECOVERY_STABILITY_SECONDS,
    FakeUtcClock,
    MutableBotsProvider,
    MutableSuperusersProvider,
    assert_rfc3339_utc,
    assert_status_exact_shape,
    assert_telemetry_closed_maps,
    build_runtime_kwargs,
)
from tests.group_admission.runtime_support import (
    AdmissionStorageFake,
    build_runtime,
    import_admission_package,
    import_runtime_module,
    install_singleton,
    stored_policy,
)
from tests.group_admission.sensitive_canary import (
    build_extended_canary_bundle,
)

pytestmark = pytest.mark.group_admission_acceptance


def _observability_kwargs(clock: FakeUtcClock) -> dict[str, object]:
    """状态投影用例默认注入时钟 + 空通知环境（通知细节归通知用例）。"""
    return build_runtime_kwargs(
        clock=clock,
        bots_provider=MutableBotsProvider(),
        superusers_provider=MutableSuperusersProvider(),
    )


async def test_status_projects_exact_full_field_set_when_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ready 状态完整投影：键集精确、时间有值且来自私有时钟/快照。"""
    clock = FakeUtcClock()
    t0 = clock.now
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, _runtime, _manager = await prepare_control_plane(
        monkeypatch, storage, runtime_kwargs=_observability_kwargs(clock)
    )

    async with asgi_client(app) as client:
        response = await client.get(STATUS_PATH, headers=auth_headers(READER_TOKEN))

    assert response.status_code == 200, response.text
    body = response.json()
    assert_status_exact_shape(body)

    assert body["status"] == "ready"
    assert body["problem_code"] is None
    assert body["configured_revision"] == 1
    assert body["effective_revision"] == 1
    assert body["using_last_known_good"] is False

    # configured_updated_at 来自 ConfigSnapshot 的存储写入时间
    configured_updated = assert_rfc3339_utc(
        body["configured_updated_at"], field_name="configured_updated_at"
    )
    assert configured_updated == stored_policy(1, {}).updated_at

    assert assert_rfc3339_utc(
        body["effective_loaded_at"], field_name="effective_loaded_at"
    ) == t0
    assert assert_rfc3339_utc(
        body["last_refresh_attempt_at"], field_name="last_refresh_attempt_at"
    ) == t0
    assert assert_rfc3339_utc(
        body["last_storage_success_at"], field_name="last_storage_success_at"
    ) == t0
    assert body["problem_since"] is None

    telemetry = body["telemetry"]
    assert assert_rfc3339_utc(
        telemetry["started_at"], field_name="telemetry.started_at"
    ) == t0
    assert_telemetry_closed_maps(telemetry, total=0)


async def test_started_at_is_construction_moment_and_frozen_after_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """telemetry.started_at 精确等于运行时构造时刻（假时钟），start 后冻结不变。

    构造与 start 之间推进时钟；started_at 必须保持构造时刻，不得因
    ``start`` 刷新为更晚的时钟值（冻结契约）。
    """
    clock = FakeUtcClock()
    constructed_at = clock.now  # 构造前捕获，等价于构造时刻

    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    runtime, manager = build_runtime(
        monkeypatch, storage, runtime_kwargs=build_runtime_kwargs(clock=clock)
    )

    # 构造与 start 之间推进时钟：started_at 必须保持构造时刻，不因 start 刷新
    clock.advance(seconds=1234)

    await runtime.start(manager)
    install_singleton(monkeypatch, runtime)

    admission = import_admission_package()
    app = FastAPI()
    admission.register_group_admission_api(
        app,
        api_token=management_credentials(),
        allowed_origins=[],
    )

    async with asgi_client(app) as client:
        response = await client.get(
            STATUS_PATH, headers=auth_headers(READER_TOKEN)
        )
    body = response.json()
    assert_status_exact_shape(body)
    started_at = assert_rfc3339_utc(
        body["telemetry"]["started_at"], field_name="telemetry.started_at"
    )
    assert started_at == constructed_at, (
        "started_at 必须冻结在构造时刻而非 start 时刻"
    )


async def test_status_unstarted_failed_projects_null_times_and_zero_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未启动 failed：attempt/success/problem_since 与快照时间均 null。

    telemetry 仍完整预初始化（计数全零、四张 map 封闭键齐全），
    ``started_at`` 非 null。
    """
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [])))
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: storage)

    runtime_module = import_runtime_module()
    runtime = runtime_module._AdmissionRuntime()
    install_singleton(monkeypatch, runtime)

    admission = import_admission_package()
    app = FastAPI()
    admission.register_group_admission_api(
        app,
        api_token=[
            {
                "credential_id": "reader",
                "token": "reader-token-00000000",
                "permissions": ["config:read"],
            }
        ],
        allowed_origins=[],
        audit_recorder=None,
    )

    async with asgi_client(app) as client:
        response = await client.get(
            STATUS_PATH, headers=auth_headers(READER_TOKEN)
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert_status_exact_shape(body)

    assert body["status"] == "failed"
    assert body["problem_code"] == "storage_unavailable"
    assert body["configured_revision"] is None
    assert body["effective_revision"] is None
    assert body["using_last_known_good"] is False

    for field_name in (
        "configured_updated_at",
        "effective_loaded_at",
        "last_refresh_attempt_at",
        "last_storage_success_at",
        "problem_since",
    ):
        assert body[field_name] is None, f"未启动时 {field_name} 必须是 null"

    telemetry = body["telemetry"]
    assert telemetry["started_at"] is not None
    assert_rfc3339_utc(telemetry["started_at"], field_name="telemetry.started_at")
    assert_telemetry_closed_maps(telemetry, total=0)


async def test_status_invalid_higher_revision_keeps_times_and_sets_problem_since(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """更高非法修订：保留 effective loaded / configured updated，problem_since 置位。

    configured_updated_at 跟随已接纳的更高持久修订（rev2 存储写入时间）；
    effective_loaded_at 保留 LKG（rev1）装载时刻；problem_since 为故障发生
    的私有时钟时刻；problem occurrence 记 1 次。
    """
    clock = FakeUtcClock()
    t0 = clock.now
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, _runtime, _manager = await prepare_control_plane(
        monkeypatch, storage, runtime_kwargs=_observability_kwargs(clock)
    )

    clock.advance(seconds=10)
    fault_at = clock.now
    storage.deliver(stored_policy(2, {"mode": "graylist", "group_ids": []}))

    async with asgi_client(app) as client:
        response = await client.get(STATUS_PATH, headers=auth_headers(READER_TOKEN))

    body = response.json()
    assert_status_exact_shape(body)
    assert body["status"] == "degraded"
    assert body["problem_code"] == "stored_policy_invalid"
    assert body["configured_revision"] == 2
    assert body["effective_revision"] == 1
    assert body["using_last_known_good"] is True

    configured_updated = assert_rfc3339_utc(
        body["configured_updated_at"], field_name="configured_updated_at"
    )
    assert configured_updated == stored_policy(2, {}).updated_at
    assert assert_rfc3339_utc(
        body["effective_loaded_at"], field_name="effective_loaded_at"
    ) == t0, "降级不得刷新 effective_loaded_at"
    assert assert_rfc3339_utc(
        body["problem_since"], field_name="problem_since"
    ) == fault_at
    assert body["telemetry"]["runtime_problem_occurrences"][
        "stored_policy_invalid"
    ] == 1


async def test_status_storage_failure_keeps_effective_times_and_stable_problem_since(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """持久刷新失败：保留 rev1 时间锚点，problem_since 在重复失败下稳定。"""
    clock = FakeUtcClock()
    t0 = clock.now
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, _runtime, _manager = await prepare_control_plane(
        monkeypatch, storage, runtime_kwargs=_observability_kwargs(clock)
    )

    clock.advance(seconds=20)
    fault_at = clock.now
    storage.fetch_error = RuntimeError("pg refresh failure #1")

    async with asgi_client(app) as client:
        first = await client.get(POLICY_PATH, headers=auth_headers(READER_TOKEN))
        assert first.status_code == 503

        clock.advance(seconds=10)
        second = await client.get(POLICY_PATH, headers=auth_headers(READER_TOKEN))
        assert second.status_code == 503
        attempt_after_second = clock.now

        response = await client.get(
            STATUS_PATH, headers=auth_headers(READER_TOKEN)
        )

    body = response.json()
    assert_status_exact_shape(body)
    assert body["status"] == "degraded"
    assert body["problem_code"] == "storage_unavailable"
    assert body["configured_revision"] == 1
    assert body["effective_revision"] == 1
    assert body["using_last_known_good"] is True

    assert assert_rfc3339_utc(
        body["configured_updated_at"], field_name="configured_updated_at"
    ) == stored_policy(1, {}).updated_at
    assert assert_rfc3339_utc(
        body["effective_loaded_at"], field_name="effective_loaded_at"
    ) == t0, "存储失败不得刷新 effective_loaded_at"
    assert assert_rfc3339_utc(
        body["last_refresh_attempt_at"], field_name="last_refresh_attempt_at"
    ) == attempt_after_second, "每次持久刷新尝试必须更新 attempt 时间"
    assert assert_rfc3339_utc(
        body["last_storage_success_at"], field_name="last_storage_success_at"
    ) == t0, "失败刷新不得更新 success 时间"
    assert assert_rfc3339_utc(
        body["problem_since"], field_name="problem_since"
    ) == fault_at, "重复失败下 problem_since 必须稳定在首次故障时刻"
    assert body["telemetry"]["runtime_problem_occurrences"][
        "storage_unavailable"
    ] == 1


async def test_status_recovery_ready_immediately_but_problem_since_clears_after_stability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同 revision 存储恢复：状态立即 READY，problem_since 到 60 秒稳定后才清空。"""
    clock = FakeUtcClock()
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, runtime, _manager = await prepare_control_plane(
        monkeypatch, storage, runtime_kwargs=_observability_kwargs(clock)
    )

    clock.advance(seconds=20)
    fault_at = clock.now
    storage.fetch_error = RuntimeError("pg refresh failure before recovery")

    async with asgi_client(app) as client:
        assert (
            await client.get(POLICY_PATH, headers=auth_headers(READER_TOKEN))
        ).status_code == 503

        storage.fetch_error = None
        clock.advance(seconds=20)
        recovered_at = clock.now
        assert (
            await client.get(POLICY_PATH, headers=auth_headers(READER_TOKEN))
        ).status_code == 200

        body = (await client.get(STATUS_PATH, headers=auth_headers(READER_TOKEN))).json()
        assert body["status"] == "ready", "成功刷新必须立即恢复 READY"
        assert body["problem_code"] is None
        assert assert_rfc3339_utc(
            body["effective_loaded_at"], field_name="effective_loaded_at"
        ) == recovered_at
        assert assert_rfc3339_utc(
            body["last_storage_success_at"], field_name="last_storage_success_at"
        ) == recovered_at
        assert assert_rfc3339_utc(
            body["problem_since"], field_name="problem_since"
        ) == fault_at, "episode 未结束前 problem_since 必须保留"

        # ready 未稳定 60 秒：process 不得清空 problem_since
        clock.advance(seconds=RECOVERY_STABILITY_SECONDS - 1)
        await runtime.process_observability()
        body = (await client.get(STATUS_PATH, headers=auth_headers(READER_TOKEN))).json()
        assert assert_rfc3339_utc(
            body["problem_since"], field_name="problem_since"
        ) == fault_at, "59 秒稳定期未满不得结束故障 episode"

        # 再推进 1 秒达到 60 秒稳定：下一次 process 结束 episode
        clock.advance(seconds=1)
        await runtime.process_observability()
        body = (await client.get(STATUS_PATH, headers=auth_headers(READER_TOKEN))).json()
        assert body["status"] == "ready"
        assert body["problem_since"] is None, "60 秒稳定后 episode 必须结束"


async def test_status_broken_storage_returns_200_without_io_or_counter_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """存储方法调用即抛时：status 仍 200，存储调用计数与既有计数不变。"""
    clock = FakeUtcClock()
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, runtime, _manager = await prepare_control_plane(
        monkeypatch, storage, runtime_kwargs=_observability_kwargs(clock)
    )

    admission = import_admission_package()
    install_singleton(monkeypatch, runtime)
    admission.adjudicate([300])  # 一次计数，证明遥测在断存储前后一致

    storage.break_all(RuntimeError("all storage io forbidden"))

    async with asgi_client(app) as client:
        fetch_before = storage.fetch_calls
        cas_before = len(storage.cas_calls)
        response = await client.get(STATUS_PATH, headers=auth_headers(READER_TOKEN))

    assert response.status_code == 200, response.text
    assert storage.fetch_calls == fetch_before, "status 触发存储读取"
    assert len(storage.cas_calls) == cas_before, "status 触发存储写入"

    body = response.json()
    assert_status_exact_shape(body)
    assert body["status"] == "ready"
    telemetry = body["telemetry"]
    assert_telemetry_closed_maps(telemetry, total=1)
    assert telemetry["by_reason_code"]["policy_admitted"] == 1
    build_extended_canary_bundle().assert_no_leaks(
        body, context="断存储后的 status 响应体"
    )


async def test_status_after_put_success_updates_storage_success_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CAS 成功写入同样进入观测链：last_storage_success_at 更新、配置时间前移。"""
    clock = FakeUtcClock()
    storage = AdmissionStorageFake(stored_policy(1, atomic_policy("blacklist", [200])))
    app, _runtime, _manager = await prepare_control_plane(
        monkeypatch, storage, runtime_kwargs=_observability_kwargs(clock)
    )

    clock.advance(seconds=15)
    cas_at = clock.now

    async with asgi_client(app) as client:
        response = await client.put(
            POLICY_PATH,
            headers=put_headers(WRITER_TOKEN, if_match='"1"'),
            json=atomic_policy("whitelist", [700]),
        )
        assert response.status_code == 200, response.text

        body = (await client.get(STATUS_PATH, headers=auth_headers(READER_TOKEN))).json()

    assert_status_exact_shape(body)
    assert body["status"] == "ready"
    assert body["configured_revision"] == 2
    assert body["effective_revision"] == 2
    assert assert_rfc3339_utc(
        body["last_storage_success_at"], field_name="last_storage_success_at"
    ) == cas_at, "CAS 成功必须计入存储成功时间"
    assert assert_rfc3339_utc(
        body["configured_updated_at"], field_name="configured_updated_at"
    ) == stored_policy(2, {}).updated_at
    assert body["problem_since"] is None
