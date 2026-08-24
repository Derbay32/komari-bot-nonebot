"""TSK-248 生产生命周期 / 调度装配测试共享基础设施（测试专用，不承载生产语义）。

承载生产装配 seam 的确定性驱动：

- ``FakeScheduler``：nonebot-plugin-apscheduler 公开 scheduler 的可替换接缝
  （``add_job`` / ``remove_job`` / ``get_job`` / ``get_jobs``）。记录全部
  注册 / 注销调用，维护按 job id 去重的当前作业集，供测试断言「恰一次注册 /
  注销」并**确定性调用**已注册 job 函数验证 ``process_observability``，
  全程无真实 sleep；
- ``lifecycle_context``：以真实 NoneBot Driver 生命周期为装配源：

  1. 快照 driver lifespan startup/shutdown hook 集合、五组事件注册表与
     scheduler 引用；
  2. 替换 scheduler 为记录 fake，patch 共享 ``get_config_storage`` 存储工
     厂，并安装公开 ``get_config_manager`` getter fake（registry 语义、记录
     调用，可选注入获取抛错）；
  3. 弹出 ``group_admission`` 全部子模块（保留 ``runtime`` / ``contracts`` /
     ``policy`` / ``config_schema``，以维持运行时 singleton 注入实例与强类型
     表 / 契约类型的身份不失效），重新加载包，触发生产在 import 期注册
     driver hooks 与 scheduler job；
  4. 把测试构造的真实 ``_AdmissionRuntime``（可选时钟 / 在线 Bot / SUPERUSER
     DI）安装为 module singleton，再经**真实 driver startup hook** 启动
     （AC7：本 support 绝不调用 ``runtime.start()`` 冒充生产生命周期）；
  5. ``finally`` 精确恢复 lifespan、事件注册表、scheduler 与单例注入。

冻结的生产 seam 语义（生产实现必须满足，否则用例红）：

- 生产在 ``group_admission`` 包 import 期经 ``driver.on_startup`` /
  ``driver.on_shutdown`` 注册钩子，钩子函数 ``__module__`` 前缀为
  ``komari_bot.plugins.group_admission.``；
- 生产经 ``nonebot_plugin_apscheduler.scheduler`` 注册一个周期（interval）
  job，其函数驱动 ``process_observability``；shutdown 注销该 job（重复注销需
  容忍 ``JobLookupError``，否则第二次 shutdown 会在生产中崩溃）；
- 生产 startup 经 config_manager **顶层 ``get_config_manager``** 唯一注册表
  获取 manager（getter 恰收到资源名 ``"group_admission"`` 与
  ``GroupAdmissionConfigSchema``，全程恰一次获取；重复 startup 不重复获取 /
  初始化；**禁止**生产直接构造 ``ConfigManager``，否则 getter 调用记录为空使
  用例红）并启动运行时；存储失败 / 管理器获取或初始化异常收敛 ``failed`` 且不
  向 driver 冒泡；
- 顶层 ``get_runtime_state`` / ``adjudicate`` 在调用时经
  ``runtime._runtime`` 模块属性解析 singleton（与 TSK-222 冻结接缝一致）。
"""

from __future__ import annotations

import importlib
import sys
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    import pytest
    from pydantic import BaseModel

    from .runtime_support import AdmissionStorageFake

from nonebot import get_driver

from komari_bot.plugins.config_manager import manager as manager_module
from tests.group_admission.entry_gate_support import (
    _clear_registries,
    _restore_registries,
    snapshot_event_registries,
)
from tests.group_admission.runtime_support import (
    import_admission_package,
    import_runtime_module,
    install_singleton,
)

_PACKAGE = "komari_bot.plugins.group_admission"
_PACKAGE_PREFIX = _PACKAGE + "."

#: reload 时必须保留的子模块：运行时 singleton 的注入实例与强类型契约 /
#: 表身份不能因 reload 而失效（否则事件门禁的 ``is`` 身份检查与 SQLAlchemy
#: 表注册会破坏）。只弹出负责注册 hooks / gate 的模块（event_gate 及生产
#: 新增的 lifecycle/bootstrap 模块等）。
_KEEP_SUBMODULES = frozenset(
    {
        f"{_PACKAGE}.runtime",
        f"{_PACKAGE}.contracts",
        f"{_PACKAGE}.policy",
        f"{_PACKAGE}.config_schema",
    }
)


