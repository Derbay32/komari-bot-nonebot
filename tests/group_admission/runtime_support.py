"""TSK-222 运行时测试共享基础设施（测试专用，不承载生产语义）。

提供：

- ``AdmissionValueSchema``：测试定义的 permissive Pydantic value schema
  （``policy: dict[str, object]``），policy 级校验职责留给准入运行时；
  ``policy`` 默认值为合法默认原子策略 ``{"mode": "blacklist", "group_ids":
  []}``（ADR-0012：存储健康但记录缺失时初始化为「全部群获准」），用
  ``Field(default_factory=...)`` 生成新对象，不共享可变默认值；
- ``AdmissionStorageFake``：无服务配置存储 fake——没有后台轮询任务，
  捕获真实 ``ConfigManager`` 注册的 watcher 回调，由测试通过 ``deliver()``
  显式投递快照；
- ``start_runtime`` / ``install_singleton``：用真实 ``ConfigManager`` 与真实
  ``_AdmissionRuntime`` 拉起运行时，并把实例 monkeypatch 成
  ``komari_bot.plugins.group_admission.runtime._runtime`` module singleton。

生产包尚不存在时，helper 内部的惰性 import 会抛出
``ModuleNotFoundError``，使依赖它的用例以缺失生产符号的原因失败（red）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from komari_bot.plugins.config_manager import manager as manager_module
from komari_bot.plugins.config_manager.manager import ConfigManager
from komari_bot.plugins.config_manager.storage import StoredConfig

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable

    import pytest

PLUGIN_NAME = "group_admission"

_UPDATED_AT_BASE = datetime(2026, 8, 8, 8, 0, 0, tzinfo=UTC)


def _default_atomic_policy() -> dict[str, object]:
    """合法默认原子策略：空黑名单 → 全部群获准（ADR-0012）。"""
    return {"mode": "blacklist", "group_ids": []}


class AdmissionValueSchema(BaseModel):
    """permissive value schema：只约束 policy 为 dict，校验归准入运行时。"""

    policy: dict[str, object] = Field(default_factory=_default_atomic_policy)


def stored_policy(revision: int, policy: object) -> StoredConfig:
    """构造一条携带完整策略对象的存储快照。"""
    return StoredConfig(
        plugin_name=PLUGIN_NAME,
        config_data={"policy": policy},
        revision=revision,
        updated_at=_UPDATED_AT_BASE + timedelta(seconds=revision),
    )


class AdmissionStorageFake:
    """无服务配置存储 fake。

    复刻真实 ``ConfigStorage`` 被 ``ConfigManager`` 消费的最小接口面
    （``register_watcher`` / ``fetch`` / ``fetch_async`` /
    ``insert_if_absent_async``），但不启动任何后台轮询任务；快照投递完全
    由测试经 ``deliver()`` 驱动，保持确定性。
    """

    def __init__(
        self,
        initial: StoredConfig | None = None,
        *,
        fetch_error: Exception | None = None,
    ) -> None:
        self._initial = initial
        self._fetch_error = fetch_error
        self.fetch_calls = 0
        self.insert_calls = 0
        self.watcher_callbacks: list[Callable[[StoredConfig], None]] = []
        self.fetch_started: asyncio.Event | None = None
        self.fetch_gate: asyncio.Event | None = None
        self._race_snapshots: list[StoredConfig] = []

    # ------------------------------ 测试驱动 ------------------------------

    def race_with(self, *snapshots: StoredConfig) -> None:
        """登记在 fetch_async 返回前经 watcher 投递的竞态快照。"""
        self._race_snapshots.extend(snapshots)

    def deliver(self, stored: StoredConfig) -> None:
        """以真实 ConfigManager watcher 身份投递一条存储快照。"""
        for callback in tuple(self.watcher_callbacks):
            callback(stored)

    # --------------------- ConfigStorage 最小接口面 ---------------------

    def register_watcher(
        self,
        plugin_name: str,
        callback: Callable[[StoredConfig], None],
        *,
        max_staleness_seconds: float,
    ) -> None:
        del plugin_name, max_staleness_seconds
        self.watcher_callbacks.append(callback)

    def fetch(self, plugin_name: str) -> StoredConfig | None:
        del plugin_name
        self.fetch_calls += 1
        if self._fetch_error is not None:
            raise self._fetch_error
        return self._initial

    async def fetch_async(self, plugin_name: str) -> StoredConfig | None:
        del plugin_name
        self.fetch_calls += 1
        if self._fetch_error is not None:
            raise self._fetch_error
        if self.fetch_started is not None:
            self.fetch_started.set()
        if self.fetch_gate is not None:
            await self.fetch_gate.wait()
        for snapshot in self._race_snapshots:
            self.deliver(snapshot)
        self._race_snapshots.clear()
        return self._initial

    async def insert_if_absent_async(
        self,
        *,
        plugin_name: str,
        config: BaseModel,
    ) -> StoredConfig:
        del plugin_name
        self.insert_calls += 1
        stored = stored_policy(1, config.model_dump().get("policy", {}))
        self._initial = stored
        return stored


def import_runtime_module() -> Any:
    """惰性 import 生产运行时模块（red 阶段在此抛 ModuleNotFoundError）。"""
    import komari_bot.plugins.group_admission.runtime as runtime_module

    return runtime_module


def import_admission_package() -> Any:
    """惰性 import 生产顶层包（red 阶段在此抛 ModuleNotFoundError）。"""
    import komari_bot.plugins.group_admission as admission

    return admission


def build_runtime(
    monkeypatch: pytest.MonkeyPatch,
    storage: AdmissionStorageFake,
) -> tuple[Any, ConfigManager]:
    """构造真实 ConfigManager 与真实 ``_AdmissionRuntime``（未启动）。"""
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: storage)
    runtime_module = import_runtime_module()
    manager = ConfigManager(PLUGIN_NAME, AdmissionValueSchema)
    runtime = runtime_module._AdmissionRuntime()
    return runtime, manager


async def start_runtime(
    monkeypatch: pytest.MonkeyPatch,
    storage: AdmissionStorageFake,
) -> tuple[Any, ConfigManager]:
    """用真实 ConfigManager 构造并启动真实 ``_AdmissionRuntime``。

    返回 ``(runtime, manager)``；不触碰 module singleton，调用方按需经
    ``install_singleton`` 显式安装。
    """
    runtime, manager = build_runtime(monkeypatch, storage)
    await runtime.start(manager)
    return runtime, manager


def install_singleton(
    monkeypatch: pytest.MonkeyPatch,
    runtime: Any,
) -> None:
    """把真实 ``_AdmissionRuntime`` 实例安装为 module singleton。

    顶层 ``adjudicate`` / ``get_runtime_state`` 必须在调用时经
    ``runtime._runtime`` 属性解析 singleton；生产不得提供
    ``_install_for_testing`` / reset hook / Fake / Protocol。
    """
    runtime_module = import_runtime_module()
    monkeypatch.setattr(runtime_module, "_runtime", runtime)
