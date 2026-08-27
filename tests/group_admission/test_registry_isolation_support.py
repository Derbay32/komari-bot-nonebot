"""TSK-253 收敛 group_admission 测试 registry 隔离唯一真源：行为验收（红基线）。

本文件为 ``tests/group_admission/registry_isolation_support.py`` 中立 helper
的行为验收。本轮只写验收测试、不实现 helper：模块尚不存在，本文件全部用例在
运行期以 ``ModuleNotFoundError`` 失败（red），精确指向「唯一真源缺失」。

约定（未来唯一真源必须满足的契约）：

- 模块路径：``tests/group_admission/registry_isolation_support.py``；
- 公开 API：同步异常安全 context manager ``registry_isolation_context()``
  （``with`` 语句即可；无 I/O、无事件循环操作，可在异步用例内嵌套使用）；
- 进入时统一快照，退出时（正常 / 异常）精确恢复以下 8 个
  registry/lifespan 槽位：
  * ``nonebot.matcher.matchers``（dict-like，``clear`` + ``update`` 恢复，
    身份保持）；
  * ``nonebot.message._event_preprocessors`` / ``_event_postprocessors`` /
    ``_run_preprocessors`` / ``_run_postprocessors``（set，``clear`` +
    ``update`` 恢复，身份保持）；
  * ``driver._lifespan._startup_funcs`` / ``_ready_funcs`` /
    ``_shutdown_funcs``（list，必须**原地切片恢复**，禁止属性重绑）；
- 恢复前后全部被管理容器身份（``is``）保持不变；
- 支持嵌套：内层退出恢复外层入口状态，外层退出恢复初始状态；
- helper 不接管 ``sys.modules``、不 reload、不注入 scheduler/runtime fake
  （该范围边界由 ``test_dependency_boundary.py`` 的 AST/census 另行守护）。

红基线精确性：本文件使用**惰性导入**（在用例内获取 helper），使文件可正常收
集、不影响依赖全量收集的 evidence/anchor 用例；每个用例独立以导入失败红，
不依赖环境、导入顺序或任何无关旧失败。行为用例只做增量写（``add`` /
``discard`` / ``append`` / ``remove`` / dict-like 键赋值删除），刻意避开
``clear`` / ``update`` / 切片赋值 / 属性重绑等恢复写模式，因此不会被依赖边
界 AST/census 误判为第二恢复真源。
"""

from __future__ import annotations

import contextlib
import importlib
from typing import Any, cast

import pytest

pytestmark = pytest.mark.group_admission_acceptance

#: matchers（dict-like）探针 priority 键基址，避开真实 priority 取值范围。
_MATCHER_KEY_BASE = 10**9


def _registry_isolation_context() -> Any:
    """惰性获取中立 helper 的 context manager（返回可调用对象）。

    helper 缺失时抛出 ``ModuleNotFoundError``（红基线精确指向「唯一真源缺
    失」）；延迟导入使本文件可正常收集，避免影响依赖全量收集的
    evidence/anchor 用例。
    """
    support = cast(
        "Any",
        importlib.import_module(
            "tests.group_admission.registry_isolation_support"
        ),
    )
    return support.registry_isolation_context


class _Probe:
    """单槽位探针：携带槽位名与代数，用于增量写入与恢复断言。"""

    __slots__ = ("gen", "name")

    def __init__(self, name: str, gen: int) -> None:
        self.name = name
        self.gen = gen


def _slot_containers() -> dict[str, Any]:
    """返回 8 个被管理容器的真实引用（只读，不触发任何恢复写模式）。"""
    import nonebot.matcher as _matcher_mod
    import nonebot.message as _msg_mod
    from nonebot import get_driver

    lifespan = get_driver()._lifespan
    return {
        "matchers": _matcher_mod.matchers,
        "event_pre": _msg_mod._event_preprocessors,
        "event_post": _msg_mod._event_postprocessors,
        "run_pre": _msg_mod._run_preprocessors,
        "run_post": _msg_mod._run_postprocessors,
        "startup": lifespan._startup_funcs,
        "ready": lifespan._ready_funcs,
        "shutdown": lifespan._shutdown_funcs,
    }


def _capture_slots() -> dict[str, Any]:
    """读取全部 8 个槽位的内容快照（list 保留顺序）。"""
    containers = _slot_containers()
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


def _apply_generation(gen: int) -> list[_Probe]:
    """向每个槽位增量加入一枚第 ``gen`` 代探针，返回探针列表。

    只使用增量写（set ``add`` / list ``append`` / dict-like 键赋值），
    刻意避开 ``clear`` / ``update`` / 切片赋值 / 属性重绑等恢复写模式。
    """
    probes: list[_Probe] = []
    containers = _slot_containers()
    for name, container in containers.items():
        probe = _Probe(name, gen)
        probes.append(probe)
        if name == "matchers":
            cast("Any", container)[_MATCHER_KEY_BASE + gen] = [probe]
        elif isinstance(container, set):
            container.add(probe)
        elif isinstance(container, list):
            container.append(probe)
    return probes