class FakeScheduler:
    """nonebot-plugin-apscheduler 公开 scheduler 的可替换接缝。

    记录 ``add_job`` / ``remove_job`` 全部调用，并按 job id 去重维护当前
    作业集（``replace_existing=True`` 时同 id 新注册替换旧条目）。测试断言
    恰一次注册 / 注销，并经 ``jobs`` 里的 ``func`` 确定性调用
    ``process_observability``，不真实 sleep。
    """

    def __init__(self) -> None:
        self.add_job_calls: list[dict[str, Any]] = []
        self.removed_job_ids: list[str] = []
        self.jobs: list[dict[str, Any]] = []

    def _job_id(self, kwargs: dict[str, Any]) -> str | None:
        job_id = kwargs.get("id")
        return job_id if isinstance(job_id, str) and job_id else None

    def add_job(
        self,
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "func": func,
            "args": tuple(args),
            "kwargs": dict(kwargs),
        }
        self.add_job_calls.append(record)
        job_id = self._job_id(kwargs)
        if job_id is not None and kwargs.get("replace_existing", False):
            self.jobs = [
                job for job in self.jobs if self._job_id(job["kwargs"]) != job_id
            ]
        self.jobs.append(record)
        return record

    def remove_job(self, job_id: str, *args: Any, **kwargs: Any) -> None:
        """注销 job；不存在时抛 ``JobLookupError``（与真实 MemoryJobStore 一致）。

        使 shutdown 幂等性被真实验证：生产 shutdown 必须容忍重复注销
        （像 user_ban 的 ``unregister_expiration_job`` 一样捕获
        ``JobLookupError``），否则第二次 shutdown 会在生产中崩溃。
        """
        del args, kwargs
        existing = [job for job in self.jobs if self._job_id(job["kwargs"]) == job_id]
        if not existing:
            from apscheduler.jobstores.base import JobLookupError

            raise JobLookupError(job_id)
        self.removed_job_ids.append(job_id)
        self.jobs = [
            job for job in self.jobs if self._job_id(job["kwargs"]) != job_id
        ]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        for job in self.jobs:
            if self._job_id(job["kwargs"]) == job_id:
                return job
        return None

    def get_jobs(self) -> list[dict[str, Any]]:
        return list(self.jobs)

    def trigger_of(self, record: dict[str, Any]) -> str | None:
        """解析一条 ``add_job`` 记录的触发器类型（``"interval"`` 等）。

        ``add_job(func, trigger, ...)``：``func`` 已由签名提取为第一参数，
        ``*args`` 首位即触发器；也兼容 ``trigger`` 关键字形态。
        """
        trigger = record["kwargs"].get("trigger")
        if trigger is not None:
            return str(trigger)
        args = record["args"]
        if args:
            return str(args[0])
        return None


def require_single_startup_hook(ctx: SimpleNamespace) -> Any:
    """断言生产恰注册一个 driver startup hook，返回之。

    红基线：生产尚未装配 lifecycle hook 时，用例必须以明确的「生产未注册
    恰一个 startup hook」失败，而不是对空列表取 ``[0]`` 触发 IndexError。
    """
    assert len(ctx.startup_hooks) == 1, (
        "生产未注册恰一个 group_admission driver startup hook，"
        f"实际 {len(ctx.startup_hooks)}"
    )
    return ctx.startup_hooks[0]


def require_single_shutdown_hook(ctx: SimpleNamespace) -> Any:
    """断言生产恰注册一个 driver shutdown hook，返回之（语义同
    ``require_single_startup_hook``）。"""
    assert len(ctx.shutdown_hooks) == 1, (
        "生产未注册恰一个 group_admission driver shutdown hook，"
        f"实际 {len(ctx.shutdown_hooks)}"
    )
    return ctx.shutdown_hooks[0]


