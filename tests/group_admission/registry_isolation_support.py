"""TSK-253 收敛 group_admission 测试 registry/lifespan 隔离唯一真源。

``registry_isolation_context`` 是 tests/group_admission/ 内对 NoneBot 全局
registry（``nonebot.matcher.matchers`` 与 nonebot.message 四组 pre/post 处
理器集合）与 Driver lifespan 列表（startup / ready / shutdown）保存与恢复的
**唯一真源**。其他测试文件不得重新实现这些容器的恢复写操作（由
``test_dependency_boundary.py`` 的 AST census 守护）。

契约：

- 同步、异常安全、可嵌套的 context manager：``with`` 语句即可使用，无 I/O、
  无事件循环操作，可在异步用例内嵌套使用；
- 进入时对 8 个被管理容器统一**快照并清空**——隔离语义：context 内新注册
  的 handler / hook 是唯一可观测状态，配合 event_gate / lifecycle 的包重载
  编排（生产 import 期只注册一次）实现「恰一个」断言；退出时（正常 / 异
  常）精确恢复；
- 恢复机制：dict/set 以 ``clear`` + ``update`` 回填、三个 lifespan list 以
  **原地切片**（``lst[:] = ...``）恢复，全部容器身份（``is``）保持不变、
  list 顺序保持不变；
- 支持嵌套：内层退出恢复外层入口状态，外层退出恢复初始状态；
- 本模块只负责 registry/lifespan 容器的保存与恢复，**不**接管
  ``sys.modules``、不 reload、不注入 scheduler/runtime fake——模块弹出 /
  包重载 / fake 注入等编排职责留在调用方（``entry_gate_support`` /
  ``lifecycle_support``），由 AST census 守护（不 import
  importlib/nonebot_plugin_apscheduler/pytest、不访问 sys.modules、不使用
  monkeypatch）。
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

#: dict-like 槽位（``clear`` + ``update`` 恢复，身份保持）。
_DICT_LIKE_SLOTS: tuple[str, ...] = ("matchers",)
#: set 槽位（``clear`` + ``update`` 恢复，身份保持）。
_SET_SLOTS: tuple[str, ...] = ("event_pre", "event_post", "run_pre", "run_post")
#: list 槽位（原地切片恢复，禁止属性重绑）。
_LIST_SLOTS: tuple[str, ...] = ("startup", "ready", "shutdown")


def _managed_containers() -> dict[str, Any]:
    """返回 8 个被管理容器的真实引用。

    延迟获取 nonebot 对象，避免模块导入期触碰未初始化的 Driver。
    """
    import nonebot.matcher as matcher_mod
    import nonebot.message as msg_mod
    from nonebot import get_driver

    lifespan = get_driver()._lifespan
    return {
        "matchers": matcher_mod.matchers,
        "event_pre": msg_mod._event_preprocessors,
        "event_post": msg_mod._event_postprocessors,
        "run_pre": msg_mod._run_preprocessors,
        "run_post": msg_mod._run_postprocessors,
        "startup": lifespan._startup_funcs,
        "ready": lifespan._ready_funcs,
        "shutdown": lifespan._shutdown_funcs,
    }


def _capture_snapshot(containers: dict[str, Any]) -> dict[str, Any]:
    """读取全部槽位内容快照（dict 保留键值、list 保留顺序）。"""
    return {
        "matchers": dict(containers["matchers"].items()),
        "event_pre": set(containers["event_pre"]),
        "event_post": set(containers["event_post"]),
        "run_pre": set(containers["run_pre"]),
        "run_post": set(containers["run_post"]),
        "startup": list(containers["startup"]),
        "ready": list(containers["ready"]),
        "shutdown": list(containers["shutdown"]),
    }


def _clear_containers(containers: dict[str, Any]) -> None:
    """进入时清空全部被管理容器（隔离：context 内从空状态开始观测）。"""
    for name in _DICT_LIKE_SLOTS + _SET_SLOTS + _LIST_SLOTS:
        containers[name].clear()


def _restore_snapshot(containers: dict[str, Any], snapshot: dict[str, Any]) -> None:
    """精确恢复全部被管理容器至快照状态。

    dict/set 以 ``clear`` + ``update`` 回填（身份保持）；list 以原地切片
    （``lst[:] = ...``）恢复（身份与顺序保持，禁止属性重绑）。
    """
    matchers = containers["matchers"]
    matchers.clear()
    matchers.update(snapshot["matchers"])
    for name in _SET_SLOTS:
        container = containers[name]
        container.clear()
        container.update(snapshot[name])
    for name in _LIST_SLOTS:
        containers[name][:] = snapshot[name]


@contextlib.contextmanager
def registry_isolation_context() -> Iterator[None]:
    """同步异常安全 context manager：统一保存/恢复 8 个 registry/lifespan 槽位。

    进入：快照全部 8 个被管理容器并清空；退出（正常 / 异常）：精确恢复快
    照（dict/set ``clear``+``update``、list 原地切片，身份与顺序保持）。支
    持嵌套：内层退出恢复外层入口状态，外层退出恢复初始状态。
    """
    containers = _managed_containers()
    snapshot = _capture_snapshot(containers)
    _clear_containers(containers)
    try:
        yield
    finally:
        _restore_snapshot(containers, snapshot)
