"""Jev 判定引擎：输入冻结、共享调度、单次 SDK 请求与单次交接。

本模块是 TSK-182 的业务实现层。它只依赖冻结的纯契约
(``komari_bot.decision.jev``)、判定自有 Redis 布局、判定仓储与官方 SDK
请求 seam；不导入 NoneBot matcher、不写记忆模块、不发送业务回复。

外部效果（引用补取、SDK 调用、交接、失败通知）之前一律先核对持久任务仍是
本实例当前代数，再做同步准入裁决；裁决与持久撤销都不允许被迟到结果复活。
"""

from __future__ import annotations

import asyncio
import random
from collections import OrderedDict
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING, Any, Final, Literal, cast
from uuid import uuid4

from nonebot import logger
from typesafe_sdk import (
    SystemOneResponse,
    TypeSafeAPIConnectionError,
    TypeSafeAPIError,
    TypeSafeAPIResponseValidationError,
    TypeSafeAPITimeoutError,
    TypeSafeAuthenticationError,
    TypeSafePermissionDeniedError,
    TypeSafeRateLimitError,
)

from komari_bot.decision.jev import (
    DecisionHealthSnapshot,
    DecisionLogMetadata,
    DecisionMessage,
    DecisionReason,
    DecisionRequest,
    DecisionResult,
    DecisionStatus,
    DecisionUsage,
    EventIdentity,
    HandoffRoute,
    PhaseDurations,
    ReferenceSummary,
    ReplyChoice,
    RetentionChoice,
)
from komari_bot.llm.diagnostic_sanitizer import (
    sanitize_log_value,
    sanitize_text_field,
)
from komari_bot.plugins.komari_decision.repositories.decision_log_repository import (
    DecisionLogRepository,
)
from komari_bot.plugins.komari_decision.repositories.jev_repository import (
    STATE_BYPASS,
    STATE_CANCELLED,
    STATE_FAILED,
    STATE_FILTERED,
    STATE_SUCCESS,
    Ingress,
    JevDecisionRepository,
    RecentSettlementFact,
    Reservation,
    ReservationKind,
    ReservePlan,
)
from komari_bot.plugins.komari_decision.services.jev_input import (
    AdmissionRevokedError,
    DecisionProtocolError,
    DecisionPseudonymizer,
    ParsedAnswers,
    QuestionPlan,
    ReferenceResolver,
    TaskRevokedError,
    build_questions,
    judge_ordinary_message,
    parse_answers,
)
from komari_bot.plugins.komari_decision.services.jev_recent import (
    DecisionRecentStore,
    ResolveAction,
    resolve_action_for_retention,
)
from komari_bot.plugins.komari_decision.services.jev_request import (
    JevRequestConfigurationError,
    JevRequestTimeoutError,
    configured_jev_request,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    import httpx2
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncSession
    from typesafe_sdk import Question

    from komari_bot.config.prompt_storage import PromptTemplateLoader, StoredPrompt
    from komari_bot.plugins.config_manager import ConfigSnapshot
    from komari_bot.plugins.komari_decision.config_schema import (
        KomariDecisionConfigSchema,
    )

SUMMARY_THRESHOLD: Final = 0.7
"""总结独占阈值：严格大于 0.7 才命中（0.7 不命中）。"""

_WATCH_POLL_SECONDS: Final = 0.2
"""撤销/租约观察间隔（只读本地与数据库状态，不探测模型）。"""

_NOTIFICATION_REASONS: Final[frozenset[DecisionReason]] = frozenset(
    {
        "request_timeout",
        "network_error",
        "rate_limited",
        "authentication_error",
        "provider_error",
        "protocol_error",
        "configuration_invalid",
        "prompt_unavailable",
        "input_unavailable",
        "storage_unavailable",
        "internal_error",
    }
)
"""上游/运行时技术失败的原因码（可取得一次性失败通知资格）。"""

_SUMMARY_NOTIFICATION_REASONS: Final[frozenset[DecisionReason]] = frozenset(
    {"queue_full", "queue_timeout"}
)
"""总结候选在排队阶段被拒也拿不到有效 Noul，需要一次失败通知资格。"""

_SILENT_REASONS: Final[frozenset[DecisionReason]] = frozenset(
    {"duplicate_event", "disabled", "admission_revoked", "interrupted", "filtered"}
)
"""绝不取得失败通知资格的原因码。"""

_REMOVE_REASONS: Final[frozenset[str]] = frozenset({"disabled", "admission_revoked"})
"""这些原因码的终结行必须把条目从输入窗口移除（即便曾有成功布尔值）。"""

_MAX_RETAINED_RESULTS: Final = 256
"""同次分发结果的内存保留上限（超出后按最早写入淘汰，不会重新执行）。"""

_OBSERVATION: Final[ContextVar[_DiagnosticObservation | None]] = ContextVar(
    "jev_decision_observation",
    default=None,
)
"""当前分发的诊断观测载体。

只承载日志事实（实际请求 state/questions、真实响应、阶段耗时、引用完整性、
入口配置/Prompt 修订）。绝不承载授权、准入裁决或业务所有权，也不得用于任何
业务分流。
"""


def _current_observation() -> _DiagnosticObservation | None:
    """返回当前分发的诊断观测（无观测时返回 ``None``，绝不隐式创建）。"""
    return _OBSERVATION.get()


def _observation_secrets() -> tuple[str, ...]:
    """任务入口冻结的脱敏用凭据（任务内不变，配置变更不得暴露旧值）。"""
    observation = _OBSERVATION.get()
    return () if observation is None else observation.secrets()


@dataclass(frozen=True, slots=True, kw_only=True)
class _FrozenTask:
    """任务入口一次性冻结的配置与时间预算。"""

    config: KomariDecisionConfigSchema
    revision: int | None
    plugin_enable: bool
    bot_aliases: tuple[str, ...]
    queue_timeout_seconds: float
    reference_timeout_seconds: float
    request_timeout_seconds: float
    optional_reply_probability: float
    model: str


@dataclass(slots=True)
class _DiagnosticObservation:
    """一次分发的诊断观测：只承载事实，绝不参与业务判定。

    由 ``_evaluate_dispatch`` 建立，经 ContextVar 在内部方法里填写实际请求
    state/questions、真实响应、阶段耗时与引用完整性；终态业务/所有权清理
    完成后由 ``_record_observation`` 提交一次，随即释放捕获的正文。

    所有脱敏都用任务入口冻结的同一份凭据 ``api_key``：任务中途修改配置既不会
    改变本次观测的掩码，也不会把旧凭据原样暴露到日志。
    """

    decision_id: str
    identity: EventIdentity
    created_at: datetime
    api_key: str = ""
    config_revision: int | None = None
    requested_model: str | None = None
    prompt_revision: int | None = None
    path_observed: bool = False
    admission_denied: bool = False
    queue_ms: float | None = None
    references_ms: float | None = None
    request_ms: float | None = None
    references: ReferenceSummary | None = None
    actual_model: str | None = None
    usage: DecisionUsage | None = None
    state: dict[str, Any] | None = None
    questions: dict[str, Any] | None = None
    response: Any = None
    response_status: int | None = None
    recorded: bool = False
    """本次观测是否已经提交过（同一分发绝不重复写入）。"""

    def secrets(self) -> tuple[str, ...]:
        """冻结的脱敏用凭据元组（空值不参与字面量替换）。"""
        return () if not self.api_key else (self.api_key,)

    def note_path(self, path: _Path) -> None:
        """记录入口首次形成的路径（ADR-0012：入口即拒绝则不取得诊断历史）。"""
        self.path_observed = True
        self.admission_denied = path.kind == "admission_denied"

    def note_prompt(self, prompt: StoredPrompt) -> None:
        """记录本次实际加载到的 Prompt 修订。"""
        self.prompt_revision = prompt.revision

    def merge_durations(self, durations: PhaseDurations) -> None:
        """合并实际观测到的阶段耗时（不覆盖已知值）。"""
        self.queue_ms = _first_float(durations.queue_ms, self.queue_ms)
        self.references_ms = _first_float(durations.references_ms, self.references_ms)
        self.request_ms = _first_float(durations.request_ms, self.request_ms)

    def mark_references_incomplete(self) -> None:
        """引用链开始补取：在取得完整摘要之前不得声称链接已完整。"""
        self.references = ReferenceSummary(complete=False)

    def capture_request(
        self, *, state: dict[str, Any], questions: dict[str, Any]
    ) -> None:
        """在真实请求边界记录已脱敏的实际提交 state 与问题集。"""
        self.state = state
        self.questions = questions

    def note_response(self, response: SystemOneResponse) -> None:
        """记录已成功解码的真实响应：原始 JSON/文本、状态码、模型与用量。"""
        body, status = _raw_response_facts(response)
        self.response = body
        self.response_status = status
        self.actual_model = response.model
        self.usage = DecisionUsage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

    def note_error_response(self, error: TypeSafeAPIError) -> None:
        """失败响应证据只取 SDK 公开的 ``body`` 与 ``status``。"""
        self.response = error.body
        self.response_status = int(error.status)

    def body(self) -> dict[str, Any] | None:
        """实际正文；没有在请求边界真正提交过请求时保持 ``None``。"""
        if self.state is None or self.questions is None:
            return None
        return {
            "state": self.state,
            "questions": self.questions,
            "response": self.response,
            "response_status": self.response_status,
        }

    def diagnostic_result(self, result: DecisionResult) -> DecisionResult:
        """只为日志补齐确实观测到的诊断字段；分类与路由保持业务结果原样。"""
        return replace(
            result,
            config_revision=_first_value(result.config_revision, self.config_revision),
            prompt_revision=_first_value(result.prompt_revision, self.prompt_revision),
            requested_model=_first_value(result.requested_model, self.requested_model),
            actual_model=_first_value(result.actual_model, self.actual_model),
            references=self.references
            if self.references is not None
            else result.references,
            durations=PhaseDurations(
                queue_ms=_first_value(result.durations.queue_ms, self.queue_ms),
                references_ms=_first_value(
                    result.durations.references_ms, self.references_ms
                ),
                request_ms=_first_value(result.durations.request_ms, self.request_ms),
            ),
            usage=self.usage if self.usage is not None else result.usage,
        )

    def release(self) -> None:
        """释放已提交的正文，避免观测继续占用内存。"""
        self.state = None
        self.questions = None
        self.response = None


@dataclass(slots=True)
class _HealthState:
    """可变的健康快照视图字段（不对外暴露可变对象）。"""

    runtime_status: Literal["ready", "disabled", "failed"] = "failed"
    reason_code: DecisionReason = "initializing"
    last_request_status: Literal["unknown", "success", "failure"] = "unknown"
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_failure_reason_code: DecisionReason | None = None
    consecutive_failures: int = 0


@dataclass(slots=True)
class _OwnedDispatch:
    """本实例持有的同次分发行。"""

    identity: EventIdentity
    decision_id: str
    task: asyncio.Task[DecisionResult]
    waiters: int = 0
    provisional: bool = False
    """本次分发是否已在近期窗口写入 provisional（关闭时需要定案）。"""
    registered: bool = False
    """本次分发是否已经写入持久身份行（此前不得按撤销处理）。"""
    cancelling: bool = False
    """本地已经发出过取消：等待其真实清理结束，绝不重复取消。"""


class _DecisionInputUnavailableError(RuntimeError):
    """判定输入窗口不可用（Redis 边界失败）。"""


class _ModelFailureError(RuntimeError):
    """SDK/配置/协议失败的内部封闭原因码载体。"""

    def __init__(
        self, reason: DecisionReason, *, counts_as_request: bool = True
    ) -> None:
        super().__init__(reason)
        self.reason: DecisionReason = reason
        self.counts_as_request = counts_as_request


class JevDecisionEngine:
    """新判定引擎（Jev 主判定）。"""

    def __init__(
        self,
        *,
        config_source: Callable[[], ConfigSnapshot],
        prompt_loader: PromptTemplateLoader,
        redis: Redis,
        reference_reader: Callable[
            [EventIdentity, str], Awaitable[DecisionMessage | None]
        ],
        admission: Callable[[EventIdentity], bool],
        session_factory: Callable[[], AsyncSession] | None = None,
        transport_factory: Callable[[], httpx2.AsyncBaseTransport] | None = None,
        random_source: Callable[[], float] = random.random,
        log_repository: DecisionLogRepository | None = None,
    ) -> None:
        self._config_source = config_source
        self._prompt_loader = prompt_loader
        self._redis = redis
        self._reference_reader = reference_reader
        self._admission = admission
        self._transport_factory = transport_factory
        self._random_source = random_source
        self._session_factory = session_factory or _default_session_factory()
        self._repository = JevDecisionRepository(self._session_factory)
        # 诊断日志仓储：注入只为测试，``None`` 表示真实共享 ORM 仓储（不是关闭日志）。
        self._log_repository = (
            log_repository
            if log_repository is not None
            else DecisionLogRepository(self._session_factory, self._log_retention_days)
        )
        self._log_tasks: set[asyncio.Task[None]] = set()
        self._recent = DecisionRecentStore(redis)
        self._pseudonymizer: DecisionPseudonymizer | None = None
        self._health = _HealthState()
        self._dispatches: dict[tuple[EventIdentity, str], _OwnedDispatch] = {}
        self._results: OrderedDict[tuple[EventIdentity, str], DecisionResult] = (
            OrderedDict()
        )
        self._revoked: set[tuple[EventIdentity, str]] = set()
        self._admission_revoked: set[tuple[EventIdentity, str]] = set()
        self._state_observed_at = datetime.now(UTC)
        self._readiness_lock = asyncio.Lock()
        self._leased = False
        self._lease_lost = False
        self._initialized = False
        self._ownership_attempted = False
        self._closed = False
        self._watch_task: asyncio.Task[None] | None = None

    # ------------------------------ 生命周期 ------------------------------

    async def initialize(self) -> None:
        """初始化持久身份与就绪状态；失败只反映到健康快照。"""
        await self._ensure_ready(cancel_inflight=False)

    async def refresh(self) -> None:
        """重新检查配置、Prompt 与持久所有权（不发起任何模型请求）。"""
        await self._ensure_ready(cancel_inflight=True)

    async def close(self) -> None:
        """取消并等待自有任务、定案输入、撤销未交接资格、释放自有资源。"""
        if self._closed:
            return
        self._closed = True
        watch_task = self._watch_task
        self._watch_task = None
        if watch_task is not None:
            watch_task.cancel()
            with suppress(BaseException):
                await watch_task
        owned = list(self._dispatches.values())
        for dispatch in owned:
            dispatch.cancelling = True
            dispatch.task.cancel()
        if owned:
            await asyncio.gather(
                *(dispatch.task for dispatch in owned), return_exceptions=True
            )
        for dispatch in owned:
            if dispatch.provisional:
                await self._settle_finished(dispatch.identity)
        # 自有诊断写入必须在关闭判定仓储前全部结束，绝不遗留后台任务。
        await self._drain_diagnostics()
        self._dispatches.clear()
        self._results.clear()
        self._revoked.clear()
        self._admission_revoked.clear()
        await self._repository.close()
        self._leased = False
        self._pseudonymizer = None
        self._set_runtime("failed", "interrupted")
        with suppress(Exception):
            await self._redis.aclose()

    def get_health(self) -> DecisionHealthSnapshot:
        """返回被动健康快照（只读内存状态，不产生 I/O）。"""
        observation = self._repository.last_observation
        observed_at = self._state_observed_at
        repository_observed_at = self._repository.last_observation_at
        if repository_observed_at is not None and repository_observed_at > observed_at:
            observed_at = repository_observed_at
        return DecisionHealthSnapshot(
            runtime_status=self._health.runtime_status,
            reason_code=self._health.reason_code,
            last_request_status=self._health.last_request_status,
            last_success_at=self._health.last_success_at,
            last_failure_at=self._health.last_failure_at,
            last_failure_reason_code=self._health.last_failure_reason_code,
            consecutive_failures=self._health.consecutive_failures,
            in_flight_count=None if observation is None else observation.running,
            queued_count=None if observation is None else observation.queued,
            observed_at=observed_at,
        )

    # ------------------------------ 输入与观测 ------------------------------

    async def observe_delivered(
        self,
        message: DecisionMessage,
        *,
        protocol: str,
        account_id: str,
    ) -> None:
        """吸收一条已确认的平台送达（只更新判定自有上下文）。"""
        if self._closed:
            return
        await self._recent.observe_confirmed(
            protocol=protocol,
            account_id=account_id,
            message=message,
        )

    # ------------------------------ 判定入口 ------------------------------

    async def evaluate(self, request: DecisionRequest) -> DecisionResult:
        """执行一次完整判定（同一分发复用同一个本地任务）。"""
        key = (request.event, request.dispatch_id)
        cached = self._results.get(key)
        if cached is not None:
            return cached
        owned = self._dispatches.get(key)
        if owned is None:
            if self._closed:
                return _terminal_result(
                    "failed", "interrupted", decision_id=str(uuid4())
                )
            # 普通任务配置在公开入口同步冻结，早于任何调度间隙。
            frozen = self._freeze_task()
            task = asyncio.create_task(
                self._evaluate_dispatch(request, frozen),
                name=f"jev-decision-{request.dispatch_id}",
            )
            owned = _OwnedDispatch(
                identity=request.event,
                decision_id="",
                task=task,
            )
            self._dispatches[key] = owned
            task.add_done_callback(
                lambda finished, dispatch_key=key: self._dispatch_finished(
                    dispatch_key, finished
                )
            )
        owned.waiters += 1
        try:
            return await asyncio.shield(owned.task)
        finally:
            owned.waiters -= 1

    async def claim_handoff(
        self,
        request: DecisionRequest,
        result: DecisionResult,
        route: HandoffRoute,
    ) -> bool:
        """领取一次单次外部效果资格；同一行最多成功一次。"""
        key = (request.event, request.dispatch_id)
        if self._closed or self._lease_lost or key in self._revoked:
            return False
        if not self._admission(request.event):
            # 观察到的撤权必须持久化：恢复准入后也不能复活该事件。
            await self._repository.persist_revocation(
                identity=request.event,
                decision_id=result.decision_id,
                dispatch_id=request.dispatch_id,
            )
            return False
        if route == "failure_notification":
            return await self._claim_notification(request, result)
        return await self._claim_business_route(request, result, route)

    async def _claim_business_route(
        self,
        request: DecisionRequest,
        result: DecisionResult,
        route: HandoffRoute,
    ) -> bool:
        """聊天/总结交接：请求路由必须等于完成时持久化的允许路由。"""
        if result.route != route:
            return False
        allowed = {STATE_SUCCESS, STATE_BYPASS} if route == "chat" else {STATE_SUCCESS}
        if result.status not in allowed:
            return False
        return await self._repository.claim_handoff(
            identity=request.event,
            decision_id=result.decision_id,
            dispatch_id=request.dispatch_id,
            route=route,
            status=result.status,
            reason_code=result.reason_code,
            effect_check=lambda: self._admission(request.event),
        )

    async def _claim_notification(
        self, request: DecisionRequest, result: DecisionResult
    ) -> bool:
        """失败通知使用独立的一次性资格。"""
        if not self._notification_eligible(
            result.reason_code, summary_candidate=request.summary_candidate
        ):
            return False
        return await self._repository.claim_notification(
            identity=request.event,
            decision_id=result.decision_id,
            dispatch_id=request.dispatch_id,
            status=result.status,
            reason_code=result.reason_code,
            effect_check=lambda: self._admission(request.event),
        )

    # ------------------------------ 判定主体 ------------------------------

    async def _evaluate_dispatch(
        self,
        request: DecisionRequest,
        frozen: _FrozenTask | None,
    ) -> DecisionResult:
        decision_id = str(uuid4())
        owned = self._dispatches.get((request.event, request.dispatch_id))
        if owned is not None:
            owned.decision_id = decision_id
        started = monotonic()
        observation = _DiagnosticObservation(
            decision_id=decision_id,
            identity=request.event,
            created_at=datetime.now(UTC),
            api_key="" if frozen is None else frozen.config.jev_api_key,
            config_revision=None if frozen is None else frozen.revision,
            requested_model=None if frozen is None else frozen.model,
        )
        token = _OBSERVATION.set(observation)
        try:
            result = await self._business_outcome(
                request, decision_id, frozen, started, owned, observation
            )
        finally:
            _OBSERVATION.reset(token)
        # 单次观测边界：业务与所有权清理已全部结束，只写一条日志。
        await self._record_observation(observation, result)
        return result

    async def _business_outcome(
        self,
        request: DecisionRequest,
        decision_id: str,
        frozen: _FrozenTask | None,
        started: float,
        owned: _OwnedDispatch | None,
        observation: _DiagnosticObservation,
    ) -> DecisionResult:
        """执行业务主体，并只在取消/未预期失败时做各自的一次性收尾。"""
        try:
            return await self._run_dispatch(request, decision_id, frozen, started)
        except asyncio.CancelledError:
            # 持有清理任务并等待真正完成；shield 本身不能阻止外层重复取消。
            if owned is not None:
                owned.cancelling = True
            cleanup = asyncio.create_task(
                self._finalize_cancelled(request, decision_id, owned, observation)
            )
            await _complete_owned(cleanup)
            raise
        except Exception as exc:
            logger.warning(
                "[KomariDecision] Jev 判定未预期失败: error={}", type(exc).__name__
            )
            try:
                await self._repository.finalize(
                    identity=request.event,
                    decision_id=decision_id,
                    dispatch_id=request.dispatch_id,
                    status=STATE_FAILED,
                    reason_code="internal_error",
                    business_route="none",
                    notification_eligible=True,
                    recent_retained=None,
                )
            except Exception:
                self._invalidate_unsettled_owner()
            await self._settle_finished(request.event)
            return _terminal_result("failed", "internal_error", decision_id=decision_id)

    async def _finalize_cancelled(
        self,
        request: DecisionRequest,
        decision_id: str,
        owned: _OwnedDispatch | None,
        observation: _DiagnosticObservation,
    ) -> None:
        """取消后的收尾：写终态（释放名额）、定案本次 provisional，再提交诊断。"""
        try:
            if not self._lease_lost:
                await self._repository.finalize(
                    identity=request.event,
                    decision_id=decision_id,
                    dispatch_id=request.dispatch_id,
                    status=STATE_CANCELLED,
                    reason_code="interrupted",
                    business_route="none",
                    notification_eligible=False,
                    recent_retained=None,
                )
        except Exception:
            self._invalidate_unsettled_owner()
        finally:
            try:
                await self._settle_cancelled(request, owned)
            finally:
                # 业务落库失败也不能跳过已开始任务的诊断收尾。
                await self._record_observation(
                    observation, self._cancelled_result(request, decision_id)
                )

    def _invalidate_unsettled_owner(self) -> None:
        """收尾事实未确认时停用本实例；实际清理结束前继续持有旧会话锁。"""
        self._lease_lost = True
        self._leased = False
        self._set_runtime("failed", "storage_unavailable")
        current = asyncio.current_task()
        for owned in tuple(self._dispatches.values()):
            if owned.task is not current and not owned.task.done():
                owned.cancelling = True
                owned.task.cancel()
        # refresh/close 先等待旧任务与诊断，再释放旧锁；绝不提前回收在途名额。

    def _cancelled_result(
        self, request: DecisionRequest, decision_id: str
    ) -> DecisionResult:
        """取消终态：保留真实撤权原因码，业务路由固定为 none。"""
        revoked = (request.event, request.dispatch_id) in self._admission_revoked
        return _terminal_result(
            "cancelled",
            "admission_revoked" if revoked else "interrupted",
            decision_id=decision_id,
        )

    # ------------------------------ 诊断边界 ------------------------------

    def _log_retention_days(self) -> int:
        """诊断日志保留期：每次操作实时读同一个配置源快照。"""
        return self._config_source().value.decision_log_retention_days

    def _diagnostics_eligible(self, observation: _DiagnosticObservation) -> bool:
        """ADR-0012：入口即拒绝的事件不得取得可查询的诊断历史。"""
        if observation.admission_denied:
            return False
        if observation.path_observed:
            return True
        # 入口路径尚未形成（例如存储就绪检查先失败）：只按当前同步准入裁决决定
        # 这条元数据是否可查询，绝不复用给任何业务效果。
        try:
            return bool(self._admission(observation.identity))
        except Exception:  # 准入不可用时不取得诊断历史
            return False

    async def _record_observation(
        self, observation: _DiagnosticObservation, result: DecisionResult
    ) -> None:
        """提交本次分发唯一的诊断观测；失败只留安全诊断，不重试不改业务。"""
        if observation.recorded or not self._diagnostics_eligible(observation):
            return
        observation.recorded = True
        metadata = DecisionLogMetadata(
            decision_id=observation.decision_id,
            event=observation.identity,
            created_at=observation.created_at,
            finished_at=datetime.now(UTC),
            result=observation.diagnostic_result(result),
        )
        task = asyncio.create_task(
            self._write_observation(
                metadata,
                body=observation.body(),
                known_secrets=observation.secrets(),
            ),
            name=f"jev-decision-log-{observation.decision_id}",
        )
        self._log_tasks.add(task)
        task.add_done_callback(self._log_tasks.discard)
        try:
            # 已开始的写入必须写完；期间的重复取消在写完之后再传播。
            await _await_owned_log(task)
        finally:
            observation.release()

    async def _write_observation(
        self,
        metadata: DecisionLogMetadata,
        *,
        body: dict[str, Any] | None,
        known_secrets: tuple[str, ...],
    ) -> None:
        """写入一条诊断日志；任何异常只转成固定安全诊断。"""
        try:
            await self._log_repository.record(
                metadata, body=body, known_secrets=known_secrets
            )
        except Exception as exc:  # 存储故障一律收敛为固定安全诊断
            logger.warning(
                "[KomariDecision] Jev 判定诊断日志写入失败: error={}",
                type(exc).__name__,
            )

    async def _drain_diagnostics(self) -> None:
        """等待本实例已开始的诊断写入结束。"""
        while True:
            pending = [task for task in self._log_tasks if not task.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    async def _settle_cancelled(
        self, request: DecisionRequest, owned: _OwnedDispatch | None
    ) -> None:
        """取消/关闭时定案本次分发的 provisional：撤权内容绝不转成确认。"""
        if owned is None or not owned.provisional:
            return
        await self._settle_finished(request.event)
        owned.provisional = False

    async def _run_dispatch(
        self,
        request: DecisionRequest,
        decision_id: str,
        frozen: _FrozenTask | None,
        started: float,
    ) -> DecisionResult:
        if not self._repository.ready or self._lease_lost:
            return _terminal_result(
                "failed", "storage_unavailable", decision_id=decision_id
            )
        prepared = await self._prepare_dispatch(request, decision_id, frozen)
        if prepared.early is not None:
            return prepared.early
        early, queue_ms = await self._await_slot(request, decision_id, prepared)
        if early is not None:
            return early
        del started
        return await self._run_model_path(
            request=request,
            decision_id=decision_id,
            prepared=prepared,
            queue_ms=queue_ms,
        )

    async def _prepare_dispatch(
        self,
        request: DecisionRequest,
        decision_id: str,
        frozen: _FrozenTask | None,
    ) -> _Prepared:
        """确定路径、冻结输入并写入持久身份（早退结果放在 ``early``）。"""
        path = self._resolve_path(request, frozen)
        observation = _current_observation()
        if observation is not None:
            observation.note_path(path)
        eligible = path.input_eligible
        prompt = await self._load_prompt() if path.needs_prompt else None
        if prompt is not None and observation is not None:
            observation.note_prompt(prompt)
        if path.needs_prompt and prompt is None:
            # Prompt 缺失只影响执行受理，不撤销已经成立的输入资格。
            path = _Path(
                kind="prompt_failure",
                terminal_state=STATE_FAILED,
                terminal_reason="prompt_unavailable",
                input_eligible=eligible,
            )
        reservation, predecessors = await self._reserve(
            request, path, frozen, decision_id
        )
        early = self._reservation_outcome(reservation, path, decision_id)
        if early is not None:
            return _Prepared(early=early)
        return _Prepared(
            frozen=frozen,
            prompt=prompt,
            path=path,
            predecessors=predecessors,
            reservation=reservation,
        )

    async def _reserve(
        self,
        request: DecisionRequest,
        path: _Path,
        frozen: _FrozenTask | None,
        decision_id: str,
    ) -> tuple[Reservation, tuple[DecisionMessage, ...]]:
        """串行化入口：死主回收 → 输入定案 → 准入复检 → 身份占位 → 冻结正文。

        身份占位与重复判定**先于**任何正文读取/写入：外部重投既不会写正文，
        也不会改写已有缓存文本。输入边界失败在**同一事务**里把刚占位的行改为
        failed/input_unavailable（不保留名额/路由，也不回滚身份）。
        """
        plan = self._reserve_plan(request, path, frozen, decision_id)
        key = (request.event, request.dispatch_id)
        predecessors: tuple[DecisionMessage, ...] = ()
        register_provisional = False
        try:
            async with self._repository.ingress() as ingress:
                await ingress.reconcile()
                await self._settle_recent_scope(ingress, request.event)
                if not self._admission(request.event):
                    # 锁内、任何 Redis 正文操作之前的复检：只提交最小身份。
                    return (
                        await ingress.reserve(
                            request_identity=request.event,
                            plan=_admission_denied_plan(plan),
                        ),
                        (),
                    )
                reservation = await ingress.reserve(
                    request_identity=request.event, plan=plan
                )
                if reservation.kind in {
                    ReservationKind.ENQUEUED,
                    ReservationKind.TERMINAL,
                    ReservationKind.QUEUE_FULL,
                } and not self._admission(request.event):
                    return (
                        await ingress.finish_reserved(
                            request_identity=request.event,
                            plan=_admission_denied_plan(plan),
                        ),
                        (),
                    )
                if reservation.kind is ReservationKind.ENQUEUED:
                    try:
                        predecessors = await self._freeze_recent(request)
                    except _DecisionInputUnavailableError:
                        # 输入失败保留身份；通知资格仍按事件类型计算。
                        reservation = await ingress.finish_reserved(
                            request_identity=request.event,
                            plan=_input_failure_plan(plan, request),
                        )
                    else:
                        register_provisional = True
                elif path.input_eligible and reservation.kind in {
                    ReservationKind.TERMINAL,
                    ReservationKind.QUEUE_FULL,
                }:
                    # 通过过滤但未被执行（直通/队列拒绝/Prompt 缺失）：保留合格原文，
                    # 不伪造任何分类。重复/不可用结果绝不触碰正文。
                    await self._confirm_delivered(request)
        except Exception as exc:
            logger.warning(
                "[KomariDecision] Jev 判定持久化入口失败: error={}", type(exc).__name__
            )
            return Reservation(kind=ReservationKind.UNAVAILABLE), ()
        if register_provisional:
            # 只在事务提交成功之后发布本地 provisional/registered 状态：
            # 观察者绝不能看到「已登记但行未提交」的窗口。
            owned = self._dispatches.get(key)
            if owned is not None:
                owned.provisional = True
                owned.registered = True
        return reservation, predecessors

    def _reserve_plan(
        self,
        request: DecisionRequest,
        path: _Path,
        frozen: _FrozenTask | None,
        decision_id: str,
    ) -> ReservePlan:
        return ReservePlan(
            decision_id=decision_id,
            dispatch_id=request.dispatch_id,
            wants_slot=path.wants_slot,
            queue_timeout_seconds=(
                frozen.queue_timeout_seconds if frozen is not None else 3.0
            ),
            terminal_state=path.terminal_state,
            terminal_reason=path.terminal_reason,
            failure_notification=self._plan_failure_notification(request, path),
        )

    def _plan_failure_notification(self, request: DecisionRequest, path: _Path) -> bool:
        """总结候选在取不到有效 Noul 的技术失败/排队拒绝上需要一次失败通知。"""
        if path.terminal_state == STATE_FAILED:
            return self._notification_eligible(
                _coerce_reason(path.terminal_reason or ""),
                summary_candidate=request.summary_candidate,
            )
        return bool(request.summary_candidate and path.wants_slot)

    async def _settle_finished(self, identity: EventIdentity) -> None:
        """按当前持久事实定案；迟到的本地结果不能覆盖撤销记录。"""
        with suppress(Exception):
            async with self._repository.ingress() as ingress:
                await self._settle_recent_scope(ingress, identity)

    async def _settle_recent_scope(
        self, ingress: Ingress, identity: EventIdentity
    ) -> None:
        """用同一调度事务里的持久事实定案该作用域的 provisional 条目。"""
        protocol = identity.protocol
        account_id = identity.account_id
        group_id = identity.group_id
        try:
            pending = await self._recent.provisional_ids(
                protocol=protocol, account_id=account_id, group_id=group_id
            )
        except Exception as exc:
            logger.warning(
                "[KomariDecision] 输入窗口未定案条目读取失败: error={}",
                type(exc).__name__,
            )
            return
        if not pending:
            return
        try:
            facts = await ingress.settlement_facts(
                protocol=protocol,
                account_id=account_id,
                group_id=group_id,
                message_ids=pending,
            )
        except Exception as exc:
            logger.warning(
                "[KomariDecision] 输入窗口定案事实读取失败: error={}",
                type(exc).__name__,
            )
            return
        decisions: dict[str, ResolveAction] = {}
        for message_id in pending:
            action = self._settlement_action(facts.get(message_id))
            if action is not None:
                decisions[message_id] = action
        if not decisions:
            return
        with suppress(Exception):
            await self._recent.settle(
                protocol=protocol,
                account_id=account_id,
                group_id=group_id,
                decisions=decisions,
            )

    @staticmethod
    @staticmethod
    def _settlement_action(
        fact: RecentSettlementFact | None,
    ) -> ResolveAction | None:
        """把持久事实映射为窗口定案动作（不猜分类，不按时间判断）。"""
        if fact is None:
            # 只存在于 Redis 的 provisional：没有可运行所有者（例如入口提交失败）。
            return "confirm"
        if not fact.settled:
            # 仍在排队/执行且所有者活着：无论多旧都保持 provisional。
            return None
        if fact.state == STATE_SUCCESS and fact.recent_retained is None:
            # 成功行必须带缓存处置事实；缺失时失败关闭并报告一致性缺口。
            logger.warning("[KomariDecision] 成功判定缺少输入缓存处置事实；保持未定案")
            return None
        if fact.state == STATE_FILTERED or fact.reason_code in _REMOVE_REASONS:
            return "remove"
        if fact.recent_retained is None:
            # 其它终结（技术失败/中断）：保留合格原文，不发明分类。
            return "confirm"
        return "confirm" if fact.recent_retained else "remove"

    def _reservation_outcome(
        self,
        reservation: Reservation,
        path: _Path,
        decision_id: str,
    ) -> DecisionResult | None:
        """把非入队结果映射为终态；入队返回 None 继续执行。"""
        if reservation.kind is ReservationKind.ENQUEUED:
            return None
        if reservation.kind is ReservationKind.TERMINAL:
            # 入口内改判的终态以持久行的原因码为准（路径可能已被锁内复检改写）。
            if reservation.reason_code == "input_unavailable":
                return _terminal_result(
                    "failed", "input_unavailable", decision_id=decision_id
                )
            if reservation.reason_code == "admission_revoked":
                return _terminal_result(
                    "cancelled", "admission_revoked", decision_id=decision_id
                )
            return self._terminal_from_plan(decision_id, path)
        status, reason = _RESERVATION_OUTCOMES[reservation.kind]
        return _terminal_result(status, reason, decision_id=decision_id)

    async def _await_slot(
        self,
        request: DecisionRequest,
        decision_id: str,
        prepared: _Prepared,
    ) -> tuple[DecisionResult | None, float | None]:
        """等待执行名额；取得名额返回 None，否则返回终态结果。"""
        row_id = prepared.reservation.row_id
        if row_id is None:
            return (
                _terminal_result(
                    "failed", "storage_unavailable", decision_id=decision_id
                ),
                None,
            )
        frozen = prepared.frozen
        grant = await self._repository.wait_for_grant(
            row_id=row_id,
            dispatch_id=request.dispatch_id,
            timeout_seconds=(
                frozen.queue_timeout_seconds if frozen is not None else 3.0
            ),
            failure_notification=self._notification_eligible(
                "queue_timeout", summary_candidate=request.summary_candidate
            ),
        )
        observation = _current_observation()
        if observation is not None:
            observation.queue_ms = grant.queue_ms
        if not grant.granted:
            return (
                await self._slot_failed(
                    request,
                    decision_id,
                    grant.state,
                    grant.reason_code,
                    queue_ms=grant.queue_ms,
                ),
                grant.queue_ms,
            )
        if self._lease_lost:
            await self._repository.release_slot(
                identity=request.event,
                decision_id=decision_id,
                dispatch_id=request.dispatch_id,
            )
            return (
                _terminal_result("cancelled", "interrupted", decision_id=decision_id),
                grant.queue_ms,
            )
        try:
            await self._require_current(request, decision_id)
        except (AdmissionRevokedError, TaskRevokedError) as exc:
            return (
                await self._revoked_result(
                    request,
                    decision_id,
                    exc,
                    durations=PhaseDurations(queue_ms=grant.queue_ms),
                ),
                grant.queue_ms,
            )
        return None, grant.queue_ms

    async def _slot_failed(
        self,
        request: DecisionRequest,
        decision_id: str,
        state: str,
        reason_code: str | None,
        *,
        queue_ms: float | None = None,
    ) -> DecisionResult:
        """排队未被授权时的终态与失败通知资格。"""
        reason = _coerce_reason(reason_code or "", default="queue_timeout")
        status: DecisionStatus = (
            "cancelled"
            if state == STATE_CANCELLED
            or reason in {"interrupted", "disabled", "admission_revoked"}
            else "failed"
        )
        if status == "failed" and self._notification_eligible(
            reason, summary_candidate=request.summary_candidate
        ):
            with suppress(Exception):
                await self._repository.mark_failure_notification(
                    identity=request.event,
                    decision_id=decision_id,
                    dispatch_id=request.dispatch_id,
                )
        await self._resolve_recent(
            request.event,
            request.target.group_id,
            request.target.message_id,
            retain_recent=None if status == "failed" else False,
        )
        return _terminal_result(
            status,
            reason,
            decision_id=decision_id,
            durations=PhaseDurations(queue_ms=queue_ms),
        )

    async def _revoked_result(
        self,
        request: DecisionRequest,
        decision_id: str,
        error: Exception,
        *,
        durations: PhaseDurations,
        references: ReferenceSummary | None = None,
    ) -> DecisionResult:
        """撤权/撤销的终态：持久化撤销，本地工作已停止后才释放名额。"""
        revoked = isinstance(error, AdmissionRevokedError)
        if revoked:
            await self._revoke(request, decision_id)
        # 引用/SDK 上下文此时已经退出：这是真正结束清理的那条路径。
        await self._repository.release_slot(
            identity=request.event,
            decision_id=decision_id,
            dispatch_id=request.dispatch_id,
        )
        await self._settle_entry(
            request.event,
            request.target.group_id,
            request.target.message_id,
            action="remove",
        )
        return _terminal_result(
            "cancelled",
            "admission_revoked" if revoked else "interrupted",
            decision_id=decision_id,
            durations=durations,
            references=references,
        )

    async def _run_model_path(
        self,
        *,
        request: DecisionRequest,
        decision_id: str,
        prepared: _Prepared,
        queue_ms: float | None,
    ) -> DecisionResult:
        """冻结引用链 → 一次 SDK 请求 → 单次完成。"""
        frozen = prepared.frozen
        prompt = prepared.prompt
        if frozen is None or prompt is None or self._pseudonymizer is None:
            await self._revoke(request, decision_id)
            return _terminal_result(
                "failed", "configuration_invalid", decision_id=decision_id
            )
        if prepared.path.question_plan.empty:
            msg = "确定性召唤不应进入模型判定"
            raise RuntimeError(msg)
        outcome = await self._effect_exchange(
            request=request,
            decision_id=decision_id,
            prepared=prepared,
            queue_ms=queue_ms,
        )
        if outcome.early is not None:
            return outcome.early
        if outcome.failure is not None:
            return await self._failure_outcome(
                request=request,
                decision_id=decision_id,
                frozen=frozen,
                prompt=prompt,
                outcome=outcome,
            )
        return await self._complete(
            request=request,
            decision_id=decision_id,
            prepared=prepared,
            outcome=outcome,
        )

    async def _effect_exchange(
        self,
        *,
        request: DecisionRequest,
        decision_id: str,
        prepared: _Prepared,
        queue_ms: float | None,
    ) -> _Exchange:
        """执行引用补取与 SDK 请求（含请求内的应用级校验）。"""
        frozen = prepared.frozen
        prompt = prepared.prompt
        assert frozen is not None
        assert prompt is not None
        assert self._pseudonymizer is not None
        plan = prepared.path.question_plan
        observation = _current_observation()
        if observation is not None:
            # 引用链一旦开始补取，未取得完整摘要前不得声称链接已完整。
            observation.mark_references_incomplete()
        reference_started = monotonic()
        references: ReferenceSummary | None = None
        references_ms = 0.0
        request_started: float | None = None
        try:
            frozen_input = await ReferenceResolver(
                identity=request.event,
                reader=self._reference_reader,
                guard=lambda: self._require_current(request, decision_id),
                pseudonymizer=self._pseudonymizer,
                deadline=reference_started + frozen.reference_timeout_seconds,
                cutoff=request.received_at,
                clock=monotonic,
            ).resolve(
                target=request.target,
                recent=prepared.predecessors,
                known_references=request.known_references,
            )
            references = frozen_input.references
            references_ms = (monotonic() - reference_started) * 1000.0
            if observation is not None:
                observation.references = frozen_input.references
            questions = build_questions(
                plan,
                # Prompt 快照不改写：只把实际提交的四个文本段落按文本字段脱敏。
                prompt_data=_sanitized_prompt_data(
                    prompt.prompt_data, known_secrets=_observation_secrets()
                ),
            )
            await self._require_current(request, decision_id)
            request_started = monotonic()
            response, parsed = await self._call_model(
                config=frozen.config,
                state=frozen_input.state,
                questions=questions,
                plan=plan,
            )
            # 上游成功已经发生；后续撤权或业务落库失败不能抹掉调用证据。
            self._observe_request(success=True, reason=None)
            durations = PhaseDurations(
                queue_ms=queue_ms,
                references_ms=references_ms,
                request_ms=(monotonic() - request_started) * 1000.0,
            )
            if observation is not None:
                observation.merge_durations(durations)
            await self._require_current(request, decision_id)
        except (AdmissionRevokedError, TaskRevokedError) as exc:
            durations = PhaseDurations(queue_ms=queue_ms, references_ms=references_ms)
            if observation is not None:
                observation.merge_durations(durations)
            return _Exchange(
                early=await self._revoked_result(
                    request,
                    decision_id,
                    exc,
                    durations=durations,
                    references=references,
                )
            )
        except asyncio.CancelledError:
            if observation is not None:
                observation.merge_durations(
                    PhaseDurations(
                        queue_ms=queue_ms,
                        references_ms=(
                            (monotonic() - reference_started) * 1000.0
                            if references is None
                            else references_ms
                        ),
                        request_ms=(
                            None
                            if request_started is None
                            else (monotonic() - request_started) * 1000.0
                        ),
                    )
                )
            raise
        except _ModelFailureError as failure:
            if failure.counts_as_request:
                self._observe_request(success=False, reason=failure.reason)
            # 只有真正开始过上游交换才记录请求阶段耗时；配置拒绝保持无证据。
            request_ms = (
                None
                if request_started is None or not failure.counts_as_request
                else (monotonic() - request_started) * 1000.0
            )
            durations = PhaseDurations(
                queue_ms=queue_ms,
                references_ms=references_ms,
                request_ms=request_ms,
            )
            if observation is not None:
                observation.merge_durations(durations)
            return _Exchange(
                failure=failure,
                references=references or ReferenceSummary(),
                durations=durations,
            )
        return _Exchange(
            response=response,
            parsed=parsed,
            references=frozen_input.references,
            durations=durations,
        )

    async def _failure_outcome(
        self,
        *,
        request: DecisionRequest,
        decision_id: str,
        frozen: _FrozenTask,
        prompt: StoredPrompt,
        outcome: _Exchange,
    ) -> DecisionResult:
        """技术失败：记录终态与通知资格，并如实报告原因码。"""
        failure = outcome.failure
        assert failure is not None
        await self._repository.finalize(
            identity=request.event,
            decision_id=decision_id,
            dispatch_id=request.dispatch_id,
            status=STATE_FAILED,
            reason_code=failure.reason,
            business_route="none",
            notification_eligible=self._notification_eligible(
                failure.reason, summary_candidate=request.summary_candidate
            ),
            recent_retained=None,
        )
        await self._settle_finished(request.event)
        return _terminal_result(
            "failed",
            failure.reason,
            decision_id=decision_id,
            durations=outcome.durations,
            references=outcome.references,
            config_revision=frozen.revision,
            prompt_revision=prompt.revision,
            requested_model=frozen.model,
        )

    async def _complete(
        self,
        *,
        request: DecisionRequest,
        decision_id: str,
        prepared: _Prepared,
        outcome: _Exchange,
    ) -> DecisionResult:
        """组合结果、写入终态（含允许路由）并定案输入窗口。"""
        frozen = prepared.frozen
        prompt = prepared.prompt
        response = outcome.response
        parsed = outcome.parsed
        assert frozen is not None
        assert prompt is not None
        assert response is not None
        assert parsed is not None
        result = self._compose(
            decision_id=decision_id,
            path=prepared.path,
            frozen=frozen,
            prompt=prompt,
            parsed=parsed,
            response=response,
            durations=outcome.durations,
            references=outcome.references,
        )
        finalized = await self._repository.finalize(
            identity=request.event,
            decision_id=decision_id,
            dispatch_id=request.dispatch_id,
            status=result.status,
            reason_code=result.reason_code,
            business_route=result.route,
            notification_eligible=False,
            recent_retained=result.retain_recent,
        )
        await self._settle_finished(request.event)
        if not finalized:
            return _terminal_result(
                "cancelled",
                "interrupted",
                decision_id=decision_id,
                durations=outcome.durations,
                references=outcome.references,
            )
        return result

    # ------------------------------ 组合 ------------------------------

    def _compose(
        self,
        *,
        decision_id: str,
        path: _Path,
        frozen: _FrozenTask,
        prompt: StoredPrompt,
        parsed: ParsedAnswers,
        response: SystemOneResponse,
        durations: PhaseDurations,
        references: ReferenceSummary,
    ) -> DecisionResult:
        base: dict[str, Any] = {
            "decision_id": decision_id,
            "reply_choice": parsed.reply,
            "retention_choice": parsed.retention,
            "summary_probability": parsed.summary,
            "references": references,
            "durations": durations,
            "usage": DecisionUsage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            ),
            "config_revision": frozen.revision,
            "prompt_revision": prompt.revision,
            "requested_model": frozen.model,
            "actual_model": response.model,
        }
        if path.kind == "platform_summary":
            return self._compose_platform_summary(base=base, summary=parsed.summary)
        return self._compose_chat(
            base=base,
            path=path,
            reply_choice=parsed.reply,
            retention_choice=parsed.retention,
            summary=parsed.summary,
            probability=frozen.optional_reply_probability,
        )

    def _compose_platform_summary(
        self, *, base: dict[str, Any], summary: float | None
    ) -> DecisionResult:
        if summary is None:
            msg = "平台召唤的总结候选缺少 Noul"
            raise DecisionProtocolError(msg)
        if summary > SUMMARY_THRESHOLD:
            return DecisionResult(
                status="success",
                reason_code="summary_matched",
                route="summary",
                retain_recent=True,
                memory_candidate=None,
                **base,
            )
        return DecisionResult(
            status="success",
            reason_code="platform_bypass",
            route="chat",
            force_reply=True,
            effective_reply="required",
            retain_recent=True,
            memory_candidate=True,
            optional_selected=None,
            **base,
        )

    def _compose_chat(
        self,
        *,
        base: dict[str, Any],
        path: _Path,
        reply_choice: ReplyChoice | None,
        retention_choice: RetentionChoice | None,
        summary: float | None,
        probability: float,
    ) -> DecisionResult:
        if reply_choice is None or retention_choice is None:
            msg = "普通判定缺少 Choice 答案"
            raise DecisionProtocolError(msg)
        if (
            path.kind == "full_summary"
            and summary is not None
            and summary > SUMMARY_THRESHOLD
        ):
            retain_recent, memory_candidate = _retention_flags(
                reply_choice.value, retention_choice.value
            )
            return DecisionResult(
                status="success",
                reason_code="summary_matched",
                route="summary",
                force_reply=False,
                effective_reply=None,
                retain_recent=retain_recent,
                memory_candidate=memory_candidate,
                optional_selected=None,
                **base,
            )
        retain_recent, memory_candidate = _retention_flags(
            reply_choice.value, retention_choice.value
        )
        if reply_choice.value == "required":
            return DecisionResult(
                status="success",
                reason_code="classified",
                route="chat",
                force_reply=True,
                effective_reply="required",
                retain_recent=retain_recent,
                memory_candidate=memory_candidate,
                optional_selected=None,
                **base,
            )
        if reply_choice.value == "optional":
            selected = float(self._random_source()) < probability
            return DecisionResult(
                status="success",
                reason_code="optional_selected" if selected else "optional_skipped",
                route="chat" if selected else "none",
                force_reply=False,
                effective_reply="optional" if selected else "silent",
                retain_recent=retain_recent,
                memory_candidate=memory_candidate,
                optional_selected=selected,
                **base,
            )
        return DecisionResult(
            status="success",
            reason_code="silent",
            route="none",
            force_reply=False,
            effective_reply="silent",
            retain_recent=retain_recent,
            memory_candidate=memory_candidate,
            optional_selected=None,
            **base,
        )

    # ------------------------------ 输入冻结 ------------------------------

    def _freeze_task(self) -> _FrozenTask | None:
        try:
            snapshot = self._config_source()
        except Exception:
            return None
        value = snapshot.value
        config = value.model_copy(deep=True)
        return _FrozenTask(
            config=config,
            revision=int(snapshot.revision),
            plugin_enable=bool(config.plugin_enable),
            bot_aliases=tuple(config.bot_aliases),
            queue_timeout_seconds=float(config.queue_timeout_seconds),
            reference_timeout_seconds=float(config.reference_timeout_seconds),
            request_timeout_seconds=float(config.jev_request_timeout_seconds),
            optional_reply_probability=float(config.optional_reply_probability),
            model=str(config.jev_model),
        )

    async def _freeze_recent(
        self, request: DecisionRequest
    ) -> tuple[DecisionMessage, ...]:
        try:
            return await self._recent.freeze_for_target(
                protocol=request.event.protocol,
                account_id=request.event.account_id,
                target=request.target,
                cutoff=request.received_at,
            )
        except Exception as exc:
            raise _DecisionInputUnavailableError from exc

    async def _confirm_delivered(self, request: DecisionRequest) -> None:
        with suppress(Exception):
            await self._recent.observe_confirmed(
                protocol=request.event.protocol,
                account_id=request.event.account_id,
                message=request.target,
            )

    async def _settle_entry(
        self,
        identity: EventIdentity,
        group_id: str,
        message_id: str,
        *,
        action: ResolveAction,
    ) -> None:
        """定案本次分发自己的条目（只作用于已存在的记录）。"""
        with suppress(Exception):
            await self._recent.resolve(
                protocol=identity.protocol,
                account_id=identity.account_id,
                group_id=group_id,
                message_id=message_id,
                action=action,
            )

    async def _resolve_recent(
        self,
        identity: EventIdentity,
        group_id: str,
        message_id: str,
        *,
        retain_recent: bool | None,
    ) -> None:
        await self._settle_entry(
            identity,
            group_id,
            message_id,
            action=resolve_action_for_retention(retain_recent=retain_recent),
        )

    # ------------------------------ 路径与就绪 ------------------------------

    def _resolve_path(
        self, request: DecisionRequest, frozen: _FrozenTask | None
    ) -> _Path:
        if not self._admission(request.event):
            return _ADMISSION_DENIED_PATH
        # functional_command 是「真实独立命令路由拥有该事件」的可信入口事实，
        # 与平台召唤无关：确定性召唤也不能把它变成普通聊天。
        if request.functional_command:
            return _FILTERED_PATH
        if request.platform_summoned:
            return self._summon_path(request, frozen)
        return self._ordinary_path(request, frozen)

    def _summon_path(
        self, request: DecisionRequest, frozen: _FrozenTask | None
    ) -> _Path:
        """平台召唤：非总结候选零名额直通；总结候选只问 Noul。"""
        if not request.summary_candidate:
            return _BYPASS_PATH
        if frozen is None:
            return _CONFIGURATION_INVALID_PATH
        if not frozen.plugin_enable:
            return _DISABLED_PATH
        return self._admitted_path(
            request, _Path(kind="platform_summary", input_eligible=True)
        )

    def _ordinary_path(
        self, request: DecisionRequest, frozen: _FrozenTask | None
    ) -> _Path:
        """普通正文：先按 TSK-180 §2 过滤，再按开关与总结候选分流。"""
        if frozen is None:
            return _CONFIGURATION_INVALID_PATH
        verdict = judge_ordinary_message(
            text=request.target.text, aliases=frozen.bot_aliases
        )
        if verdict.filtered:
            return _FILTERED_PATH
        if not frozen.plugin_enable:
            return _DISABLED_PATH
        kind: Literal["full_summary", "ordinary"] = (
            "full_summary" if request.summary_candidate else "ordinary"
        )
        return self._admitted_path(request, _Path(kind=kind, input_eligible=True))

    def _admitted_path(self, request: DecisionRequest, path: _Path) -> _Path:
        """准入在占位前先看一眼：受限事件只留最小身份，不缓存正文。"""
        if self._admission(request.event):
            return path
        return _ADMISSION_DENIED_PATH

    async def _ensure_ready(self, *, cancel_inflight: bool) -> None:
        async with self._readiness_lock:
            if self._closed:
                return
            await self._ensure_ownership()
            await self._recheck_readiness(cancel_inflight=cancel_inflight)
            self._initialized = True

    async def _ensure_ownership(self) -> None:
        """取得持久所有权；失败保持 failed，可在配置修正后重试。

        租约丢失后必须丢弃旧租约并创建**新的**仓储实例：旧对象既不复活旧资格，
        也不会把已关闭状态带进新incarnation。
        """
        if self._repository.ready and not self._lease_lost:
            return
        if self._lease_lost or self._repository.ready:
            await self._discard_ownership()
        self._ownership_attempted = True
        try:
            await self._repository.initialize()
            self._pseudonymizer = DecisionPseudonymizer(self._repository.pseudonym_salt)
        except Exception as exc:
            logger.warning(
                "[KomariDecision] Jev 判定持久化初始化失败: error={}",
                type(exc).__name__,
            )
            self._pseudonymizer = None
            return
        self._leased = True
        self._lease_lost = False
        self._state_observed_at = datetime.now(UTC)
        if self._watch_task is None or self._watch_task.done():
            self._watch_task = asyncio.create_task(
                self._watch_owned_work(), name="jev-decision-watch"
            )

    async def _discard_ownership(self) -> None:
        """取消并等待旧工作、释放旧租约，再从同一会话工厂重建仓储实例。"""
        watch_task = self._watch_task
        self._watch_task = None
        if watch_task is not None:
            watch_task.cancel()
            with suppress(BaseException):
                await watch_task
        owned = list(self._dispatches.values())
        for dispatch in owned:
            dispatch.cancelling = True
            dispatch.task.cancel()
        if owned:
            await asyncio.gather(
                *(dispatch.task for dispatch in owned), return_exceptions=True
            )
        self._dispatches.clear()
        self._results.clear()
        self._revoked.clear()
        self._admission_revoked.clear()
        previous = self._repository
        self._repository = JevDecisionRepository(self._session_factory)
        self._pseudonymizer = None
        self._leased = False
        with suppress(Exception):
            await previous.close()

    async def _recheck_readiness(self, *, cancel_inflight: bool) -> None:
        try:
            snapshot = self._config_source()
        except Exception:
            self._set_runtime("failed", "configuration_invalid")
            return
        value = snapshot.value
        if not value.plugin_enable:
            self._set_runtime("disabled", "disabled")
            if cancel_inflight:
                self._cancel_owned()
            return
        if not self._repository.ready:
            # 没有持久所有者就绝不允许报告 ready（初始化尚未尝试时如实说明）。
            self._set_runtime(
                "failed",
                "storage_unavailable" if self._ownership_attempted else "initializing",
            )
            return
        problem = _request_configuration_problem(value)
        if problem is not None:
            self._set_runtime("failed", problem)
            return
        if await self._load_prompt() is None:
            self._set_runtime("failed", "prompt_unavailable")
            return
        self._set_runtime("ready", "ready")

    async def _load_prompt(self) -> StoredPrompt | None:
        try:
            return await self._prompt_loader.get_versioned_snapshot_async()
        except Exception as exc:
            logger.warning(
                "[KomariDecision] Jev Prompt 快照不可用: error={}", type(exc).__name__
            )
            return None

    def _set_runtime(
        self,
        status: Literal["ready", "disabled", "failed"],
        reason: DecisionReason,
    ) -> None:
        self._health.runtime_status = status
        self._health.reason_code = reason
        self._state_observed_at = datetime.now(UTC)

    def _observe_request(self, *, success: bool, reason: DecisionReason | None) -> None:
        now = datetime.now(UTC)
        self._state_observed_at = now
        if success:
            self._health.last_request_status = "success"
            self._health.last_success_at = now
            self._health.consecutive_failures = 0
            return
        self._health.last_request_status = "failure"
        self._health.last_failure_at = now
        self._health.last_failure_reason_code = reason
        self._health.consecutive_failures += 1

    @staticmethod
    def _notification_eligible(
        reason: DecisionReason, *, summary_candidate: bool
    ) -> bool:
        if reason in _SILENT_REASONS:
            return False
        if reason in _NOTIFICATION_REASONS:
            return True
        return summary_candidate and reason in _SUMMARY_NOTIFICATION_REASONS

    # ------------------------------ SDK 请求 ------------------------------

    async def _call_model(
        self,
        *,
        config: KomariDecisionConfigSchema,
        state: Mapping[str, Any],
        questions: Mapping[str, Question],
        plan: QuestionPlan,
    ) -> tuple[SystemOneResponse, ParsedAnswers]:
        """在请求总截止内完成调用**与**应用级答案校验。"""
        transport = (
            None if self._transport_factory is None else self._transport_factory()
        )
        observation = _current_observation()
        secrets = _observation_secrets()
        try:
            async with configured_jev_request(config, transport=transport) as client:
                # 真实请求边界：先按冻结凭据脱敏，再把同一份对象交给 SDK 与正文日志。
                supplied_state = _request_state(state, known_secrets=secrets)
                supplied_questions = _request_questions(
                    questions, known_secrets=secrets
                )
                if observation is not None:
                    observation.capture_request(
                        state=supplied_state, questions=supplied_questions
                    )
                response = await client.system_one(
                    state=supplied_state, questions=questions
                )
                # 在应用级校验之前保留真实响应体与状态码（未知字段只在正文里）。
                if observation is not None:
                    observation.note_response(response)
                parsed = parse_answers(response, plan)
        except DecisionProtocolError as exc:
            raise _ModelFailureError("protocol_error") from exc
        except JevRequestConfigurationError as exc:
            # 在创建客户端前被拒：不是上游请求失败。
            raise _ModelFailureError(
                "configuration_invalid", counts_as_request=False
            ) from exc
        except JevRequestTimeoutError as exc:
            raise _ModelFailureError("request_timeout") from exc
        except TypeSafeAPITimeoutError as exc:
            raise _ModelFailureError("request_timeout") from exc
        except (TypeSafeAuthenticationError, TypeSafePermissionDeniedError) as exc:
            _note_error_response(observation, exc)
            raise _ModelFailureError("authentication_error") from exc
        except TypeSafeRateLimitError as exc:
            _note_error_response(observation, exc)
            raise _ModelFailureError("rate_limited") from exc
        except TypeSafeAPIConnectionError as exc:
            raise _ModelFailureError("network_error") from exc
        except TypeSafeAPIResponseValidationError as exc:
            _note_error_response(observation, exc)
            raise _ModelFailureError("protocol_error") from exc
        except TypeSafeAPIError as exc:
            _note_error_response(observation, exc)
            raise _ModelFailureError("provider_error") from exc
        except TimeoutError as exc:
            raise _ModelFailureError("request_timeout") from exc
        return response, parsed

    # ------------------------------ 守卫与观察 ------------------------------

    async def _require_current(
        self, request: DecisionRequest, decision_id: str
    ) -> None:
        """外部效果前：先核对持久任务仍是当前任务，再做同步准入裁决。"""
        if self._closed or self._lease_lost:
            raise TaskRevokedError
        key = (request.event, request.dispatch_id)
        if key in self._revoked:
            raise TaskRevokedError
        current = await self._repository.is_current(
            identity=request.event,
            decision_id=decision_id,
            dispatch_id=request.dispatch_id,
        )
        if not current:
            self._revoked.add(key)
            raise TaskRevokedError
        if not self._admission(request.event):
            raise AdmissionRevokedError

    async def _revoke(self, request: DecisionRequest, decision_id: str) -> None:
        key = (request.event, request.dispatch_id)
        self._revoked.add(key)
        self._admission_revoked.add(key)
        await self._repository.persist_revocation(
            identity=request.event,
            decision_id=decision_id,
            dispatch_id=request.dispatch_id,
        )

    async def _watch_owned_work(self) -> None:
        """观察租约、持久撤销与准入变化；取消受影响的本地任务（粘性）。"""
        while not self._closed:
            await asyncio.sleep(_WATCH_POLL_SECONDS)
            if not self._leased:
                return
            if not await self._repository.check_lease():
                self._lease_lost = True
                self._leased = False
                self._set_runtime("failed", "storage_unavailable")
                logger.warning(
                    "[KomariDecision] Jev 判定租约丢失；取消自有任务并拒绝陈旧完成/交接"
                )
                self._cancel_owned()
                return
            # 先取本地跟踪快照，再用**同一快照**查询与判定：本轮新登记的任务不在
            # 观测集内，绝不因为「没查到」而被推断为已撤销。
            snapshot = [
                (key, owned)
                for key, owned in self._dispatches.items()
                if owned.registered and not owned.task.done()
            ]
            retained = list(self._results.items())
            tracked = [key for key, _ in snapshot] + [key for key, _ in retained]
            try:
                rows = await self._repository.observe_owned_work(tracked)
            except Exception:
                continue
            by_key = {(row.identity, row.dispatch_id): row for row in rows}
            # 本地保留的已完成结果：持久撤销/代数变化 → 阻止交接；观察到的撤权
            # 同样持久化并保持粘性（有界墓碑，随本地结果淘汰而释放）。
            for key, result in retained:
                if self._results.get(key) is not result:
                    continue
                row = by_key.get(key)
                stale = row is not None and (
                    not row.generation_current or row.state == STATE_CANCELLED
                )
                if stale:
                    self._revoked.add(key)
                    continue
                if self._admission(key[0]):
                    continue
                self._revoked.add(key)
                self._admission_revoked.add(key)
                with suppress(Exception):
                    await self._repository.persist_revocation(
                        identity=key[0],
                        decision_id=result.decision_id,
                        dispatch_id=key[1],
                    )
            for key, owned in snapshot:
                if self._dispatches.get(key) is not owned or owned.task.done():
                    continue
                row = by_key.get(key)
                if row is None:
                    continue  # 未观测到：不做撤销推断
                if not row.generation_current or row.state == STATE_CANCELLED:
                    self._revoked.add(key)
                    if not owned.cancelling:
                        owned.cancelling = True
                        owned.task.cancel()
                    continue
                if self._admission(owned.identity):
                    continue
                self._revoked.add(key)
                self._admission_revoked.add(key)
                with suppress(Exception):
                    await self._repository.persist_revocation(
                        identity=owned.identity,
                        decision_id=owned.decision_id,
                        dispatch_id=key[1],
                    )
                if not owned.cancelling:
                    owned.cancelling = True
                    owned.task.cancel()

    def _cancel_owned(self) -> None:
        for owned in tuple(self._dispatches.values()):
            owned.cancelling = True
            owned.task.cancel()

    def _terminal_from_plan(self, decision_id: str, path: _Path) -> DecisionResult:
        spec = _TERMINAL_SPECS.get(path.kind, _INTERNAL_ERROR_SPEC)
        return _terminal_result(
            spec.status,
            spec.reason,
            decision_id=decision_id,
            route=spec.route,
            force_reply=spec.force_reply,
            effective_reply=spec.effective_reply,
            retain_recent=spec.retain_recent,
            memory_candidate=spec.memory_candidate,
        )

    def _dispatch_finished(
        self, key: tuple[EventIdentity, str], task: asyncio.Task[DecisionResult]
    ) -> None:
        self._dispatches.pop(key, None)
        if task.cancelled():
            self._revoked.discard(key)
            self._admission_revoked.discard(key)
            return
        with suppress(Exception):
            result = task.result()
            if len(self._results) >= _MAX_RETAINED_RESULTS:
                evicted, _ = self._results.popitem(last=False)
                # 墓碑有界：本地行已淘汰，持久撤销仍会阻止陈旧结果的交接。
                self._revoked.discard(evicted)
                self._admission_revoked.discard(evicted)
            self._results[key] = result


@dataclass(frozen=True, slots=True, kw_only=True)
class _Path:
    """入口路径（决定问题组合与是否占执行名额）。"""

    kind: Literal[
        "filtered",
        "bypass",
        "platform_summary",
        "full_summary",
        "ordinary",
        "disabled",
        "admission_denied",
        "configuration_invalid",
        "prompt_failure",
    ]
    terminal_state: str | None = None
    terminal_reason: str | None = None
    input_eligible: bool = False
    """通过前置过滤并被准入的合格原文：技术失败时仍需留在输入窗口。"""

    @property
    def wants_slot(self) -> bool:
        return self.kind in {"platform_summary", "full_summary", "ordinary"}

    @property
    def needs_prompt(self) -> bool:
        return self.wants_slot

    @property
    def question_plan(self) -> QuestionPlan:
        if self.kind == "ordinary":
            return QuestionPlan(reply=True, retention=True)
        if self.kind == "full_summary":
            return QuestionPlan(reply=True, retention=True, summary=True)
        if self.kind == "platform_summary":
            return QuestionPlan(summary=True)
        return QuestionPlan()


def _admission_denied_plan(plan: ReservePlan) -> ReservePlan:
    """锁内复检发现撤权：只提交最小 cancelled/admission_revoked 身份。"""
    return replace(
        plan,
        wants_slot=False,
        terminal_state=STATE_CANCELLED,
        terminal_reason="admission_revoked",
        failure_notification=False,
    )


def _input_failure_plan(plan: ReservePlan, request: DecisionRequest) -> ReservePlan:
    """输入边界失败：同事务写 failed/input_unavailable，不留名额/路由。"""
    del request
    return replace(
        plan,
        wants_slot=False,
        terminal_state=STATE_FAILED,
        terminal_reason="input_unavailable",
        failure_notification=True,
    )


def _first_value[ValueT](
    preferred: ValueT | None, fallback: ValueT | None
) -> ValueT | None:
    """取已知值（首选显式事实，其次观测事实）。"""
    return preferred if preferred is not None else fallback


def _first_float(preferred: float | None, fallback: float | None) -> float | None:
    """阶段耗时的取值顺序：先用显式测量值，再用观测到的值。"""
    return preferred if preferred is not None else fallback


def _sanitized_prompt_data(
    prompt_data: Mapping[str, str], *, known_secrets: tuple[str, ...]
) -> dict[str, str]:
    """四个可编辑 Prompt 段落按文本字段脱敏，固定问题定义不经这里。

    Prompt 快照本身（以及其中的正文）不被改写；整段媒体载荷换成固定占位符，
    但仍保留同名问题，不因一段媒体而丢掉整个必需问题。
    """
    return {
        name: sanitize_text_field(value, key=name, known_secrets=known_secrets)
        for name, value in prompt_data.items()
    }


def _request_state(
    state: Mapping[str, Any], *, known_secrets: tuple[str, ...]
) -> dict[str, Any]:
    """实际提交的 state：按冻结凭据与媒体边界脱敏后的同构对象。"""
    projected = sanitize_log_value(state, known_secrets=known_secrets)
    if not isinstance(projected, dict):
        msg = "判定 state 的脱敏投影不是 JSON 对象"
        raise DecisionProtocolError(msg)
    return projected


def _request_questions(
    questions: Mapping[str, Question], *, known_secrets: tuple[str, ...]
) -> dict[str, Any]:
    """实际提交的问题集：用 SDK 公开序列化得到与请求体一致的 JSON 形状。"""
    payload: dict[str, Any] = {}
    for name, question in questions.items():
        model_dump = getattr(question, "model_dump", None)
        raw = model_dump(mode="json") if callable(model_dump) else question
        projected = sanitize_log_value(raw, key=name, known_secrets=known_secrets)
        if not isinstance(projected, dict):
            msg = "判定问题的脱敏投影不是 JSON 对象"
            raise DecisionProtocolError(msg)
        payload[name] = projected
    return payload


def _raw_response_facts(
    response: SystemOneResponse,
) -> tuple[Any, int | None]:
    """从 SDK 公共属性取真实 HTTP 响应正文与状态码（不序列化异常/头/地址）。"""
    try:
        raw = response.raw_http_response
    except Exception:  # 无原始响应时明确保持未知
        return None, None
    status = int(raw.status_code)
    try:
        return raw.json(), status
    except ValueError:
        return raw.text, status


def _note_error_response(
    observation: _DiagnosticObservation | None, error: TypeSafeAPIError
) -> None:
    """失败响应证据只取 SDK 公开的 body/status（网络/超时无响应时保持空）。"""
    if observation is not None:
        observation.note_error_response(error)


async def _complete_owned(task: asyncio.Task[Any]) -> None:
    """等待一个自有任务真正结束；期间的取消延后给调用方处理。"""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:  # 任务异常在下面统一读取
            break
    with suppress(BaseException):
        task.result()


async def _await_owned_log(task: asyncio.Task[None]) -> None:
    """等待一次已开始的诊断写入完；期间的取消在写完之后再传播。"""
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
        except BaseException:  # 任务异常在下面统一读取
            break
    with suppress(BaseException):
        task.result()
    if cancellation is not None:
        raise cancellation


@dataclass(frozen=True, slots=True, kw_only=True)
class _Prepared:
    """占位完成后继续执行所需的冻结事实（或早退终态）。"""

    early: DecisionResult | None = None
    frozen: _FrozenTask | None = None
    prompt: StoredPrompt | None = None
    path: _Path = field(default_factory=lambda: _Path(kind="ordinary"))
    predecessors: tuple[DecisionMessage, ...] = ()
    reservation: Reservation = field(
        default_factory=lambda: Reservation(kind=ReservationKind.UNAVAILABLE)
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _Exchange:
    """一次「引用补取 + SDK 请求」的结果三元组。"""

    early: DecisionResult | None = None
    failure: _ModelFailureError | None = None
    response: SystemOneResponse | None = None
    parsed: ParsedAnswers | None = None
    references: ReferenceSummary = field(default_factory=ReferenceSummary)
    durations: PhaseDurations = field(default_factory=PhaseDurations)


@dataclass(frozen=True, slots=True, kw_only=True)
class _TerminalSpec:
    """一类入口路径对应的终态结果形状。"""

    status: DecisionStatus
    reason: DecisionReason
    route: Literal["none", "chat", "summary"] = "none"
    force_reply: bool = False
    effective_reply: Literal["required", "optional", "silent"] | None = None
    retain_recent: bool | None = None
    memory_candidate: bool | None = None


_FILTERED_PATH: Final = _Path(
    kind="filtered", terminal_state=STATE_FILTERED, terminal_reason="filtered"
)
_BYPASS_PATH: Final = _Path(
    kind="bypass",
    terminal_state=STATE_BYPASS,
    terminal_reason="platform_bypass",
    input_eligible=True,
)
_DISABLED_PATH: Final = _Path(
    kind="disabled", terminal_state=STATE_FAILED, terminal_reason="disabled"
)
_ADMISSION_DENIED_PATH: Final = _Path(
    kind="admission_denied",
    terminal_state=STATE_CANCELLED,
    terminal_reason="admission_revoked",
)
_CONFIGURATION_INVALID_PATH: Final = _Path(
    kind="configuration_invalid",
    terminal_state=STATE_FAILED,
    terminal_reason="configuration_invalid",
)

_INTERNAL_ERROR_SPEC: Final = _TerminalSpec(status="failed", reason="internal_error")

_TERMINAL_SPECS: Final[dict[str, _TerminalSpec]] = {
    "filtered": _TerminalSpec(status="filtered", reason="filtered"),
    "bypass": _TerminalSpec(
        status="bypass",
        reason="platform_bypass",
        route="chat",
        force_reply=True,
        effective_reply="required",
        retain_recent=True,
        memory_candidate=True,
    ),
    "disabled": _TerminalSpec(status="failed", reason="disabled"),
    "admission_denied": _TerminalSpec(status="cancelled", reason="admission_revoked"),
    "configuration_invalid": _TerminalSpec(
        status="failed", reason="configuration_invalid"
    ),
    "prompt_failure": _TerminalSpec(status="failed", reason="prompt_unavailable"),
}

_RESERVATION_OUTCOMES: Final[
    dict[ReservationKind, tuple[DecisionStatus, DecisionReason]]
] = {
    ReservationKind.DUPLICATE: ("duplicate", "duplicate_event"),
    ReservationKind.REUSED: ("duplicate", "duplicate_event"),
    ReservationKind.UNAVAILABLE: ("failed", "storage_unavailable"),
    ReservationKind.DISABLED: ("failed", "disabled"),
    ReservationKind.QUEUE_FULL: ("failed", "queue_full"),
}


def _default_session_factory() -> Callable[[], AsyncSession]:
    def factory() -> AsyncSession:
        from nonebot_plugin_orm import get_session

        return get_session(expire_on_commit=False)

    return factory


_RETENTION_MATRIX: Final[
    dict[tuple[str | None, str | None], tuple[bool | None, bool | None]]
] = {
    ("required", "candidate"): (True, True),
    ("required", "noise"): (True, True),
    ("optional", "candidate"): (True, True),
    ("optional", "noise"): (True, True),
    ("silent", "candidate"): (True, True),
    ("silent", "noise"): (False, False),
    (None, "candidate"): (True, True),
    (None, "noise"): (False, False),
}
"""TSK-180 的 3x2 去向矩阵：required/optional 一律保留；silent+noise 排除。"""


def _retention_flags(
    reply_value: str | None, retention_value: str | None
) -> tuple[bool | None, bool | None]:
    """按 3x2 矩阵把原始分类映射为保留/记忆候选资格。"""
    return _RETENTION_MATRIX.get((reply_value, retention_value), (None, None))


def _request_configuration_problem(
    value: KomariDecisionConfigSchema,
) -> DecisionReason | None:
    """就绪检查：用公开归一化器确认请求配置齐全（不发请求）。"""
    from komari_bot.plugins.komari_decision.config_schema import (
        normalize_jev_api_key,
        normalize_jev_base_url,
        normalize_jev_model,
        normalize_jev_timeout_seconds,
    )

    try:
        base_url = normalize_jev_base_url(value.jev_base_url)
        api_key = normalize_jev_api_key(value.jev_api_key)
        model = normalize_jev_model(value.jev_model)
        normalize_jev_timeout_seconds(
            value.jev_http_timeout_seconds, field_name="jev_http_timeout_seconds"
        )
        normalize_jev_timeout_seconds(
            value.jev_request_timeout_seconds,
            field_name="jev_request_timeout_seconds",
        )
    except ValueError:
        return "configuration_invalid"
    if not (base_url and api_key and model):
        return "configuration_invalid"
    return None


def _coerce_reason(
    value: str, *, default: DecisionReason = "internal_error"
) -> DecisionReason:
    known: frozenset[str] = frozenset(
        {
            "classified",
            "summary_matched",
            "optional_selected",
            "optional_skipped",
            "silent",
            "filtered",
            "platform_bypass",
            "duplicate_event",
            "queue_full",
            "queue_timeout",
            "request_timeout",
            "network_error",
            "rate_limited",
            "authentication_error",
            "provider_error",
            "protocol_error",
            "input_unavailable",
            "disabled",
            "admission_revoked",
            "interrupted",
            "configuration_invalid",
            "prompt_unavailable",
            "storage_unavailable",
            "initializing",
            "ready",
            "internal_error",
        }
    )
    return cast("DecisionReason", value) if value in known else default


def _terminal_result(
    status: DecisionStatus,
    reason: DecisionReason,
    *,
    decision_id: str,
    route: Literal["none", "chat", "summary"] = "none",
    force_reply: bool = False,
    effective_reply: Literal["required", "optional", "silent"] | None = None,
    retain_recent: bool | None = None,
    memory_candidate: bool | None = None,
    durations: PhaseDurations | None = None,
    references: ReferenceSummary | None = None,
    config_revision: int | None = None,
    prompt_revision: int | None = None,
    requested_model: str | None = None,
) -> DecisionResult:
    return DecisionResult(
        decision_id=decision_id,
        status=status,
        reason_code=reason,
        route=route,
        force_reply=force_reply,
        effective_reply=effective_reply,
        retain_recent=retain_recent,
        memory_candidate=memory_candidate,
        durations=durations or PhaseDurations(),
        references=references or ReferenceSummary(),
        config_revision=config_revision,
        prompt_revision=prompt_revision,
        requested_model=requested_model,
    )


__all__ = ["SUMMARY_THRESHOLD", "JevDecisionEngine"]