def _drop_probes(probes: list[_Probe]) -> None:
    """从对应槽位增量移除给定探针（与 ``_apply_generation`` 对称）。"""
    containers = _slot_containers()
    for probe in probes:
        container = containers[probe.name]
        if probe.name == "matchers":
            mapping = cast("Any", container)
            key = _MATCHER_KEY_BASE + probe.gen
            if key in mapping:
                del mapping[key]
        elif isinstance(container, set):
            container.discard(probe)
        elif isinstance(container, list):
            with contextlib.suppress(ValueError):
                container.remove(probe)


def test_registry_isolation_context_restores_every_slot_on_normal_exit() -> None:
    """正常退出：8 个槽位全部精确恢复（补回被移除元素 + 清除新增元素）。

    先在 context 外注入种子使入口状态非空：退出后必须恢复含种子的精确状态
    （覆盖「补回」方向），同时新增探针必须被清除（覆盖「清除」方向）。

    断言先行，``finally`` 只做增量 ``_drop_probes`` 清理：即使 helper 缺陷
    导致断言失败，种子与新增两代探针也全部清走，不残留在全局
    registry/lifespan，避免后续用例级联失败；清理不掩盖恢复错误（断言在
    finally 之前）。
    """
    context = _registry_isolation_context()
    seed_probes: list[_Probe] = []
    added_probes: list[_Probe] = []
    try:
        seed_probes = _apply_generation(0)  # 种子（入口非空）
        initial = _capture_slots()
        with context():
            _drop_probes(seed_probes)  # 移除入口即存在的元素（补回方向）
            added_probes = _apply_generation(1)  # 新增探针（清除方向）
        assert _capture_slots() == initial, (
            "正常退出后 registry/lifespan 未精确恢复（内容或顺序不一致）"
        )
    finally:
        _drop_probes(seed_probes)  # 清理种子
        _drop_probes(added_probes)  # 异常/断言失败也清理本代探针


def test_registry_isolation_context_restores_every_slot_on_exception() -> None:
    """异常退出：异常原样传播且 8 个槽位全部精确恢复。

    helper 缺陷时（未恢复 / 吞异常 / 抛错）探针不得残留在全局
    registry/lifespan：``try/finally`` 按代累积创建的全部探针并增量
    ``_drop_probes`` 清理；断言与 ``pytest.raises`` 匹配仍在清理前执行，不
    掩盖恢复错误。
    """

    class _ProbeError(Exception):
        """测试专用异常。"""

        __slots__ = ()

    context = _registry_isolation_context()
    initial = _capture_slots()
    created_probes: list[_Probe] = []
    try:
        with pytest.raises(_ProbeError, match="boom"), context():
            created_probes.extend(_apply_generation(1))
            created_probes.extend(_apply_generation(2))
            raise _ProbeError("boom")
        assert _capture_slots() == initial, (
            "异常退出后 registry/lifespan 未精确恢复（内容或顺序不一致）"
        )
    finally:
        _drop_probes(created_probes)  # 异常/断言失败也清理全部探针


def test_registry_isolation_context_supports_nesting() -> None:
    """嵌套：内层退出恢复外层入口状态，外层退出恢复初始状态（全部 8 槽位）。

    helper 缺陷导致任一层断言失败或抛错时，外层与内层两代探针都经
    ``try/finally`` 增量 ``_drop_probes`` 清理，不残留全局状态；断言先于
    finally 清理，不掩盖恢复错误。
    """
    context = _registry_isolation_context()
    initial = _capture_slots()
    outer_probes: list[_Probe] = []
    inner_probes: list[_Probe] = []
    try:
        with context():
            outer_probes = _apply_generation(1)  # 外层状态 = 初始状态 + P1
            outer_state = _capture_slots()
            with context():
                _drop_probes(outer_probes)  # 移除 P1（补回方向）
                inner_probes = _apply_generation(2)  # 新增 P2（清除方向）
            assert _capture_slots() == outer_state, (
                "内层退出必须恢复外层入口状态（P1 补回 + P2 清除）"
            )
        assert _capture_slots() == initial, "外层退出必须恢复初始状态"
    finally:
        _drop_probes(inner_probes)  # 先清内层再清外层（增量幂等，顺序无关）
        _drop_probes(outer_probes)


def test_registry_isolation_context_preserves_container_identity() -> None:
    """身份保持：恢复前后 8 个被管理容器身份（``is``）不变，不属性重绑。

    helper 缺陷导致断言失败或抛错时，本用例创建的全部探针经 ``try/finally``
    增量 ``_drop_probes`` 清理；身份断言先于 finally 清理，不掩盖重绑定错误。
    """
    context = _registry_isolation_context()
    containers = _slot_containers()
    before = dict(containers)
    created_probes: list[_Probe] = []
    try:
        with context():
            created_probes.extend(_apply_generation(1))
            created_probes.extend(_apply_generation(2))
        after = _slot_containers()
        assert set(after) == set(before)
        for name, container in before.items():
            assert after[name] is container, (
                f"{name} 容器身份被重绑（helper 不得以属性重绑替换 lifespan lists）"
            )
    finally:
        _drop_probes(created_probes)  # 异常/断言失败也清理全部探针
