"""TSK-248 生产生命周期装配验收（AC1/AC2/AC6/AC7）。

生产 seam：NoneBot Driver 注册的 startup/shutdown hook 集合；经**真实 hook
调用**观察 group_admission 顶层 ``get_runtime_state`` / ``adjudicate`` 可观察
行为。全程不调用测试 support 的 ``runtime.start()`` 冒充生产生命周期（AC7）。

- AC1 生产 startup 恰一次获取 manager 并启动 runtime，合法策略 ready；
- AC2 startup 存储失败 / 初始化异常收敛 failed，不中止 NoneBot；群业务故障
  关闭；
- AC6 shutdown 恰一次停周期任务并 close；无 listener/task/reference 残留；
- AC7 真实 driver lifecycle 证明生产 runtime 启动（本文件全部用例只经
  driver hooks 驱动启动）。

红基线：当前生产代码尚未装配 driver lifespan hooks 与 scheduler job，本文件
用例因 ``lifecycle_context`` 捕获到的 ``startup_hooks`` / ``shutdown_hooks``
为空、scheduler 零注册而失败（red）。
"""

from __future__ import annotations

import pytest

from komari_bot.plugins.group_admission.config_schema import (
    GroupAdmissionConfigSchema,
)
from tests.group_admission.lifecycle_support import (
    invoke_hook,
    lifecycle_context,
    require_single_shutdown_hook,
    require_single_startup_hook,
)
from tests.group_admission.runtime_support import (
    AdmissionStorageFake,
    import_admission_package,
    stored_policy,
)

pytestmark = pytest.mark.group_admission_acceptance

BLACKLIST_EMPTY: dict[str, object] = {"mode": "blacklist", "group_ids": []}
BLACKLIST_200: dict[str, object] = {"mode": "blacklist", "group_ids": [200]}

_GETTER_EXPECTED = [("group_admission", GroupAdmissionConfigSchema)]


# ---------------------------------------------------------------------------
# AC1 / AC7：真实 driver startup hook 恰一次获取 manager 并启动 runtime
# ---------------------------------------------------------------------------


async def test_startup_via_driver_hook_reaches_ready_and_single_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1/AC7：经真实 driver startup hook 启动，合法策略 ready；单飞不重复。

    - 只经 ``driver._lifespan`` 注册的钩子启动（无手工 ``runtime.start()``）；
    - 恰好一个 startup / shutdown 生产钩子被注册；
    - 启动后顶层 ``get_runtime_state`` 为 ready，getter 恰一次收到精确资源名 /
      Schema，存储恰一次 fetch；
    - 再次调用 startup 钩子不重复获取 / 初始化（getter 调用记录与 fetch 计数
      不变、状态不回退）。
    """
    storage = AdmissionStorageFake(stored_policy(1, BLACKLIST_EMPTY))
    async with lifecycle_context(monkeypatch, storage) as ctx:
        startup_hook = require_single_startup_hook(ctx)
        require_single_shutdown_hook(ctx)
        assert startup_hook.__module__.startswith(
            "komari_bot.plugins.group_admission"
        ), f"生产 startup hook 必须属于 group_admission 包: {startup_hook}"
        assert ctx.runtime is not None

        # 启动前：运行时尚未启动（初始故障关闭态）——manager 由 startup 钩子
        # 延迟创建并启动，不提前预热。
        admission = import_admission_package()
        before = admission.get_runtime_state()
        assert before.status is admission.AdmissionRuntimeStatus.FAILED
        assert before.effective_revision is None

        await invoke_hook(startup_hook)

        state = admission.get_runtime_state()
        assert state.status is admission.AdmissionRuntimeStatus.READY
        assert state.problem_code is None
        assert state.configured_revision == 1
        assert state.effective_revision == 1
        assert state.using_last_known_good is False
        assert state.is_ready is True
        # getter 经 config_manager 顶层唯一注册表恰一次获取，资源名与 Schema 精确。
        assert ctx.config_manager_calls == _GETTER_EXPECTED, (
            f"startup 必须经顶层 get_config_manager 恰一次获取 manager，"
            f"实际 {ctx.config_manager_calls}"
        )
        assert storage.fetch_calls == 1, "启动必须恰一次初始化/读取存储"

        # 再次调用 startup hook：单飞，不重复获取 / 初始化。
        await invoke_hook(startup_hook)
        after = admission.get_runtime_state()
        assert after.status is admission.AdmissionRuntimeStatus.READY
        assert after.configured_revision == 1
        assert ctx.config_manager_calls == _GETTER_EXPECTED, (
            f"重复 startup 不得重新获取 manager，实际 {ctx.config_manager_calls}"
        )
        assert storage.fetch_calls == 1, "重复 startup 不得重新初始化存储"


# ---------------------------------------------------------------------------
# AC2：startup 存储失败 / 初始化异常收敛 failed，不中止 NoneBot
# ---------------------------------------------------------------------------


async def test_startup_storage_failure_fails_closed_without_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2：冷启动存储异常经 driver hook 收敛 failed，不向 driver 冒泡。

    群业务故障关闭（``adjudicate`` 拒绝 / ``effective_policy_unavailable``），
    但控制面技术清理仍可进入（``technical_cleanup_granted``）。
    """
    storage = AdmissionStorageFake(
        fetch_error=RuntimeError("postgresql unavailable at cold start")
    )
    async with lifecycle_context(monkeypatch, storage) as ctx:
        startup_hook = require_single_startup_hook(ctx)

        # 不得抛出：存储失败不得中止 NoneBot 启动。
        await invoke_hook(startup_hook)

        admission = import_admission_package()
        state = admission.get_runtime_state()
        assert state.status is admission.AdmissionRuntimeStatus.FAILED
        assert state.problem_code == "storage_unavailable"
        assert state.configured_revision is None
        assert state.effective_revision is None
        assert state.using_last_known_good is False

        # 群业务故障关闭。
        result = admission.adjudicate([100])
        assert result.qualification is admission.AdmissionQualification.REJECTED
        assert result.reason_code == "effective_policy_unavailable"
        assert result.effective_revision is None

        # 控制面技术清理仍可进入（ADR-0012 封闭分类）。
        cleanup = admission.adjudicate(
            [], intent=admission.AdmissionIntent.TECHNICAL_CLEANUP
        )
        assert (
            cleanup.qualification
            is admission.AdmissionQualification.TECHNICAL_CLEANUP
        )
        assert cleanup.reason_code == "technical_cleanup_granted"