def _install_config_manager_getter_fake(
    monkeypatch: pytest.MonkeyPatch,
    *,
    acquisition_error: Exception | None = None,
) -> list[tuple[str, type[BaseModel]]]:
    """把公开 ``get_config_manager`` getter 替换为记录 fake（registry 语义）。

    生产验收 seam：配置资源只能经 config_manager 顶层 ``get_config_manager``
    唯一注册表取得，**不允许**生产直接构造 ``ConfigManager``（否则 getter 调
    用记录为空，用例红）。本 fake 在 reload 前安装：同 ``plugin_name`` 复用同
    一真实 ``ConfigManager``（其存储面经 ``get_config_storage`` 指向
    ``AdmissionStorageFake``），重复获取不重建、不重初始化；
    ``acquisition_error`` 非 ``None`` 时每次获取直接抛错（AC2 管理器获取失
    败路径）。

    返回调用记录 ``[(plugin_name, config_schema), ...]``；测试断言 getter 恰
    收到 ``("group_admission", GroupAdmissionConfigSchema)`` 且只被调用一次。

    同时 patch 顶层包与 ``manager`` 子模块两处绑定：生产可能以
    ``from komari_bot.plugins.config_manager import get_config_manager``
    （import 期绑定，reload 前已 patch 故命中）或
    ``manager.get_config_manager``（调用期属性解析）引用该 getter；两处指向
    同一函数对象，一并替换保证任意引用形态都被拦截。
    """
    calls: list[tuple[str, type[BaseModel]]] = []
    cache: dict[str, manager_module.ConfigManager] = {}

    def fake_get_config_manager(
        plugin_name: str,
        config_schema: type[BaseModel],
        *,
        env_config_schema: type[BaseModel] | None = None,
    ) -> manager_module.ConfigManager:
        calls.append((plugin_name, config_schema))
        if acquisition_error is not None:
            raise acquisition_error
        manager = cache.get(plugin_name)
        if manager is None:
            manager = manager_module.ConfigManager(
                plugin_name,
                config_schema,
                env_config_schema=env_config_schema,
            )
            cache[plugin_name] = manager
        return manager

    monkeypatch.setattr(
        manager_module, "get_config_manager", fake_get_config_manager
    )
    config_manager_pkg = sys.modules.get("komari_bot.plugins.config_manager")
    if config_manager_pkg is not None:
        monkeypatch.setattr(
            config_manager_pkg,
            "get_config_manager",
            fake_get_config_manager,
        )
    return calls


def _pop_group_admission_submodules() -> None:
    """弹出全部可重载子模块（保留 ``_KEEP_SUBMODULES``）。

    同时删除包属性上的旧引用，确保 ``importlib.reload`` 后这些模块重新执行，
    从而重新注册 driver hooks / scheduler job / event gate。
    """
    pkg = sys.modules.get(_PACKAGE)
    for name in list(sys.modules):
        if not name.startswith(_PACKAGE_PREFIX):
            continue
        if name in _KEEP_SUBMODULES:
            continue
        sys.modules.pop(name, None)
        if pkg is not None:
            top_level = name[len(_PACKAGE_PREFIX):].split(".", 1)[0]
            with suppress(AttributeError):
                delattr(pkg, top_level)


