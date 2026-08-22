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

TSK-223 Phase A（runtime foundation）在本 seam 之上增加：

- 可选可控 UTC 时钟（``clock`` 关键字，缺省真实 ``datetime.now(UTC)``），
  驱动全部状态时间戳；constructor 记录 ``started_at``；
- 线程安全封闭的遥测 map（7 reason / 3 intent / 4 family / 4 problem）与
  ``adjudications_total``、``started_at``，``adjudicate`` 恰一次基础计数，
  为阶段 B 结构化日志与窗口聚合打底；
- ``configured_updated / effective_loaded / last_refresh_attempt /
  last_storage_success / problem_since`` 时间锚点；存储 / 非法 / 内部状态
  转换更新时间与 problem occurrence（本阶段不写 group_admission 结构日志）；
- 内存私有状态投影（11 顶层键 + telemetry 深拷贝，datetime 保持 aware UTC
  供 API 序列化）；
- 私有异步控制面：``_refresh_persistent_state``（GET 真实存储刷新与状态重
  收敛）与 ``_apply_persistent_update``（PUT 单次 strict CAS），使用
  runtime-owned manager，错误收敛为固定控制面异常 / code，不携带 raw 异常。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock
from typing import TYPE_CHECKING

from .contracts import (
    AdmissionIntent,
    AdmissionProblemCode,
    AdmissionQualification,
    AdmissionReasonCode,
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


# ---------------------------------------------------------------------------
# 冻结低基数遥测闭集（与 observability 冻结键集一致）
# ---------------------------------------------------------------------------

_REASON_CODES: frozenset[str] = frozenset(
    {
        "policy_admitted",
        "policy_restricted",
        "group_attribution_unavailable",
        "effective_policy_unavailable",
        "fact_finalization_granted",
        "technical_cleanup_granted",
        "private_input_rejected",
    }
)
_INTENT_CODES: frozenset[str] = frozenset(
    {"business", "fact_finalization", "technical_cleanup"}
)
_FAMILY_CODES: frozenset[str] = frozenset(
    {"message", "notice", "request", "unknown"}
)
_PROBLEM_CODES: frozenset[str] = frozenset(
    {
        "storage_unavailable",
        "stored_policy_invalid",
        "snapshot_publish_failed",
        "internal_error",
    }
)
_DEFAULT_EVENT_FAMILY = "unknown"


# ---------------------------------------------------------------------------
# 控制面固定错误 / 结果类型（阶段 A control plane，不携带 raw 异常）
# ---------------------------------------------------------------------------

_CONTROL_PLANE_RUNTIME_UNAVAILABLE = "runtime_unavailable"
_CONTROL_PLANE_STORAGE_ERROR = "storage_error"
_CONTROL_PLANE_CAS_CONFLICT = "cas_conflict"
_CONTROL_PLANE_UNKNOWN_FIELD = "unknown_field"


class _ControlPlaneError(Exception):
    """固定控制面错误类型，阶段 A control plane 内部消费；不携带 raw 存储 / 解析异常。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _ControlPlaneConflictError(_ControlPlaneError):
    """修订冲突：明确失败，由阶段 A 映射为 409，不重读、不重试。"""


@dataclass(frozen=True, slots=True)
class _ControlPlaneApplyResult:
    """一次 PUT 持久化更新的结果（成功路径；冲突以异常表达）。"""

    new_revision: int
    published_locally: bool


def _failed_state(problem_code: AdmissionProblemCode | None) -> AdmissionRuntimeState:
    return AdmissionRuntimeState(
        status=AdmissionRuntimeStatus.FAILED,
        problem_code=problem_code,
        configured_revision=None,
        effective_revision=None,
        using_last_known_good=False,
    )


@dataclass(frozen=True, slots=True)
class _RuntimeAggregate:
    """运行时状态与生效策略的单一 frozen 聚合。

    发布即整体替换本聚合的引用；任何读取只取一次引用，保证单调用内五元
    组状态与裁决三元组各自完整，不出现跨字段撕裂。
    """

    state: AdmissionRuntimeState
    policy: _CompiledPolicy | None


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
    步接口无 I/O，只读取进程内不可变聚合。控制面刷新 / 更新为私有异步方法，
    使用 ``start`` 注入并持有的 ``ConfigManager``。
    """

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._started_at = self._clock()

        self._aggregate: _RuntimeAggregate = _INITIAL_AGGREGATE
        self._manager: ConfigManager | None = None
        self._publish_lock = RLock()
        self._telemetry_lock = RLock()
        self._start_lock = asyncio.Lock()
        self._started = False
        self._closed = False

        # 时间锚点（aware UTC；未发生为 None）
        self._configured_updated_at: datetime | None = None
        self._effective_loaded_at: datetime | None = None
        self._last_refresh_attempt_at: datetime | None = None
        self._last_storage_success_at: datetime | None = None
        self._problem_since: datetime | None = None
        # 当前活跃问题 episode 的问题码；None 表示无活跃 episode。
        self._active_problem_code: AdmissionProblemCode | None = None

        # 封闭遥测 map（值始终为 int，键固定为冻结闭集）
        self._adjudications_total = 0
        self._telemetry_reason: dict[str, int] = dict.fromkeys(_REASON_CODES, 0)
        self._telemetry_intent: dict[str, int] = dict.fromkeys(_INTENT_CODES, 0)
        self._telemetry_family: dict[str, int] = dict.fromkeys(_FAMILY_CODES, 0)
        self._telemetry_problem: dict[str, int] = dict.fromkeys(_PROBLEM_CODES, 0)

    # --------------------------- 生命周期 ---------------------------

    async def start(self, config_manager: ConfigManager) -> None:
        """单飞启动：先注册快照 listener，再初始化，最后显式消费缓存快照。"""
        async with self._start_lock:
            if self._started:
                return
            self._started = True
            self._manager = config_manager
            now = self._clock()
            with self._publish_lock:
                self._last_refresh_attempt_at = now
            config_manager.register_snapshot_listener(self._on_snapshot)
            try:
                await config_manager.initialize_async()
            except Exception:
                self._converge_storage_failure()
            else:
                with self._publish_lock:
                    self._last_storage_success_at = self._clock()
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
        event_family: str = _DEFAULT_EVENT_FAMILY,
    ) -> AdmissionResult:
        """单调用原子裁决：无 I/O，只读取当前不可变聚合。

        ``event_family`` 为阶段 B 结构化日志 / 窗口聚合预留的私有接缝，默认
        ``"unknown"``；非法 family 归一到 ``"unknown"``。本阶段只做恰一次基
        础遥测计数，不触发任何日志或通知副作用。
        """
        aggregate = self._aggregate
        effective_revision = aggregate.state.effective_revision
        attribution = _validate_attribution(associated_group_ids)

        if intent is AdmissionIntent.TECHNICAL_CLEANUP:
            if attribution is None:
                result = _rejected(effective_revision, "group_attribution_unavailable")
            else:
                result = AdmissionResult(
                    qualification=AdmissionQualification.TECHNICAL_CLEANUP,
                    effective_revision=effective_revision,
                    reason_code="technical_cleanup_granted",
                )
            self._record_adjudication(result, intent, event_family)
            return result

        if not attribution:
            result = _rejected(effective_revision, "group_attribution_unavailable")
            self._record_adjudication(result, intent, event_family)
            return result

        if intent is AdmissionIntent.FACT_FINALIZATION:
            result = AdmissionResult(
                qualification=AdmissionQualification.FACT_FINALIZATION,
                effective_revision=effective_revision,
                reason_code="fact_finalization_granted",
            )
            self._record_adjudication(result, intent, event_family)
            return result

        policy = aggregate.policy
        if policy is None:
            result = _rejected(effective_revision, "effective_policy_unavailable")
            self._record_adjudication(result, intent, event_family)
            return result
        if policy_admits(policy, attribution):
            qualification = AdmissionQualification.BUSINESS
            reason_code: AdmissionReasonCode = "policy_admitted"
        else:
            qualification = AdmissionQualification.REJECTED
            reason_code = "policy_restricted"
        result = AdmissionResult(
            qualification=qualification,
            effective_revision=effective_revision,
            reason_code=reason_code,
        )
        self._record_adjudication(result, intent, event_family)
        return result

    # --------------------------- 阶段 B 接缝（本阶段静默 / 打底） ----------

    def record_private_input_rejected(self, *, event_family: str) -> None:
        """记录一次私有输入拒绝（阶段 B 结构化日志 / 窗口聚合接缝）。

        仅记账、静默：与所有裁决一致，使 ``adjudications_total += 1``、
        ``by_reason_code.private_input_rejected += 1``、``by_intent.business
        += 1``；不进入归属失败 family map（归属窗口只键于
        ``group_attribution_unavailable``）。``event_family`` 为必填关键字，
        本阶段保留以稳定阶段 B 契约，不展开族维度。
        """
        result = AdmissionResult(
            qualification=AdmissionQualification.REJECTED,
            effective_revision=self._aggregate.state.effective_revision,
            reason_code="private_input_rejected",
        )
        self._record_adjudication(result, AdmissionIntent.BUSINESS, event_family)

    # --------------------------- 快照发布 ---------------------------

    def _on_snapshot(self, snapshot: ConfigSnapshot) -> None:
        """ConfigManager 快照 listener：只接纳严格更高修订。"""
        self._publish_snapshot(snapshot)

    def _publish_snapshot(self, snapshot: ConfigSnapshot) -> None:
        """接纳严格更高修订：合法编译后 READY，否则按 LKG 收敛。"""
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
                self._publish_invalid_locked(
                    snapshot.revision, "stored_policy_invalid", snapshot.updated_at
                )
                return
            except Exception:
                self._publish_invalid_locked(
                    snapshot.revision, "snapshot_publish_failed", snapshot.updated_at
                )
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
            now = self._clock()
            self._configured_updated_at = snapshot.updated_at
            self._effective_loaded_at = now
            # problem_since 由阶段 B 稳定恢复窗口延迟清空；本阶段合法发布
            # 保留既有 problem_since，不在此立即结束 episode。

    def _publish_invalid_locked(
        self,
        revision: int,
        problem_code: AdmissionProblemCode,
        updated_at: datetime,
    ) -> None:
        """更高非法修订：有 LKG 时 DEGRADED 保留 LKG，无 LKG 时 FAILED。

        不论降级或故障关闭，``configured_updated_at`` 跟随已接纳的更高非法
        持久修订（其存储写入时间 ``updated_at``）；``effective_loaded`` 保留
        LKG，不随非法修订前移。
        """
        with self._publish_lock:
            if self._closed:
                return
            current = self._aggregate
            now = self._clock()
            if current.policy is not None:
                state = AdmissionRuntimeState(
                    status=AdmissionRuntimeStatus.DEGRADED,
                    problem_code=problem_code,
                    configured_revision=revision,
                    effective_revision=current.state.effective_revision,
                    using_last_known_good=True,
                )
                self._aggregate = _RuntimeAggregate(state=state, policy=current.policy)
            else:
                state = AdmissionRuntimeState(
                    status=AdmissionRuntimeStatus.FAILED,
                    problem_code=problem_code,
                    configured_revision=revision,
                    effective_revision=None,
                    using_last_known_good=False,
                )
                self._aggregate = _RuntimeAggregate(state=state, policy=None)
            self._configured_updated_at = updated_at
            self._mark_problem(problem_code, now)

    def _converge_storage_failure(self) -> None:
        """冷启动存储异常收敛为故障关闭；已接纳快照不回退。"""
        with self._publish_lock:
            if self._closed or self._aggregate.state.configured_revision is not None:
                return
            self._aggregate = _INITIAL_AGGREGATE
            now = self._clock()
            if self._problem_since is None:
                self._problem_since = now
            self._mark_problem("storage_unavailable", now)

    # --------------------------- 控制面：GET 持久刷新 ---------------------------

    async def _refresh_persistent_state(self) -> None:
        """控制面 GET：经 runtime-owned manager 真实读取存储并收敛状态。

        - 存储读取异常：未持有效策略时收敛 FAILED/DEGRADED（保留 LKG）；
          已 READY 不降级，继续信任当前有效快照；
        - 读取成功：合法同修订恢复 READY；非法持久策略收敛 stored_policy_invalid；
        - 不查询 manager registry、不重试、不重读冲突。
        """
        if self._manager is None or self._closed:
            return
        now = self._clock()
        with self._publish_lock:
            self._last_refresh_attempt_at = now
        try:
            await self._manager.reload_async()
        except Exception:
            self._converge_refresh_storage_error(now)
            return
        try:
            snapshot = self._manager.get_cached_versioned_snapshot()
        except RuntimeError:
            self._converge_refresh_storage_error(now)
            return
        with self._publish_lock:
            self._last_storage_success_at = self._clock()
        self._reconcile_stored(snapshot)

    def _converge_refresh_storage_error(self, now: datetime) -> None:
        """刷新存储读取失败收敛：保留 LKG 收敛 DEGRADED，无 LKG 收敛 FAILED。

        即便当前 READY，持久刷新失败也表明无法再验证最新持久修订，必须降级
        为 DEGRADED/storage_unavailable（configured/effective 沿用最近有效视
        图、using_last_known_good=True），不得继续 READY。
        """
        with self._publish_lock:
            if self._closed:
                return
            current = self._aggregate
            if current.policy is not None:
                self._aggregate = _RuntimeAggregate(
                    state=AdmissionRuntimeState(
                        status=AdmissionRuntimeStatus.DEGRADED,
                        problem_code="storage_unavailable",
                        configured_revision=current.state.configured_revision,
                        effective_revision=current.state.effective_revision,
                        using_last_known_good=True,
                    ),
                    policy=current.policy,
                )
            else:
                self._aggregate = _INITIAL_AGGREGATE
            self._mark_problem("storage_unavailable", now)

    def _reconcile_stored(self, snapshot: ConfigSnapshot) -> None:
        """依据真实存储快照重收敛运行时状态（供刷新调用）。"""
        with self._publish_lock:
            if self._closed:
                return
            configured = self._aggregate.state.configured_revision
            if configured is not None and snapshot.revision < configured:
                return  # 存储回滚：信任本地更高视图
            payload = getattr(snapshot.value, "policy", None)
            try:
                compile_policy(payload)  # 仅校验编译，合法路径下游重新编译
            except PolicyCompilationError:
                self._publish_invalid_locked(
                    snapshot.revision, "stored_policy_invalid", snapshot.updated_at
                )
                return
            except Exception:
                self._publish_invalid_locked(
                    snapshot.revision, "snapshot_publish_failed", snapshot.updated_at
                )
                return
            # 合法快照
            if self._aggregate.state.is_ready and (
                configured is None or snapshot.revision <= configured
            ):
                return  # 已 READY 且修订一致：无需变更
            if configured is not None and snapshot.revision == configured:
                self._restore_ready_locked(snapshot)
            else:
                self._publish_snapshot(snapshot)

    def _restore_ready_locked(self, snapshot: ConfigSnapshot) -> None:
        """同修订恢复 READY（严格更高由 ``_publish_snapshot`` 处理）。"""
        with self._publish_lock:
            if self._closed:
                return
            payload = getattr(snapshot.value, "policy", None)
            policy = compile_policy(payload)
            now = self._clock()
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
            self._configured_updated_at = snapshot.updated_at
            self._effective_loaded_at = now
            # problem_since 由阶段 B 稳定恢复窗口延迟清空；本阶段同修订恢复
            # READY 不立即结束 episode。

    # --------------------------- 控制面：PUT 持久更新 ---------------------------

    async def _apply_persistent_update(
        self,
        field_name: str,
        value: object,
        *,
        expected_revision: int,
    ) -> _ControlPlaneApplyResult:
        """控制面 PUT：经 runtime-owned manager 单次 strict CAS 持久化更新。

        - 成功：检测本地 listener 是否已发布；未发布（detached 持久快照）
          标记 ``snapshot_publish_failed``（configured=new / effective=LKG）；
        - 修订冲突（CAS 返回 ``None``）：明确以固定冲突异常失败，不重读、不重试；
        - manager 未初始化 / 未知字段 / 存储异常：收敛为固定控制面错误，
          不携带 raw 异常；
        - 不查询 manager registry。
        """
        if self._manager is None or self._closed:
            raise _ControlPlaneError(
                _CONTROL_PLANE_RUNTIME_UNAVAILABLE, "运行时未启动或已关闭"
            )
        now = self._clock()
        with self._publish_lock:
            self._last_refresh_attempt_at = now
        try:
            new_snapshot = await self._manager.update_field_if_revision_async(
                field_name,
                value,
                expected_revision=expected_revision,
            )
        except ValueError:
            raise _ControlPlaneError(
                _CONTROL_PLANE_UNKNOWN_FIELD,
                "未知的配置字段或不支持的严格 CAS 参数",
            ) from None
        except Exception:
            # 与 GET 刷新失败同一收敛：保留 LKG 转 DEGRADED（无 LKG 转 FAILED），
            # 再抛固定控制面错误（不携带 raw 异常）。
            self._converge_refresh_storage_error(self._clock())
            raise _ControlPlaneError(
                _CONTROL_PLANE_STORAGE_ERROR, "持久化更新存储读取 / 写入失败"
            ) from None

        if new_snapshot is None:
            raise _ControlPlaneConflictError(
                _CONTROL_PLANE_CAS_CONFLICT, "配置修订冲突，未应用变更"
            )

        # 严格 CAS 写入成功（storage 写入已确认）：登记存储成功时间。
        with self._publish_lock:
            self._last_storage_success_at = self._clock()

        published_locally = (
            self._aggregate.state.configured_revision == new_snapshot.revision
        )
        if not published_locally:
            # detached persisted snapshot：已持久化但未本地发布
            with self._publish_lock:
                if self._closed:
                    return _ControlPlaneApplyResult(
                        new_revision=new_snapshot.revision, published_locally=False
                    )
                current = self._aggregate
                now = self._clock()
                self._aggregate = _RuntimeAggregate(
                    state=AdmissionRuntimeState(
                        status=(
                            AdmissionRuntimeStatus.DEGRADED
                            if current.policy is not None
                            else AdmissionRuntimeStatus.FAILED
                        ),
                        problem_code="snapshot_publish_failed",
                        configured_revision=new_snapshot.revision,
                        effective_revision=current.state.effective_revision,
                        using_last_known_good=current.policy is not None,
                    ),
                    policy=current.policy,
                )
                self._configured_updated_at = new_snapshot.updated_at
                self._mark_problem("snapshot_publish_failed", now)
            return _ControlPlaneApplyResult(
                new_revision=new_snapshot.revision, published_locally=False
            )
        return _ControlPlaneApplyResult(
            new_revision=new_snapshot.revision, published_locally=True
        )

    # --------------------------- 内存状态投影 ---------------------------

    def _project_status(self) -> dict[str, object]:
        """内存私有状态投影（11 顶层键 + telemetry 深拷贝）。

        datetime 字段保持 aware UTC 供 API 序列化；telemetry 各 map 深拷贝，
        调用方不得修改返回对象影响运行时。本阶段仅供阶段 A control plane ``/status`` 消费。
        """
        with self._publish_lock:
            state = self._aggregate.state
            configured_updated_at = self._configured_updated_at
            effective_loaded_at = self._effective_loaded_at
            last_refresh_attempt_at = self._last_refresh_attempt_at
            last_storage_success_at = self._last_storage_success_at
            problem_since = self._problem_since
        with self._telemetry_lock:
            telemetry: dict[str, object] = {
                "started_at": self._started_at,
                "adjudications_total": self._adjudications_total,
                "by_reason_code": dict(self._telemetry_reason),
                "by_intent": dict(self._telemetry_intent),
                "attribution_failures_by_event_family": dict(self._telemetry_family),
                "runtime_problem_occurrences": dict(self._telemetry_problem),
            }
        return {
            "status": state.status.value,
            "problem_code": state.problem_code,
            "configured_revision": state.configured_revision,
            "effective_revision": state.effective_revision,
            "using_last_known_good": state.using_last_known_good,
            "configured_updated_at": configured_updated_at,
            "effective_loaded_at": effective_loaded_at,
            "last_refresh_attempt_at": last_refresh_attempt_at,
            "last_storage_success_at": last_storage_success_at,
            "problem_since": problem_since,
            "telemetry": telemetry,
        }

    # --------------------------- 遥测计数 ---------------------------

    def _record_adjudication(
        self,
        result: AdmissionResult,
        intent: AdmissionIntent,
        event_family: str,
    ) -> None:
        """恰一次基础遥测计数（线程安全）。"""
        normalized_family = (
            event_family if event_family in _FAMILY_CODES else _DEFAULT_EVENT_FAMILY
        )
        with self._telemetry_lock:
            self._adjudications_total += 1
            self._telemetry_reason[str(result.reason_code)] += 1
            self._telemetry_intent[intent.value] += 1
            if result.reason_code == "group_attribution_unavailable":
                self._telemetry_family[normalized_family] += 1

    def _mark_problem(self, problem_code: AdmissionProblemCode, now: datetime) -> None:
        """同一问题 episode 只计一次 occurrence；切换问题码开新计数。

        活跃 episode 由最近一次 ``_mark_problem`` 的问题码标识：相同码为延续
        （保留首次 ``problem_since``、不重复计数），不同码或从无到有为新的
        episode（计 occurrence、必要时置位 ``problem_since``）。episode 的完整
        生命周期（稳定恢复窗口）由阶段 B ``process_observability`` 显式结束。
        """
        if self._active_problem_code == problem_code:
            return  # 同一 episode 延续：不重复计数、保留首次 problem_since
        if self._problem_since is None:
            self._problem_since = now
        with self._telemetry_lock:
            self._telemetry_problem[problem_code] += 1
        self._active_problem_code = problem_code


def _rejected(
    effective_revision: int | None,
    reason_code: AdmissionReasonCode,
) -> AdmissionResult:
    return AdmissionResult(
        qualification=AdmissionQualification.REJECTED,
        effective_revision=effective_revision,
        reason_code=reason_code,  # type: ignore[arg-type]
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
