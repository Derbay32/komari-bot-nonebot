"""TSK-222/TSK-223 运行时测试共享基础设施（测试专用，不承载生产语义）。

提供：

- ``AdmissionValueSchema``：测试定义的 permissive Pydantic value schema
  （``policy: dict[str, object]``），policy 级校验职责留给准入运行时；
  ``policy`` 默认值为合法默认原子策略 ``{"mode": "blacklist", "group_ids":
  []}``（ADR-0012：存储健康但记录缺失时初始化为「全部群获准」），用
  ``Field(default_factory=...)`` 生成新对象，不共享可变默认值；
- ``AdmissionStorageFake``：无服务配置存储 fake——没有后台轮询任务，
  捕获真实 ``ConfigManager`` 注册的 watcher 回调，由测试通过 ``deliver()``
  显式投递快照；TSK-223 扩展了严格 CAS（``update_fields_if_revision_async``）
  与逐方法错误注入 / 调用计数 / 全断裂开关，承载管理控制面 PUT 契约；
- ``start_runtime`` / ``install_singleton``：用真实 ``ConfigManager`` 与真实
  ``_AdmissionRuntime`` 拉起运行时，并把实例 monkeypatch 成
  ``komari_bot.plugins.group_admission.runtime._runtime`` module singleton；
  TSK-223 阶段 B 起 ``build_runtime`` / ``start_runtime`` 接受可选
  ``runtime_kwargs``，原样透传给 ``_AdmissionRuntime`` 构造器（可控 UTC 时钟
  / 在线 Bot 提供者 / SUPERUSERS 提供者三个可选内部 DI 关键字）。

生产包尚不存在时，helper 内部的惰性 import 会抛出
``ModuleNotFoundError``，使依赖它的用例以缺失生产符号的原因失败（red）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from komari_bot.plugins.config_manager import manager as manager_module
from komari_bot.plugins.config_manager.manager import ConfigManager
from komari_bot.plugins.config_manager.storage import StoredConfig

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable, Mapping

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


@dataclass(frozen=True, slots=True)
class AdmissionCasCall:
    """一次严格 CAS 写入调用的完整入参记录（测试断言用）。"""

    plugin_name: str
    field_names: frozenset[str]
    expected_revision: int
    config_dump: dict[str, Any] = field(default_factory=dict)


class AdmissionStorageFake:
    """无服务配置存储 fake。

    复刻真实 ``ConfigStorage`` 被 ``ConfigManager`` 消费的接口面
    （``register_watcher`` / ``fetch`` / ``fetch_async`` /
    ``insert_if_absent_async`` / ``update_if_unchanged_async`` /
    ``update_fields_if_revision_async``），但不启动任何后台轮询任务；快照
    投递完全由测试经 ``deliver()`` 驱动，保持确定性。

    TSK-223 控制面契约依赖的错误注入开关（全部可变、逐方法独立）：

    - ``fetch_error`` / ``insert_error`` / ``cas_error``：单方法抛错；
    - ``force_cas_conflict``：CAS 恒返回 ``None``（revision 冲突）；
    - ``break_all(error)``：所有读写方法调用即抛，用于证明同步读取面无隐
      藏 I/O；``restore_all()`` 恢复。

    CAS 语义与真实存储一致：仅当 ``expected_revision`` 等于当前存储
    revision 时原子写入并返回 ``revision + 1`` 的新快照，否则返回 ``None``；
    每次调用都完整记录进 ``cas_calls`` 供 exactly-once 断言。
    """

    def __init__(
        self,
        initial: StoredConfig | None = None,
        *,
        fetch_error: Exception | None = None,
    ) -> None:
        self._initial = initial
        self.fetch_error = fetch_error
        self.insert_error: Exception | None = None
        self.cas_error: Exception | None = None
        self.force_cas_conflict = False
        self.broken_error: Exception | None = None
        self.fetch_calls = 0
        self.insert_calls = 0
        self.update_if_unchanged_calls = 0
        self.cas_calls: list[AdmissionCasCall] = []
        self.watcher_callbacks: list[Callable[[StoredConfig], None]] = []
        self.fetch_started: asyncio.Event | None = None
        self.fetch_gate: asyncio.Event | None = None
        self._race_snapshots: list[StoredConfig] = []

    # ------------------------------ 测试驱动 ------------------------------

    def race_with(self, *snapshots: StoredConfig) -> None:
        """登记在 fetch_async 返回前经 watcher 投递的竞态快照。"""
        self._race_snapshots.extend(snapshots)

    def deliver(self, stored: StoredConfig) -> None:
        """以真实 ConfigManager watcher 身份投递一条存储快照。

        watcher 因存储行已变更而触发，因此先更新存储当前状态再回调，
        与真实存储「先提交、后通知」的顺序一致；后续 fetch/CAS 必须能观察
        到该 revision。
        """
        self._initial = stored
        for callback in tuple(self.watcher_callbacks):
            callback(stored)

    def set_stored(self, stored: StoredConfig) -> None:
        """模拟外部受支持写入：直接替换存储快照，不经 watcher 投递。

        控制面 GET 必须经真实持久刷新观察到该变化；只读缓存/LKG 的实现
        在此显形（red）。
        """
        self._initial = stored

    def break_all(self, error: Exception) -> None:
        """令全部读写方法调用即抛（证明同步面无隐藏 I/O）。"""
        self.broken_error = error

    def restore_all(self) -> None:
        """清除全断裂开关（单方法错误注入保持原值）。"""
        self.broken_error = None

    def _raise_if_broken(self) -> None:
        if self.broken_error is not None:
            raise self.broken_error

    @property
    def current_revision(self) -> int:
        """当前存储快照 revision（无记录为 0）。"""
        return self._initial.revision if self._initial is not None else 0

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
        self._raise_if_broken()
        if self.fetch_error is not None:
            raise self.fetch_error
        return self._initial

    async def fetch_async(self, plugin_name: str) -> StoredConfig | None:
        del plugin_name
        self.fetch_calls += 1
        self._raise_if_broken()
        if self.fetch_error is not None:
            raise self.fetch_error
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
        self._raise_if_broken()
        if self.insert_error is not None:
            raise self.insert_error
        stored = stored_policy(1, config.model_dump().get("policy", {}))
        self._initial = stored
        return stored

    async def update_if_unchanged_async(
        self,
        *,
        plugin_name: str,
        config: BaseModel,
        expected_updated_at: datetime,
    ) -> StoredConfig | None:
        """按 ``updated_at`` 的整份配置 CAS（归一化同步路径使用）。"""
        del plugin_name
        self.update_if_unchanged_calls += 1
        self._raise_if_broken()
        current = self._initial
        if current is None or current.updated_at != expected_updated_at:
            return None
        new_revision = current.revision + 1
        stored = StoredConfig(
            plugin_name=PLUGIN_NAME,
            config_data=config.model_dump(mode="json"),
            revision=new_revision,
            updated_at=_UPDATED_AT_BASE + timedelta(seconds=new_revision),
        )
        self._initial = stored
        return stored

    async def update_fields_if_revision_async(
        self,
        *,
        plugin_name: str,
        config: BaseModel,
        field_names: set[str],
        expected_revision: int,
    ) -> StoredConfig | None:
        """严格 CAS：revision 匹配时恰一次原子写入，否则返回 ``None``。

        调用记录先于 ``cas_error`` 注入生效：写失败同样要能断言「恰好一次
        调用」契约。
        """
        self._raise_if_broken()
        self.cas_calls.append(
            AdmissionCasCall(
                plugin_name=plugin_name,
                field_names=frozenset(field_names),
                expected_revision=expected_revision,
                config_dump=config.model_dump(mode="json"),
            )
        )
        if self.cas_error is not None:
            raise self.cas_error
        if self.force_cas_conflict:
            return None
        if expected_revision != self.current_revision:
            return None
        new_revision = expected_revision + 1
        stored = StoredConfig(
            plugin_name=plugin_name,
            config_data=config.model_dump(mode="json"),
            revision=new_revision,
            updated_at=_UPDATED_AT_BASE + timedelta(seconds=new_revision),
        )
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
    *,
    runtime_kwargs: Mapping[str, object] | None = None,
) -> tuple[Any, ConfigManager]:
    """构造真实 ConfigManager 与真实 ``_AdmissionRuntime``（未启动）。

    TSK-223 阶段 B：``runtime_kwargs`` 原样传入 ``_AdmissionRuntime`` 构
    造器。冻结的生产内部 DI 接缝（全部可选关键字，不是测试 hook，缺省时生
    产使用真实默认值）：

    - ``clock``：可控 UTC 时钟 callable（返回带时区 UTC datetime），驱动窗口/
      提醒/稳定恢复与全部状态时间戳；
    - ``online_bots_provider``：在线 Bot 枚举提供者（返回 Mapping 或
      Iterable），仅用于 SUPERUSER 通知投递；
    - ``superusers_provider``：SUPERUSER 群号提供者（返回 Iterable，仅合法
      正整数生效），仅用于通知收件人解析。
    """
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: storage)
    # ``ConfigManager`` 自身经 ``manager`` 模块绑定 ``get_config_storage``
    # （``manager.py`` 直接 ``from .storage import get_config_storage``），故
    # 只需 patch ``manager_module``；包级 ``get_config_storage`` 绑定依赖包
    # ``__init__`` 顶层 ``require('nonebot_plugin_orm')``，在测试导入期 NoneBot
    # 未就绪时会中断导入，不能依赖，故不 patch 包级绑定。
    runtime_module = import_runtime_module()
    manager = ConfigManager(PLUGIN_NAME, AdmissionValueSchema)
    runtime = runtime_module._AdmissionRuntime(**(runtime_kwargs or {}))
    return runtime, manager


async def start_runtime(
    monkeypatch: pytest.MonkeyPatch,
    storage: AdmissionStorageFake,
    *,
    runtime_kwargs: Mapping[str, object] | None = None,
) -> tuple[Any, ConfigManager]:
    """用真实 ConfigManager 构造并启动真实 ``_AdmissionRuntime``。

    返回 ``(runtime, manager)``；不触碰 module singleton，调用方按需经
    ``install_singleton`` 显式安装。``runtime_kwargs`` 语义见 ``build_runtime``。
    """
    runtime, manager = build_runtime(
        monkeypatch, storage, runtime_kwargs=runtime_kwargs
    )
    await runtime.start(manager)
    return runtime, manager


def install_singleton(
    monkeypatch: pytest.MonkeyPatch,
    runtime: Any,
) -> None:
    """把真实 ``_AdmissionRuntime`` 实例安装为 module singleton。

    顶层 ``adjudicate`` / ``get_runtime_state`` 与 TSK-223 管理控制面必须
    在调用时经 ``runtime._runtime`` 属性解析 singleton；生产不得提供
    ``_install_for_testing`` / reset hook / Fake / Protocol。
    """
    runtime_module = import_runtime_module()
    monkeypatch.setattr(runtime_module, "_runtime", runtime)


def detach_runtime_listener(
    manager: ConfigManager,
    runtime: Any,
) -> None:
    """注销运行时自己注册的快照 listener，模拟「已持久化但未本地发布」。

    只摘除 ``runtime._on_snapshot`` 这一个回调；其他 listener（例如控制面
    自行注册的）保持不动。摘除后严格 CAS 成功不会把新修订发布进运行时，
    用于验收 ``snapshot_publish_failed`` 契约。
    """
    manager.unregister_snapshot_listener(runtime._on_snapshot)
