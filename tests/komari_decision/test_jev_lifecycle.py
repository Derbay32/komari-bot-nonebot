"""TSK-182 判定生产生命周期的窄验收：真实导入、钩子注册与调度失败投影。

本文件只覆盖恢复契约第一道门要求的最小范围（导入/钩子注册/调度失败）。完整
生命周期矩阵（关闭排空、重复启动幂等、真实 prune 排空等）由运行时用例
``test_jev_runtime.py`` 与主票后续分派覆盖。

断言只穿过生产 seam：真实 ``komari_decision`` 插件的 driver 生命周期、真实
``nonebot_plugin_apscheduler.scheduler`` 替换点、真实 ``DecisionRuntime`` 与真实
NoneBot ``Lifespan``。外部配置管理器/Redis 只作为边界替身注入，绝不伪造健康值。
"""

from __future__ import annotations

import importlib
import sys
from typing import TYPE_CHECKING, Any

from komari_bot.plugins.komari_decision.services import jev_lifecycle
from komari_bot.plugins.komari_decision.services.jev_runtime import (
    PRUNE_JOB_ID,
    REFRESH_JOB_ID,
    DecisionRuntime,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest


class _StubManager:
    """外部配置管理器边界替身：只提供运行时读取的同步/异步面。"""

    async def get_async(self) -> object:
        return object()

    def get_cached_versioned_snapshot(self) -> object:
        msg = "config unavailable"
        raise RuntimeError(msg)


def _failing_runtime() -> DecisionRuntime:
    """真实运行时 + 确定性失败的 Redis 边界（无 DB/无配置存储依赖）。"""

    def _broken() -> Any:
        msg = "synthetic redis outage"
        raise ConnectionError(msg)

    return DecisionRuntime(
        config_manager=_StubManager(),  # type: ignore[arg-type]
        redis_factory=_broken,  # type: ignore[arg-type]
        bot_resolver=lambda _account_id: None,
    )


class _FakeScheduler:
    """``nonebot_plugin_apscheduler.scheduler`` 的记录替身。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.jobs: dict[str, dict[str, Any]] = {}
        self.removed: list[str] = []

    def add_job(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        if self.fail:
            msg = "synthetic scheduler failure"
            raise RuntimeError(msg)
        job_id = str(kwargs.get("id"))
        self.jobs[job_id] = {"func": func, "args": args, "kwargs": kwargs}

    def remove_job(self, job_id: str) -> None:
        self.removed.append(job_id)
        self.jobs.pop(job_id, None)


def _install_scheduler(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    # 生产模块在导入期绑定 ``scheduler``；替换该绑定才是真实替换点。
    monkeypatch.setattr(jev_lifecycle, "scheduler", fake)


def test_lifecycle_module_registers_exactly_one_startup_and_shutdown_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """driver 已初始化时真实导入该模块必须成功并恰好注册一对钩子。"""
    import nonebot

    driver = nonebot.get_driver()
    before_startup = list(driver._lifespan._startup_funcs)
    before_shutdown = list(driver._lifespan._shutdown_funcs)
    module_name = "komari_bot.plugins.komari_decision.services.jev_lifecycle"
    # 重新执行模块体：若包内相对导入路径错误，这里会直接 ImportError（RED）。
    if module_name in sys.modules:
        importlib.reload(sys.modules[module_name])
    else:
        importlib.import_module(module_name)
    try:
        new_startup = [
            func
            for func in driver._lifespan._startup_funcs
            if func not in before_startup
        ]
        new_shutdown = [
            func
            for func in driver._lifespan._shutdown_funcs
            if func not in before_shutdown
        ]
        assert len(new_startup) == 1, f"应恰注册一个 startup 钩子，实际 {new_startup}"
        assert len(new_shutdown) == 1, (
            f"应恰注册一个 shutdown 钩子，实际 {new_shutdown}"
        )
        assert new_startup[0].__module__ == module_name
        assert new_shutdown[0].__module__ == module_name
    finally:
        driver._lifespan._startup_funcs[:] = [
            func for func in driver._lifespan._startup_funcs if func in before_startup
        ]
        driver._lifespan._shutdown_funcs[:] = [
            func for func in driver._lifespan._shutdown_funcs if func in before_shutdown
        ]
        monkeypatch.undo()


async def test_startup_failure_does_not_abort_later_role_service_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 Lifespan 中，判定初始化失败不得阻止后续钩子执行。"""
    from nonebot.internal.driver._lifespan import Lifespan

    runtime = _failing_runtime()
    _install_scheduler(monkeypatch, _FakeScheduler())
    monkeypatch.setattr(jev_lifecycle, "get_decision_runtime", lambda: runtime)
    observed: list[str] = []

    async def _later_hook() -> None:
        observed.append("later")

    lifespan = Lifespan()
    lifespan.on_startup(jev_lifecycle._startup)
    lifespan.on_startup(_later_hook)
    try:
        await lifespan.startup()
        assert observed == ["later"], "判定初始化失败不得中断后续角色服务钩子"
        assert runtime.get_health().runtime_status == "failed"
        assert runtime.get_health().reason_code == "storage_unavailable"
    finally:
        await lifespan.shutdown()
        await runtime.close()


async def test_scheduler_failure_projects_lifecycle_failure_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """调度注册失败：安全上报、清理已装 job、投影 internal_error 且不被刷新清除。"""
    runtime = _failing_runtime()
    failing = _FakeScheduler(fail=True)
    _install_scheduler(monkeypatch, failing)
    monkeypatch.setattr(jev_lifecycle, "get_decision_runtime", lambda: runtime)
    try:
        await jev_lifecycle._startup()  # 不得抛出
        health = runtime.get_health()
        assert health.runtime_status == "failed"
        assert health.reason_code == "internal_error"
        assert failing.jobs == {}, "注册失败不得留下部分安装的 job"
        assert set(failing.removed) >= {REFRESH_JOB_ID, PRUNE_JOB_ID}

        # 普通刷新不得清除生命周期失败投影。
        await runtime.refresh()
        assert runtime.get_health().reason_code == "internal_error"

        # 真实注册成功后才清除该投影。
        working = _FakeScheduler()
        _install_scheduler(monkeypatch, working)
        await jev_lifecycle._startup()
        assert set(working.jobs) == {REFRESH_JOB_ID, PRUNE_JOB_ID}
        refresh_job = working.jobs[REFRESH_JOB_ID]["kwargs"]
        prune_job = working.jobs[PRUNE_JOB_ID]["kwargs"]
        assert "interval" in working.jobs[REFRESH_JOB_ID]["args"]
        assert refresh_job["seconds"] == 1
        assert prune_job["seconds"] == 300
        assert refresh_job["max_instances"] == 1
        assert prune_job["max_instances"] == 1
        assert refresh_job["coalesce"] is True
        assert prune_job["coalesce"] is True
        assert runtime.get_health().reason_code == "storage_unavailable", (
            "清除生命周期投影后如实回落运行时自身失败"
        )
    finally:
        await jev_lifecycle._shutdown()
        await runtime.close()
