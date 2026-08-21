"""TSK-222：``_AdmissionRuntime`` 生命周期与快照发布验收（module-owned）。

本文件约束 ``komari_bot.plugins.group_admission.runtime`` 的内部
``_AdmissionRuntime`` seam（package 内部 Adapter，不作顶层公开）：

- ``start(config_manager)``：listener 在 initialize 前注册；并发 start
  单飞且只产生一次 fetch；初始化失败收敛为 FAILED 不向调用方抛；之后真实
  ConfigManager watcher 投递合法快照可恢复 READY；健康存储无记录时经真实
  ``insert_if_absent_async`` 恰一次初始化默认原子策略（空黑名单，全群获准）；
- ``close()``：清空 effective/LKG 引用，之后 watcher 再投递不得改变运行时；
- DEGRADED 保留进程内 LKG：更高非法 persisted revision（非法 mode、非法
  group_ids、额外未知字段等参数化矩阵，确保编译器不只检查 mode）只更新
  configured_revision，非业务 intent 仍按 LKG 授各自资格；
  冷启动非法策略参数化矩阵收敛 FAILED；
  valid strict-higher snapshot 发布后 READY；
  相同/较低/乱序 revision 不回退或重复发布；
- 单调用原子性：每次 ``get_state()`` 单调用的五元组与每次 ``adjudicate()``
  单调用的三元组各自只能是完整旧相或完整新相，不撕裂；两个独立同步调用之间
  不假设任何跨调用事务，发布合法地夹在它们之间不构成违规。

使用真实 TSK-221 ``ConfigManager`` + 无服务存储 fake；并发用例只用
Barrier/Event 与有界 timeout，不真实 sleep，线程异常显式捕获；reader
task 的非 CancelledError 异常在 done 回调 + 结束时 gather 全量归并，
不只等 gate 超时、不留下 unretrieved exception。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from komari_bot.plugins.config_manager import manager as manager_module
from tests.group_admission.runtime_support import (
    AdmissionStorageFake,
    build_runtime,
    import_admission_package,
    install_singleton,
    start_runtime,
    stored_policy,
)

if TYPE_CHECKING:
    from pydantic import BaseModel

pytestmark = pytest.mark.group_admission_acceptance

BLACKLIST_EMPTY: dict[str, object] = {"mode": "blacklist", "group_ids": []}
BLACKLIST_200: dict[str, object] = {"mode": "blacklist", "group_ids": [200]}
WHITELIST_700: dict[str, object] = {"mode": "whitelist", "group_ids": [700]}
WHITELIST_100: dict[str, object] = {"mode": "whitelist", "group_ids": [100]}

#: 编译非法策略矩阵（冷启动 / 更高 revision 共用）：非法 mode、缺失字段、
#: 非法 group_ids 容器、非法元素、额外未知字段。schema 级非 dict 用例不在此列，
#: 那由 ConfigManager 拦截，运行时状态不变（见
#: test_schema_invalid_snapshot_does_not_reach_runtime）。
INVALID_POLICY_CASES: list[tuple[dict[str, object], str]] = [
    ({"mode": "graylist", "group_ids": []}, "invalid-mode"),
    ({"group_ids": []}, "missing-mode"),
    ({"mode": "blacklist"}, "missing-group-ids"),
    ({"mode": "blacklist", "group_ids": "200"}, "group-ids-string"),
    ({"mode": "whitelist", "group_ids": 700}, "group-ids-int"),
    ({"mode": "whitelist", "group_ids": {"700": True}}, "group-ids-dict"),
    ({"mode": "blacklist", "group_ids": [True]}, "element-bool"),
    ({"mode": "blacklist", "group_ids": [0]}, "element-zero"),
    ({"mode": "blacklist", "group_ids": [-5]}, "element-negative"),
    ({"mode": "whitelist", "group_ids": ["700"]}, "element-string"),
    ({"mode": "whitelist", "group_ids": [1.5]}, "element-non-integer"),
    (
        {"mode": "blacklist", "group_ids": [], "unknown_field": True},
        "extra-unknown-field",
    ),
]

_TIMEOUT_SECONDS = 5.0
_READER_COUNT = 3
_OBSERVATIONS_PER_PHASE = 10
_PROBE_GROUP_ID = 800


def _status(admission: Any, wire: str) -> Any:
    return next(m for m in admission.AdmissionRuntimeStatus if m.value == wire)


# ---------------------------------------------------------------------------
# start：初始化、单飞、失败收敛与恢复
# ---------------------------------------------------------------------------


async def test_start_with_initial_snapshot_reports_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = AdmissionStorageFake(stored_policy(1, BLACKLIST_EMPTY))
    runtime, _manager = await start_runtime(monkeypatch, storage)

    state = runtime.get_state()

    admission = import_admission_package()
    assert state.status is _status(admission, "ready")
    assert state.problem_code is None
    assert state.configured_revision == 1
    assert state.effective_revision == 1
    assert state.using_last_known_good is False
    assert state.is_ready is True


async def test_start_registers_listener_before_initialize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """initialize 期间经 watcher 投递的快照必须被 listener 捕获。

    若 listener 在 initialize 之后才注册，竞态快照的发布将被错过，运行时
    无法在 start 返回时达到 READY。
    """
    storage = AdmissionStorageFake(stored_policy(3, BLACKLIST_EMPTY))
    storage.race_with(stored_policy(5, WHITELIST_100))
    runtime, _manager = await start_runtime(monkeypatch, storage)

    state = runtime.get_state()

    admission = import_admission_package()
    assert state.status is _status(admission, "ready")
    assert state.configured_revision == 5
    assert state.effective_revision == 5
    # 竞态投递的白名单策略已生效
    result = runtime.adjudicate([200])
    assert result.qualification.value == "rejected"


async def test_concurrent_start_is_single_flight_with_one_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = AdmissionStorageFake(stored_policy(1, BLACKLIST_EMPTY))
    storage.fetch_started = asyncio.Event()
    storage.fetch_gate = asyncio.Event()
    runtime, manager = build_runtime(monkeypatch, storage)

    barrier = asyncio.Barrier(3)

    async def _starter() -> None:
        await barrier.wait()
        await runtime.start(manager)

    tasks = [asyncio.create_task(_starter()) for _ in range(3)]
    try:
        await asyncio.wait_for(storage.fetch_started.wait(), _TIMEOUT_SECONDS)
        assert storage.fetch_calls == 1, "单飞 start 期间已发生多次 fetch"
        storage.fetch_gate.set()
        await asyncio.wait_for(asyncio.gather(*tasks), _TIMEOUT_SECONDS)
    finally:
        storage.fetch_gate.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert storage.fetch_calls == 1
    assert runtime.get_state().effective_revision == 1


async def test_start_failure_converges_to_failed_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = AdmissionStorageFake(
        fetch_error=RuntimeError("postgresql unavailable at cold start")
    )
    runtime, _manager = await start_runtime(monkeypatch, storage)

    state = runtime.get_state()

    admission = import_admission_package()
    assert state.status is _status(admission, "failed")
    assert state.problem_code == "storage_unavailable"
    assert state.configured_revision is None
    assert state.effective_revision is None
    assert state.using_last_known_good is False
    assert state.is_ready is False


async def test_failed_runtime_recovers_to_ready_via_watcher_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = AdmissionStorageFake(
        fetch_error=RuntimeError("postgresql unavailable at cold start")
    )
    runtime, _manager = await start_runtime(monkeypatch, storage)
    admission = import_admission_package()
    assert runtime.get_state().status is _status(admission, "failed")
    assert storage.watcher_callbacks, "initialize 失败前必须已注册存储 watcher"

    storage.deliver(stored_policy(1, BLACKLIST_EMPTY))

    state = runtime.get_state()
    assert state.status is _status(admission, "ready")
    assert state.problem_code is None
    assert state.configured_revision == 1
    assert state.effective_revision == 1
    assert state.using_last_known_good is False


async def test_missing_record_initializes_default_policy_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """健康 storage 无记录：默认原子策略恰一次初始化（ADR-0012）。

    固定「存储健康但记录缺失 → 初始化并持久化为全部群获准」：真实
    ConfigManager 经 ``insert_if_absent_async`` 恰一次写入合法默认原子策略
    （空黑名单），运行时在 revision=1 上 READY，黑名单空集合放行全部合法群。
    本测试不创建生产 config_schema/migration，只约束运行时行为。
    """
    storage = AdmissionStorageFake(initial=None)

    def _env_config(schema: type[BaseModel]) -> BaseModel:
        # 与 tests/config_manager 同构：隔离 NoneBot driver，走 schema 默认值
        return schema()

    monkeypatch.setattr(manager_module, "get_plugin_config", _env_config)

    runtime, manager = await start_runtime(monkeypatch, storage)

    # 真实 ConfigManager 恰一次 insert，不重复初始化也不覆盖写入
    assert storage.insert_calls == 1
    snapshot = manager.get_cached_versioned_snapshot()
    assert snapshot.revision == 1
    assert snapshot.value.policy == {"mode": "blacklist", "group_ids": []}

    state = runtime.get_state()
    admission = import_admission_package()
    assert state.status is _status(admission, "ready")
    assert state.problem_code is None
    assert state.configured_revision == 1
    assert state.effective_revision == 1
    assert state.using_last_known_good is False

    # 空黑名单：全部合法群获准（「全部群获准」默认语义）
    result = runtime.adjudicate([100])
    assert result.qualification.value == "business"
    assert result.reason_code == "policy_admitted"
    assert result.effective_revision == 1

    other = runtime.adjudicate([999999])
    assert other.qualification.value == "business"
    assert other.reason_code == "policy_admitted"


# ---------------------------------------------------------------------------
# close：清理引用并隔离后续投递
# ---------------------------------------------------------------------------


async def test_close_clears_snapshot_and_ignores_later_deliveries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = AdmissionStorageFake(stored_policy(1, BLACKLIST_EMPTY))
    runtime, manager = await start_runtime(monkeypatch, storage)
    assert runtime.get_state().effective_revision == 1

    await runtime.close()
    closed_state = runtime.get_state()
    assert closed_state.effective_revision is None
    assert closed_state.using_last_known_good is False

    storage.deliver(stored_policy(2, BLACKLIST_EMPTY))

    assert runtime.get_state() == closed_state, "close 后 watcher 投递改变了运行时"
    # ConfigManager 自身生命周期独立：它仍接纳快照，只是运行时不再消费
    assert manager.get_cached_versioned_snapshot().revision == 2

    result = runtime.adjudicate([100])
    assert result.qualification.value == "rejected"
    assert result.reason_code == "effective_policy_unavailable"
    assert result.effective_revision is None


# ---------------------------------------------------------------------------
# DEGRADED / LKG 与 revision 原子发布
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "invalid_policy",
    [policy for policy, _case_id in INVALID_POLICY_CASES],
    ids=[case_id for _policy, case_id in INVALID_POLICY_CASES],
)
async def test_invalid_higher_revision_degrades_to_last_known_good(
    monkeypatch: pytest.MonkeyPatch,
    invalid_policy: dict[str, object],
) -> None:
    """更高非法 revision 参数化矩阵：编译器不只检查 mode。

    覆盖非法 mode、非法 group_ids 容器/元素与额外未知字段；manager 接纳
    permissive value snapshot（revision 2），运行时拒绝编译，DEGRADED 保留
    LKG；非业务 intent 仍按 LKG 授各自资格，不新增跨效果语义。
    """
    storage = AdmissionStorageFake(stored_policy(1, BLACKLIST_200))
    runtime, manager = await start_runtime(monkeypatch, storage)

    storage.deliver(stored_policy(2, invalid_policy))
    state = runtime.get_state()

    admission = import_admission_package()
    assert state.status is _status(admission, "degraded")
    assert state.problem_code == "stored_policy_invalid"
    assert state.configured_revision == 2
    assert state.effective_revision == 1
    assert state.using_last_known_good is True
    assert state.is_ready is False

    # manager 接纳 permissive value snapshot，拒绝编译发生在准入运行时
    assert manager.get_cached_versioned_snapshot().revision == 2

    # BUSINESS 按 LKG（blacklist [200]）继续裁决
    restricted = runtime.adjudicate([200])
    admitted = runtime.adjudicate([300])
    assert restricted.qualification.value == "rejected"
    assert restricted.reason_code == "policy_restricted"
    assert restricted.effective_revision == 1
    assert admitted.qualification.value == "business"
    assert admitted.reason_code == "policy_admitted"
    assert admitted.effective_revision == 1

    # DEGRADED + 非业务 intent：合法受限群仍授既成事实收尾资格（按 LKG）
    fact = runtime.adjudicate(
        [200], intent=admission.AdmissionIntent.FACT_FINALIZATION
    )
    assert fact.qualification is admission.AdmissionQualification.FACT_FINALIZATION
    assert fact.reason_code == "fact_finalization_granted"
    assert fact.effective_revision == 1

    # 空归属仍授技术清理资格（按 LKG）
    cleanup = runtime.adjudicate(
        [], intent=admission.AdmissionIntent.TECHNICAL_CLEANUP
    )
    assert cleanup.qualification is admission.AdmissionQualification.TECHNICAL_CLEANUP
    assert cleanup.reason_code == "technical_cleanup_granted"
    assert cleanup.effective_revision == 1


async def test_valid_strict_higher_snapshot_publishes_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = AdmissionStorageFake(stored_policy(1, WHITELIST_700))
    runtime, _manager = await start_runtime(monkeypatch, storage)
    assert runtime.adjudicate([_PROBE_GROUP_ID]).qualification.value == "rejected"

    storage.deliver(stored_policy(2, BLACKLIST_EMPTY))
    state = runtime.get_state()

    admission = import_admission_package()
    assert state.status is _status(admission, "ready")
    assert state.problem_code is None
    assert state.configured_revision == 2
    assert state.effective_revision == 2
    assert state.using_last_known_good is False

    result = runtime.adjudicate([_PROBE_GROUP_ID])
    assert result.qualification.value == "business"
    assert result.reason_code == "policy_admitted"
    assert result.effective_revision == 2


async def test_lower_or_equal_or_out_of_order_revisions_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = AdmissionStorageFake(stored_policy(2, WHITELIST_700))
    runtime, _manager = await start_runtime(monkeypatch, storage)
    state_before = runtime.get_state()
    assert state_before.effective_revision == 2

    storage.deliver(stored_policy(1, BLACKLIST_EMPTY))
    storage.deliver(stored_policy(2, BLACKLIST_EMPTY))

    assert runtime.get_state() == state_before, "较低/相同 revision 不得回退或重复发布"
    result = runtime.adjudicate([_PROBE_GROUP_ID])
    assert result.qualification.value == "rejected"
    assert result.effective_revision == 2


@pytest.mark.parametrize(
    "invalid_policy",
    [policy for policy, _case_id in INVALID_POLICY_CASES],
    ids=[case_id for _policy, case_id in INVALID_POLICY_CASES],
)
async def test_invalid_policy_at_cold_start_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    invalid_policy: dict[str, object],
) -> None:
    """冷启动非法策略参数化矩阵：故障关闭，不静默降级为默认策略。

    manager 必须接纳 permissive value snapshot（schema 级 dict 通过），
    拒绝编译发生在准入运行时：FAILED / stored_policy_invalid /
    configured_revision=N / effective None。schema 级非 dict 用例不在此列，
    那由 ConfigManager 拦截且运行时状态不变。
    """
    storage = AdmissionStorageFake(stored_policy(1, invalid_policy))
    runtime, manager = await start_runtime(monkeypatch, storage)

    # manager 接纳 permissive value snapshot（revision 1）
    assert manager.get_cached_versioned_snapshot().revision == 1

    state = runtime.get_state()

    admission = import_admission_package()
    assert state.status is _status(admission, "failed")
    assert state.problem_code == "stored_policy_invalid"
    assert state.configured_revision == 1
    assert state.effective_revision is None
    assert state.using_last_known_good is False

    result = runtime.adjudicate([100])
    assert result.qualification.value == "rejected"
    assert result.reason_code == "effective_policy_unavailable"


async def test_schema_invalid_snapshot_does_not_reach_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连 value schema 都无法通过的快照由 ConfigManager 拒绝，运行时不变。"""
    storage = AdmissionStorageFake(stored_policy(1, BLACKLIST_EMPTY))
    runtime, manager = await start_runtime(monkeypatch, storage)

    storage.deliver(stored_policy(2, "not-a-dict-policy"))

    state = runtime.get_state()
    assert state.effective_revision == 1
    assert state.configured_revision == 1
    assert manager.get_cached_versioned_snapshot().revision == 1