async def test_startup_manager_acquisition_error_does_not_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2：manager 获取 / 初始化异常（非存储路径）同样收敛 failed 不冒泡。

    生产 startup 钩子必须把 manager 获取（经 config_manager 顶层
    ``get_config_manager``）与启动的异常收敛为 failed（不得让通用配置预热直
    接中止进程），并保持群业务故障关闭。这里让公开 getter 直接抛错（无论生
    产如何引用 getter 都会命中），验证钩子不向 driver 冒泡。
    """
    storage = AdmissionStorageFake(stored_policy(1, BLACKLIST_EMPTY))

    async with lifecycle_context(
        monkeypatch,
        storage,
        manager_acquisition_error=ValueError("schema mismatch at startup"),
    ) as ctx:
        startup_hook = require_single_startup_hook(ctx)

        # 不得抛出：初始化异常不得中止 NoneBot 启动。
        await invoke_hook(startup_hook)

        # 错误路径同样经顶层 getter，且收到精确资源名与 Schema。
        assert ctx.config_manager_calls == _GETTER_EXPECTED, (
            f"getter 必须收到精确资源名与 Schema，实际 {ctx.config_manager_calls}"
        )

        admission = import_admission_package()
        state = admission.get_runtime_state()
        assert state.status is admission.AdmissionRuntimeStatus.FAILED
        assert state.is_ready is False

        result = admission.adjudicate([100])
        assert result.qualification is admission.AdmissionQualification.REJECTED
        assert result.reason_code == "effective_policy_unavailable"


# ---------------------------------------------------------------------------
# AC6 / AC7：shutdown 恰一次停周期任务并 close，无残留
# ---------------------------------------------------------------------------


async def test_shutdown_stops_scheduler_and_closes_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC6：经真实 driver shutdown hook 恰一次停周期任务并 close。

    - 启动后 scheduler 恰注册一个周期 job；shutdown 后该 job 恰一次注销；
    - 关闭后运行时故障关闭（effective_revision None），后续 watcher 投递不
      改变运行时（listener 已注销 → 无残留）；
    - 再次 shutdown 幂等：不再重复注销 job。
    """
    storage = AdmissionStorageFake(stored_policy(1, BLACKLIST_EMPTY))
    async with lifecycle_context(monkeypatch, storage) as ctx:
        startup_hook = require_single_startup_hook(ctx)
        await invoke_hook(startup_hook)

        # 启动后：恰一个周期 job（formal cycle）。
        assert len(ctx.scheduler.jobs) == 1, (
            f"生产必须注册恰一个周期 job，实际 {len(ctx.scheduler.jobs)}"
        )
        job = ctx.scheduler.jobs[0]
        assert ctx.scheduler.trigger_of(job) == "interval", (
            f"周期 job 必须是 interval 触发器: {ctx.scheduler.trigger_of(job)}"
        )
        interval_seconds = job["kwargs"].get("seconds")
        assert isinstance(interval_seconds, (int, float)) and interval_seconds > 0, (
            f"周期 job 必须带正数 interval seconds: {interval_seconds!r}"
        )
        job_id = job["kwargs"].get("id")
        assert isinstance(job_id, str) and job_id, "周期 job 必须带非空 id"

        # shutdown：恰一次注销 job。
        shutdown_hook = require_single_shutdown_hook(ctx)
        await invoke_hook(shutdown_hook)

        assert ctx.scheduler.removed_job_ids == [job_id], (
            f"shutdown 必须恰一次注销周期 job，实际 {ctx.scheduler.removed_job_ids}"
        )
        assert ctx.scheduler.get_job(job_id) is None

        admission = import_admission_package()
        state = admission.get_runtime_state()
        assert state.status is admission.AdmissionRuntimeStatus.FAILED
        assert state.effective_revision is None
        assert state.using_last_known_good is False

        # 无 listener 残留：close 后 watcher 再投递不得改变运行时。
        closed_state = state
        storage.deliver(stored_policy(2, BLACKLIST_200))
        assert admission.get_runtime_state() == closed_state, (
            "close 后 watcher 投递改变了运行时（listener 残留）"
        )

        # 再次 shutdown：幂等，不重复注销。
        await invoke_hook(shutdown_hook)
        assert ctx.scheduler.removed_job_ids == [job_id], (
            f"重复 shutdown 不得重复注销 job: {ctx.scheduler.removed_job_ids}"
        )