@asynccontextmanager
async def lifecycle_context(
    monkeypatch: pytest.MonkeyPatch,
    storage: AdmissionStorageFake,
    *,
    runtime_kwargs: dict[str, object] | None = None,
    manager_acquisition_error: Exception | None = None,
) -> AsyncIterator[SimpleNamespace]:
    """以真实 NoneBot Driver 生命周期为装配源的确定性上下文。

    ``runtime_kwargs`` 原样透传 ``_AdmissionRuntime`` 构造器（可控 UTC 时钟 /
    在线 Bot / SUPERUSER DI），语义与 ``runtime_support`` 一致。
    ``manager_acquisition_error`` 注入公开 ``get_config_manager`` getter 的
    抛错路径（AC2 管理器获取失败），其它情况 getter 返回 registry 语义的真实
    ``ConfigManager``。

    yield 的 ``SimpleNamespace`` 字段：

    - ``runtime``：已安装为 module singleton 的真实 ``_AdmissionRuntime``
      （**未启动**，由真实 driver startup hook 启动，AC7）；
    - ``startup_hooks`` / ``shutdown_hooks``：reload 期间生产新注册的
      driver lifespan 钩子函数列表；
    - ``scheduler``：记录生产注册/注销的 ``FakeScheduler``；
    - ``jobs``：去重后的当前作业集（每条含 ``func`` / ``args`` / ``kwargs``）；
    - ``config_manager_calls``：fake getter 收到的调用记录
      ``[(plugin_name, config_schema), ...]``；断言恰一次、资源名与 Schema 精
      确。

    绝不调用 ``runtime.start()``：运行时生命周期完全由 driver hooks 驱动。
    """
    # 1. 确保包已导入（生产可能在 import 期注册 hooks；快照在导入后取）。
    import_admission_package()

    driver = get_driver()
    startup_snapshot = list(driver._lifespan._startup_funcs)
    shutdown_snapshot = list(driver._lifespan._shutdown_funcs)
    event_snapshot = snapshot_event_registries()
    _clear_registries()

    # 2. 替换 scheduler 为可记录 fake（生产 import 期绑定它）。
    apscheduler_mod: Any = sys.modules.get("nonebot_plugin_apscheduler")
    prev_scheduler = getattr(apscheduler_mod, "scheduler", None)
    fake_scheduler = FakeScheduler()
    if apscheduler_mod is not None:
        apscheduler_mod.scheduler = fake_scheduler

    # 3. 把存储工厂替换为可控 fake：fake getter 返回的真实 ConfigManager 经
    #    共享 ``get_config_storage`` 调用该 fake，承载持久配置行为。
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: storage)

    # 4. 安装公开 get_config_manager fake：生产验收 seam 是 config_manager
    #    顶层唯一注册表 getter（禁止生产直接构造 ConfigManager）。fake 为
    #    registry 语义（同 plugin_name 复用同一真实 ConfigManager），记录调
    #    用供「恰一次获取 + 精确资源名/Schema」断言；manager_acquisition_error
    #    注入获取抛错路径。
    config_manager_calls = _install_config_manager_getter_fake(
        monkeypatch,
        acquisition_error=manager_acquisition_error,
    )

    # 5. 注入 DI runtime 到（未弹出的）runtime 模块 singleton：生产 startup
    #    钩子经 ``runtime._runtime`` 解析并启动它。
    runtime_module = import_runtime_module()
    runtime = runtime_module._AdmissionRuntime(**(runtime_kwargs or {}))
    install_singleton(monkeypatch, runtime)

    # 6. 弹出可重载子模块并 reload 包，触发生产装配（hooks + scheduler job）。
    _pop_group_admission_submodules()
    try:
        importlib.reload(sys.modules[_PACKAGE])

        new_startup = [
            func
            for func in driver._lifespan._startup_funcs
            if func not in startup_snapshot
        ]
        new_shutdown = [
            func
            for func in driver._lifespan._shutdown_funcs
            if func not in shutdown_snapshot
        ]

        yield SimpleNamespace(
            runtime=runtime,
            startup_hooks=new_startup,
            shutdown_hooks=new_shutdown,
            scheduler=fake_scheduler,
            jobs=list(fake_scheduler.jobs),
            config_manager_calls=config_manager_calls,
        )
    finally:
        # 6. 精确恢复 lifespan、事件注册表与 scheduler（并恢复单例注入由
        #    monkeypatch 在测试结束时完成）。
        driver._lifespan._startup_funcs[:] = startup_snapshot
        driver._lifespan._shutdown_funcs[:] = shutdown_snapshot
        _restore_registries(event_snapshot)
        if apscheduler_mod is not None:
            apscheduler_mod.scheduler = prev_scheduler


async def invoke_hook(hook: Callable[..., Any]) -> None:
    """调用一个 driver lifespan 钩子（同步 / 异步均兼容）。"""
    result = hook()
    if result is not None and hasattr(result, "__await__"):
        await result