# ---------------------------------------------------------------------------
# 原子发布：单次调用只见完整旧/新修订（无跨调用事务假设）
# ---------------------------------------------------------------------------


async def test_concurrent_readers_observe_only_complete_revisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """每次单独同步调用只能观察到完整旧相或完整新相。

    ``get_state()`` 与 ``adjudicate()`` 是两个独立的同步调用，发布可以合法地
    夹在它们之间，因此不要求同一次迭代内这两个调用属于相同 revision；原子性
    按调用分别验证：

    - 每次 ``get_state()`` 单调用的五元组（configured/effective/status/
      using_lkg/problem_code）必须完整 old 或完整 new；
    - 每次 ``adjudicate()`` 单调用的三元组（effective_revision/qualification/
      reason_code）必须完整 old 或完整 new。

    两个通道都必须确定性看到 old/new 两相。reader task 的非
    CancelledError 异常经 done 回调立即信号化并在结束时 gather 全量归并，
    不只等 gate 超时、不留下 unretrieved exception。
    """
    storage = AdmissionStorageFake(stored_policy(1, BLACKLIST_EMPTY))
    runtime, _manager = await start_runtime(monkeypatch, storage)
    admission = import_admission_package()
    ready = _status(admission, "ready")
    business = admission.AdmissionQualification.BUSINESS
    rejected = admission.AdmissionQualification.REJECTED
    intent_business = admission.AdmissionIntent.BUSINESS

    # 两相唯一合法的单调用观察（旧：空黑名单放行；新：白名单 [700] 拒绝 800）
    old_state = (1, 1, ready, False, None)
    new_state = (2, 2, ready, False, None)
    old_result = (1, business, "policy_admitted")
    new_result = (2, rejected, "policy_restricted")

    state_observations: list[tuple[object, ...]] = []
    result_observations: list[tuple[object, ...]] = []
    phase_counts: dict[tuple[str, str], int] = {
        ("state", "old"): 0,
        ("state", "new"): 0,
        ("result", "old"): 0,
        ("result", "new"): 0,
    }
    gates = {
        ("state", "old"): asyncio.Event(),
        ("state", "new"): asyncio.Event(),
        ("result", "old"): asyncio.Event(),
        ("result", "new"): asyncio.Event(),
    }
    stop = asyncio.Event()
    reader_failed = asyncio.Event()
    reader_errors: list[BaseException] = []

    def _on_reader_done(task: asyncio.Task[None]) -> None:
        """reader 异常显式归并：不留下 unretrieved exception，并立即终止相位等待。"""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            reader_errors.append(exc)
            reader_failed.set()
            stop.set()

    def _count_phase(
        channel: str,
        observation: tuple[object, ...],
        old: tuple[object, ...],
        new: tuple[object, ...],
    ) -> None:
        if observation == old:
            phase = "old"
        elif observation == new:
            phase = "new"
        else:
            return  # 撕裂/未知观察不入相位计数，由最终断言精确报告
        key = (channel, phase)
        phase_counts[key] += 1
        if phase_counts[key] >= _OBSERVATIONS_PER_PHASE:
            gates[key].set()

    async def _reader() -> None:
        while not stop.is_set():
            state = runtime.get_state()
            state_observation = (
                state.configured_revision,
                state.effective_revision,
                state.status,
                state.using_last_known_good,
                state.problem_code,
            )
            state_observations.append(state_observation)
            _count_phase("state", state_observation, old_state, new_state)

            result = runtime.adjudicate([_PROBE_GROUP_ID], intent=intent_business)
            result_observation = (
                result.effective_revision,
                result.qualification,
                result.reason_code,
            )
            result_observations.append(result_observation)
            _count_phase("result", result_observation, old_result, new_result)
            await asyncio.sleep(0)

    readers = [asyncio.create_task(_reader()) for _ in range(_READER_COUNT)]
    for reader_task in readers:
        reader_task.add_done_callback(_on_reader_done)

    delivery_errors: list[BaseException] = []

    def _deliver_from_watcher_thread() -> None:
        try:
            storage.deliver(stored_policy(2, WHITELIST_700))
        except BaseException as exc:  # 捕获线程异常防泄漏
            delivery_errors.append(exc)

    async def _await_phase(gate: asyncio.Event) -> None:
        """等待相位门；reader 失败或有界超时立即报告，不静默挂起。"""
        gate_waiter = asyncio.create_task(gate.wait())
        failure_waiter = asyncio.create_task(reader_failed.wait())
        try:
            done, _pending = await asyncio.wait(
                {gate_waiter, failure_waiter},
                timeout=_TIMEOUT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            gate_waiter.cancel()
            failure_waiter.cancel()
            await asyncio.gather(gate_waiter, failure_waiter, return_exceptions=True)
        if not done:
            msg = f"相位门等待超时（有界退出）: {gate!r}"
            raise TimeoutError(msg)
        if reader_failed.is_set():
            msg = f"reader 任务在相位完成前失败: {reader_errors!r}"
            raise AssertionError(msg)

    reader_results: list[BaseException | None] = []
    try:
        loop = asyncio.get_running_loop()
        await _await_phase(gates[("state", "old")])
        await _await_phase(gates[("result", "old")])
        await asyncio.wait_for(
            loop.run_in_executor(None, _deliver_from_watcher_thread),
            _TIMEOUT_SECONDS,
        )
        assert delivery_errors == [], f"watcher 线程投递异常: {delivery_errors}"
        await _await_phase(gates[("state", "new")])
        await _await_phase(gates[("result", "new")])
    finally:
        stop.set()
        _done, pending = await asyncio.wait(readers, timeout=_TIMEOUT_SECONDS)
        for task in pending:
            task.cancel()
        # gather 全部 reader（含已 done 与取消的 pending）：显式归并异常，
        # 不留下 unretrieved exception
        reader_results = await asyncio.gather(*readers, return_exceptions=True)

    residual_reader_errors = [
        exc
        for exc in reader_results
        if isinstance(exc, BaseException)
        and not isinstance(exc, asyncio.CancelledError)
    ]
    assert residual_reader_errors == [], (
        f"reader 任务异常未归并: {residual_reader_errors!r}"
    )

    assert state_observations, "get_state() 未产生任何观察"
    assert result_observations, "adjudicate() 未产生任何观察"

    saw_old_state = False
    saw_new_state = False
    for observation in state_observations:
        if observation == old_state:
            saw_old_state = True
        elif observation == new_state:
            saw_new_state = True
        else:
            msg = f"单次 get_state() 调用观察到撕裂修订: {observation!r}"
            raise AssertionError(msg)
    assert saw_old_state, "状态观察缺失完整旧相"
    assert saw_new_state, "状态观察缺失完整新相"

    saw_old_result = False
    saw_new_result = False
    for observation in result_observations:
        if observation == old_result:
            saw_old_result = True
        elif observation == new_result:
            saw_new_result = True
        else:
            msg = f"单次 adjudicate() 调用观察到撕裂修订: {observation!r}"
            raise AssertionError(msg)
    assert saw_old_result, "裁决观察缺失完整旧相"
    assert saw_new_result, "裁决观察缺失完整新相"


# ---------------------------------------------------------------------------
# 顶层函数委托 module singleton
# ---------------------------------------------------------------------------


async def test_top_level_functions_delegate_to_module_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = AdmissionStorageFake(stored_policy(2, BLACKLIST_EMPTY))
    runtime, _manager = await start_runtime(monkeypatch, storage)
    install_singleton(monkeypatch, runtime)

    from komari_bot.plugins.group_admission import adjudicate, get_runtime_state

    assert get_runtime_state() == runtime.get_state()

    top_level = adjudicate([123])
    internal = runtime.adjudicate([123])
    assert top_level == internal
    assert top_level.reason_code == "policy_admitted"
    assert top_level.effective_revision == 2
