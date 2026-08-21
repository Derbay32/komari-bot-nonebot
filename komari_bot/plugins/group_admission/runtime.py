"""准入运行时：进程内不可变快照聚合与 ConfigManager 版本化快照适配。

本模块是 ``group_admission`` 包内部 seam，不作顶层公开：

- 绑定 TSK-221 ``ConfigManager`` 的版本化快照基础设施：``start`` 先注册
  快照 listener 再 initialize，初始化后显式消费缓存快照，避免错过初始化
  期间经 watcher 投递的修订；并发 start 单飞；
- 只接纳严格更高修订：合法编译后 READY 且 configured/effective 同为该修
  订；更高非法修订在有最近有效策略（LKG）时 DEGRADED 保留 LKG，无 LKG
  收敛 FAILED；相同、较低或乱序修订忽略；
- 冷启动存储异常收敛 FAILED/storage_unavailable，不向调用方冒泡
  （CancelledError 保持取消语义）；不另造重试任务，ConfigManager 存储
  watcher 是唯一的后台恢复源，后续合法快照可恢复 READY；
- 状态与编译产物合并为单一 frozen 聚合，一次引用赋值原子替换；
  ``get_state()`` / ``adjudicate()`` 每次只读取一次聚合，单调用不撕裂；
- ``close`` 先从 ConfigManager 注销 listener（不持本模块锁，避免锁序死
  锁），再清空 effective/LKG/manager 引用，之后投递不再改变运行时。
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING

from .contracts import (
    AdmissionIntent,
    AdmissionQualification,
    AdmissionResult,
    AdmissionRuntimeState,
    AdmissionRuntimeStatus,
)
from .policy import (
    PolicyCompilationError,
    _CompiledPolicy,
    compile_policy,
    policy_admits,
)

if TYPE_CHECKING:
    from komari_bot.plugins.config_manager import ConfigManager, ConfigSnapshot

    from .contracts import AdmissionProblemCode, AdmissionReasonCode


@dataclass(frozen=True, slots=True)
class _RuntimeAggregate:
    """运行时状态与生效策略的单一 frozen 聚合。

    发布即整体替换本聚合的引用；任何读取只取一次引用，保证单调用内五元
    组状态与裁决三元组各自完整，不出现跨字段撕裂。
    """

    state: AdmissionRuntimeState
    policy: _CompiledPolicy | None


def _failed_state(problem_code: AdmissionProblemCode | None) -> AdmissionRuntimeState:
    return AdmissionRuntimeState(
        status=AdmissionRuntimeStatus.FAILED,
        problem_code=problem_code,
        configured_revision=None,
        effective_revision=None,
        using_last_known_good=False,
    )


#: 未启动 / 冷启动存储失败：故障关闭，无有效策略。
_INITIAL_AGGREGATE = _RuntimeAggregate(
    state=_failed_state("storage_unavailable"),
    policy=None,
)

#: close 后终态：无有效策略，后续投递不再改变运行时。
_CLOSED_AGGREGATE = _RuntimeAggregate(
    state=_failed_state(None),
    policy=None,
)


class _AdmissionRuntime:
    """准入运行时（包内部 seam）。

    顶层 ``adjudicate`` / ``get_runtime_state`` 在调用时委托本类实例；同
    步接口无 I/O，只读取进程内不可变聚合。
    """

    def __init__(self) -> None:
        self._aggregate: _RuntimeAggregate = _INITIAL_AGGREGATE
        self._manager: ConfigManager | None = None
        self._publish_lock = Lock()
        self._start_lock = asyncio.Lock()
        self._started = False
        self._closed = False

    # --------------------------- 生命周期 ---------------------------

    async def start(self, config_manager: ConfigManager) -> None:
        """单飞启动：先注册快照 listener，再初始化，最后显式消费缓存快照。"""
        async with self._start_lock:
            if self._started:
                return
            self._started = True
            self._manager = config_manager
            config_manager.register_snapshot_listener(self._on_snapshot)
            try:
                await config_manager.initialize_async()
            except Exception:
                self._converge_storage_failure()
            try:
                snapshot = config_manager.get_cached_versioned_snapshot()
            except RuntimeError:
                pass
            else:
                self._publish_snapshot(snapshot)

    async def close(self) -> None:
        """注销 listener 并清空快照引用；之后投递不得改变运行时。"""
        manager = self._manager
        if manager is not None:
            manager.unregister_snapshot_listener(self._on_snapshot)
        with self._publish_lock:
            self._closed = True
            self._manager = None
            self._aggregate = _CLOSED_AGGREGATE

    # --------------------------- 同步读取面 ---------------------------

    def get_state(self) -> AdmissionRuntimeState:
        """单调用原子读取运行时健康状态。"""
        return self._aggregate.state

    def adjudicate(
        self,
        associated_group_ids: Collection[int],
        *,
        intent: AdmissionIntent = AdmissionIntent.BUSINESS,
    ) -> AdmissionResult:
        """单调用原子裁决：无 I/O，只读取当前不可变聚合。"""
        aggregate = self._aggregate
        effective_revision = aggregate.state.effective_revision
        attribution = _validate_attribution(associated_group_ids)

        if intent is AdmissionIntent.TECHNICAL_CLEANUP:
            if attribution is None:
                return _rejected(effective_revision, "group_attribution_unavailable")
            return AdmissionResult(
                qualification=AdmissionQualification.TECHNICAL_CLEANUP,
                effective_revision=effective_revision,
                reason_code="technical_cleanup_granted",
            )

        if not attribution:
            return _rejected(effective_revision, "group_attribution_unavailable")

        if intent is AdmissionIntent.FACT_FINALIZATION:
            return AdmissionResult(
                qualification=AdmissionQualification.FACT_FINALIZATION,
                effective_revision=effective_revision,
                reason_code="fact_finalization_granted",
            )

        policy = aggregate.policy
        if policy is None:
            return _rejected(effective_revision, "effective_policy_unavailable")
        if policy_admits(policy, attribution):
            qualification = AdmissionQualification.BUSINESS
            reason_code: AdmissionReasonCode = "policy_admitted"
        else:
            qualification = AdmissionQualification.REJECTED
            reason_code = "policy_restricted"
        return AdmissionResult(
            qualification=qualification,
            effective_revision=effective_revision,
            reason_code=reason_code,
        )

    # --------------------------- 快照发布 ---------------------------

    def _on_snapshot(self, snapshot: ConfigSnapshot) -> None:
        """ConfigManager 快照 listener：只接纳严格更高修订。"""
        self._publish_snapshot(snapshot)

    def _publish_snapshot(self, snapshot: ConfigSnapshot) -> None:
        with self._publish_lock:
            if self._closed:
                return
            configured = self._aggregate.state.configured_revision
            if configured is not None and snapshot.revision <= configured:
                return
            payload = getattr(snapshot.value, "policy", None)
            try:
                policy = compile_policy(payload)
            except PolicyCompilationError:
                self._publish_invalid_locked(snapshot.revision, "stored_policy_invalid")
                return
            except Exception:
                self._publish_invalid_locked(snapshot.revision, "snapshot_publish_failed")
                return
            self._aggregate = _RuntimeAggregate(
                state=AdmissionRuntimeState(
                    status=AdmissionRuntimeStatus.READY,
                    problem_code=None,
                    configured_revision=snapshot.revision,
                    effective_revision=snapshot.revision,
                    using_last_known_good=False,
                ),
                policy=policy,
            )

    def _publish_invalid_locked(self, revision: int, problem_code: AdmissionProblemCode) -> None:
        """更高非法修订：有 LKG 时 DEGRADED 保留 LKG，无 LKG 时 FAILED。"""
        current = self._aggregate
        if current.policy is not None:
            state = AdmissionRuntimeState(
                status=AdmissionRuntimeStatus.DEGRADED,
                problem_code=problem_code,
                configured_revision=revision,
                effective_revision=current.state.effective_revision,
                using_last_known_good=True,
            )
            self._aggregate = _RuntimeAggregate(state=state, policy=current.policy)
            return
        state = AdmissionRuntimeState(
            status=AdmissionRuntimeStatus.FAILED,
            problem_code=problem_code,
            configured_revision=revision,
            effective_revision=None,
            using_last_known_good=False,
        )
        self._aggregate = _RuntimeAggregate(state=state, policy=None)

    def _converge_storage_failure(self) -> None:
        """冷启动存储异常收敛为故障关闭；已接纳快照不回退。"""
        with self._publish_lock:
            if self._closed or self._aggregate.state.configured_revision is not None:
                return
            self._aggregate = _INITIAL_AGGREGATE


def _rejected(
    effective_revision: int | None,
    reason_code: AdmissionReasonCode,
) -> AdmissionResult:
    return AdmissionResult(
        qualification=AdmissionQualification.REJECTED,
        effective_revision=effective_revision,
        reason_code=reason_code,
    )


def _validate_attribution(raw: object) -> frozenset[int] | None:
    """校验关联群归属：全部元素为正整数群号时返回去重集合，否则 ``None``。

    拒绝 ``None``、文本/字节序列、非 Collection，以及空集合以外的非法元
    素（bool、零、负数、非整数）；空集合本身合法，是否允许由行为目的决定。
    """
    if raw is None or isinstance(raw, (str, bytes, bytearray)):
        return None
    if not isinstance(raw, Collection):
        return None
    try:
        elements = list(raw)
    except TypeError:
        return None
    validated: set[int] = set()
    for element in elements:
        if type(element) is not int or element <= 0:
            return None
        validated.add(element)
    return frozenset(validated)


#: module singleton：顶层函数在调用时经本属性委托，不复制引用。
_runtime = _AdmissionRuntime()
