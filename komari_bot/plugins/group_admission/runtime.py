"""准入运行时：进程内不可变快照聚合与 ConfigManager 版本化快照适配。

本模块是 ``group_admission`` 包内部 seam，不作顶层公开。

修订接纳模型
  - 只接纳严格更高修订：合法编译后 READY；更高非法修订有最近有效策略（LKG）
    时 DEGRADED 保留 LKG，无 LKG 收敛 FAILED；相同 / 较低 / 乱序修订忽略（乱序
    记 stale WARNING，范围内只累计不改变聚合）。
  - 冷启动存储异常收敛 FAILED/storage_unavailable，不向调用方冒泡
    （CancelledError 保持取消语义）；不另造重试任务，后续重连正常回填。

安全与可观测性
  - 所有结构化日志与额外输出（聚合 map / 通知描述符 / 状态投影）只携带低基数
    枚举 / 时间 / 计数字段；绝不携带群号、策略正文、消息 ID、原始异常或完整
    正文。
  - 归属失败按 event_family 独立 300 秒窗口：首条 WARNING 并排队通知描述符，
    窗内只累计；满窗由 ``process_observability`` 输出安全摘要并结算（幂等、
    同一窗口至多一条摘要）。
  - 故障 episode：进入 failed/degraded 写起始日志 + ``fault_start`` 卡；每
    1800 秒边界记一条提醒（级别随当前状态、同 tick 幂等）；READY 稳定 60 秒
    恰一次恢复日志并清空 episode（离线 fault→recovery 合并为单一卡）。
  - ``process_observability`` 是唯一异步可见接缝：持有同一把锁串行化窗口结算 /
    提醒 / 恢复，并在该锁内尽力投递 pending 通知；``close`` 后一切为 no-op。

并发与投递
  - ``start`` 单飞；观测 / 遥测与私有持久化 / strict-CAS 写分离，线程锁绝不跨
    ``await`` 持有。
  - 通知投递尽力而为：无在线 Bot / 无合法收件人整批保留重试；渲染成功后才冻结
    收件人；每收件人按 Bot 顺序 fallback、首个成功即中断，绝不重复投递成功收件
    人；渲染异常绝不污染任何 pending 状态。
  - 管理控制面：``/status`` 只返回稳定低基数键；私有刷新与严格 CAS 修改的错误
    收敛为固定 code，冲突明确失败、不重读、不重试。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from nonebot import logger

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

#: 可观测性聚合窗口（秒）：归属失败与乱序/重复修订两窗口均固定 5 分钟。
_OBSERVABILITY_WINDOW_SECONDS = 300.0

#: 乱序/重复策略修订的抑制窗口（秒）：窗口首条 WARNING，窗口内只累计。
_STALE_REVISION_WINDOW_SECONDS = _OBSERVABILITY_WINDOW_SECONDS

#: 结构化日志固定事件名（extra.event，与 observability 冻结事件名一致）。
_EVENT_POLICY_PUBLISHED = "group_admission.policy_published"
_EVENT_STALE_REVISION_IGNORED = "group_admission.stale_revision_ignored"
_EVENT_ATTRIBUTION_UNAVAILABLE = "group_admission.attribution_unavailable"

#: 故障陈述 log 事件名（extra.event，与 observability 冻结事件名一致）。
_EVENT_RUNTIME_FAILED = "group_admission.runtime_failed"
_EVENT_RUNTIME_DEGRADED = "group_admission.runtime_degraded"
_EVENT_RUNTIME_REMINDER = "group_admission.runtime_reminder"
_EVENT_RUNTIME_RECOVERED = "group_admission.runtime_recovered"

#: 故障提醒边界（秒：冻结 30 分钟）与恢复稳定窗（秒：冻结 60 秒）。
_REMINDER_INTERVAL_SECONDS = 1800.0
_RECOVERY_STABILITY_SECONDS = 60.0

#: pending 安全通知描述符种类。
_NOTICE_KIND_START = "fault_start"
_NOTICE_KIND_REMINDER = "reminder"
_NOTICE_KIND_RECOVERED = "recovered"
_NOTICE_KIND_ATTRIBUTION = "attribution"
_NOTICE_KIND_COMBINED = "combined"

#: policy_published source 封闭值集（startup/watcher/management）。
_POLICY_SOURCE_STARTUP = "startup"
_POLICY_SOURCE_WATCHER = "watcher"
_POLICY_SOURCE_MANAGEMENT = "management"


def _canonical_policy_fingerprint(mode: str, group_ids: frozenset[int]) -> str:
    """规范化策略的 SHA-256 指纹（审计安全字段，只含摘要不含群号明文）。

    规范化序列化对齐管理参照真源：键排序、紧凑分隔符、保留非 ASCII；
    ``group_ids`` 已由编译去重，此处确定性排序后参与哈希。
    """
    canonical = json.dumps(
        {"mode": mode, "group_ids": sorted(group_ids)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _default_online_bots_provider() -> Mapping[str, object] | Iterable[object]:
    """生产默认在线 Bot 枚举提供者；NoneBot 运行时不可用时安全返回空。"""
    try:
        from nonebot import get_bots
    except Exception:
        return {}
    try:
        return get_bots()
    except Exception:
        return {}


def _default_superusers_provider() -> Iterable[object]:
    """生产默认 SUPERUSER 提供者；NoneBot 运行时不可用时安全返回空。"""
    try:
        from nonebot import get_driver
    except Exception:
        return ()
    try:
        return set(get_driver().config.superusers)
    except Exception:
        return ()


def _format_rfc3339(value: datetime) -> str:
    """将 ``datetime`` 统一到 UTC 后输出 RFC3339 字符串（``+00:00`` 后缀）。

    ``value`` 应为 aware datetime（本项目运行时时钟恒为
    ``datetime.now(UTC)``），naive 输入按本地时区解释后转换。
    """
    return value.astimezone(UTC).isoformat()


def _next_reminder_boundary(started_at: datetime, now: datetime) -> datetime:
    """返回严格大于 ``now`` 的原 30 分钟网格边界（``started_at + k*1800``）。

    提醒边界锚定在 episode 起始时刻的原始网格上（与 ``_record_problem_``
    ``occurrence_locked`` 初始 ``next_reminder_at = 开始+1800`` 对齐）。
    ``k = floor((now - started_at)/1800) + 1`` 保证结果严格大于 ``now``
    （跨越多个间隔时直接跳到跨越后的下一个网格点）：发出一条提醒后把
    ``next_reminder_at`` 推进到本函数的结果，使同一时刻重复 process 幂等、
    不爆发循环。
    """
    elapsed = (now - started_at).total_seconds()
    intervals = int(elapsed // _REMINDER_INTERVAL_SECONDS) + 1
    return started_at + timedelta(
        seconds=intervals * _REMINDER_INTERVAL_SECONDS
    )


# ---------------------------------------------------------------------------
# 控制面固定错误 / 结果类型（不携带 raw 异常）
# ---------------------------------------------------------------------------

_CONTROL_PLANE_RUNTIME_UNAVAILABLE = "runtime_unavailable"
_CONTROL_PLANE_STORAGE_ERROR = "storage_error"
_CONTROL_PLANE_CAS_CONFLICT = "cas_conflict"
_CONTROL_PLANE_UNKNOWN_FIELD = "unknown_field"


class _ControlPlaneError(Exception):
    """控制面固定错误类型，只携带 code / 消息，不携带 raw 存储 / 解析异常。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _ControlPlaneConflictError(_ControlPlaneError):
    """CAS 修订冲突：明确失败，不重读、不重试。"""


@dataclass(frozen=True, slots=True)
class _ControlPlaneApplyResult:
    """一次 PUT 持久化更新的结果（成功路径；冲突以异常表达）。"""

    new_revision: int
    published_locally: bool


@dataclass(slots=True)
class _PendingNotification:
    """待投递的安全通知描述符。

    只携带低基数枚举 / 时间 / 计数字段，绝不携带群号、策略正文、消息 ID 或原始异常；
    由 ``_deliver_pending_notifications`` 消费并投递 SUPERUSER 卡。
    """

    kind: str
    status: str
    problem_code: AdmissionProblemCode | None
    started_at: datetime
    occurrence_count: int
    duration_seconds: int | None
    resolved_at: datetime | None = None
    pending_recipients: list[int] | None = None
    event_family: str | None = None


@dataclass(slots=True)
class _FaultEpisode:
    """单个故障 episode 的可变簿记。

    ``started_at`` 与 ``problem_since`` 同值；``occurrence_count`` 为本 episode 内累
    计的故障次数（含首次）；``next_reminder_at`` 为下一 1800 秒提醒边界；
    ``ready_since`` 为进入连续 READY 稳定期的时刻（稳定期内再故障会清除）；
    ``problem_code`` 为 episode 开启时的故障码（恢复描述符保存故障身份）；
    ``counted_codes`` 记录本 episode 内问题码的去重计数（同码只计第一次）。
    """

    started_at: datetime
    occurrence_count: int
    next_reminder_at: datetime
    ready_since: datetime | None
    problem_code: AdmissionProblemCode
    counted_codes: set[str]


@dataclass(slots=True)
class _AttributionWindow:
    """单个 event_family 的归属失败 300 秒聚合窗口。

    低基数累计（``_telemetry_family``）不受窗口影响；首条归因失败开新窗口并
    立即记 WARNING，同窗后续只累计，满窗时经 ``process_observability`` 输出
    安全摘要后移除本窗口（幂等）。窗口首条即构造并排队一张 ``attribution``
    pending 描述符（``kind=_NOTICE_KIND_ATTRIBUTION``，只含安全字段）；同一
    窗口内 ``count`` 累加时同步更新该描述符 ``occurrence_count``（同一 mutable
    对象）；满窗开启新窗口时按新窗口重新构造一张描述符，绝不改写旧描述符。
    """

    window_start: datetime
    count: int
    notification: _PendingNotification


@runtime_checkable
class _SendableBot(Protocol):
    """通知投递所需的在线 Bot 最小接口面。

    只要求 ``send_private_msg``（与 OneBot V11 Bot 同形）；投递接缝对不具备该
    callable 的 Bot 直接跳过，绝不因缺少该 callable 抛错。
    """

    async def send_private_msg(self, *, user_id: int, message: object) -> object: ...


def _parse_superuser_id(raw: object) -> int | None:
    """把一条 SUPERUSER 配置条目解析为合法正整数 QQ 号；非法条目返回 ``None``。

    接受正 ``int``（显式排除 ``bool``，其是 ``int`` 子类）与纯数字 ``str``
    （生产 NoneBot 配置可能以字符串形态提供，如 ``"10001"``）；``float`` /
    ``None`` / 负号 / 零 / 非法文本一律跳过；``str`` 转数值失败也跳过。
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, str):
        if not raw.isdigit():
            return None
        try:
            parsed = int(raw)
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


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

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        online_bots_provider: Callable[
            [], Mapping[str, object] | Iterable[object]
        ]
        | None = None,
        superusers_provider: Callable[[], Iterable[object]] | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._started_at = self._clock()
        self._online_bots_provider = (
            online_bots_provider or _default_online_bots_provider
        )
        self._superusers_provider = (
            superusers_provider or _default_superusers_provider
        )

        self._aggregate: _RuntimeAggregate = _INITIAL_AGGREGATE
        self._manager: ConfigManager | None = None
        self._publish_lock = RLock()
        self._telemetry_lock = RLock()
        # pending 通知队列与描述符可变投递状态（``pending_recipients`` 及各通知
        # 字段）唯一同步锁。锁序只允许 publish/telemetry → pending，绝不反向；
        # 投递时在锁内快照/渲染/拷贝收件人后立即释放，绝不持本锁跨 ``await
        # send_private_msg``。
        self._pending_lock = RLock()
        self._start_lock = asyncio.Lock()
        self._obs_process_lock = asyncio.Lock()
        self._started = False
        self._closed = False

        # 乱序/重复快照 5 分钟抑制窗口：窗口首条 WARNING、窗内只累计，满窗
        # 经 ``process_observability`` 输出安全摘要并结束窗口。
        self._stale_window_start: datetime | None = None
        self._stale_suppressed_count = 0
        # 每 event_family 独立的归属失败 300 秒聚合窗口：首条即记日志并排队
        # 通知描述符，满窗经 ``process_observability`` 输出安全摘要并移除窗口。
        self._attribution_windows: dict[str, _AttributionWindow] = {}
        # 管理面严格 CAS 正在等待 listener 发布的新修订集合：listener 投递时
        # 用于把 ``policy_published`` source 归因到 "management" 而非 watcher。
        self._management_source_revisions: set[int] = set()
        # startup 阶段：``start`` 初始化/缓存消费期间的 listener 投递归因
        # "startup"（首个快照恰一条），phase 结束前不视为 watcher。
        self._startup_phase = False

        # 时间锚点（aware UTC；未发生为 None）
        self._configured_updated_at: datetime | None = None
        self._effective_loaded_at: datetime | None = None
        self._last_refresh_attempt_at: datetime | None = None
        self._last_storage_success_at: datetime | None = None
        self._problem_since: datetime | None = None
        # 故障 episode 簿记：None 表示无活跃 episode；``close`` 清空。
        self._episode: _FaultEpisode | None = None
        # 待投递的 SUPERUSER 故障通知描述符。
        self._pending_notifications: list[_PendingNotification] = []

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
            with self._publish_lock:
                self._startup_phase = True
            try:
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
                    self._publish_snapshot(
                        snapshot, source=_POLICY_SOURCE_STARTUP
                    )
            finally:
                with self._publish_lock:
                    self._startup_phase = False

    async def close(self) -> None:
        """注销 listener 并清空快照/归属/陈旧窗口与 pending 队列；之后投递不得改变运行时。"""
        manager = self._manager
        if manager is not None:
            manager.unregister_snapshot_listener(self._on_snapshot)
        async with self._obs_process_lock:
            with self._publish_lock:
                self._closed = True
                self._manager = None
                self._aggregate = _CLOSED_AGGREGATE
                self._stale_window_start = None
                self._stale_suppressed_count = 0
                self._episode = None
                self._problem_since = None
            with self._pending_lock:
                self._pending_notifications.clear()
            with self._telemetry_lock:
                self._attribution_windows.clear()

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

        ``event_family`` 为事件族接缝（默认 ``"unknown"``，非法值归一到 ``"unknown"``），
        供归属失败按族独立聚合窗口。恰一次基础遥测计数；归属不可用时同步维护 300 秒
        归属窗口（首条记日志并排队一条通知描述符）。
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

    # --------------------------- 观测 / 通知接缝 ---------------------------

    def record_private_input_rejected(self, *, event_family: str) -> None:
        """记录一次私有输入拒绝（仅基础遥测，静默）。

        使 ``adjudications_total += 1``、``by_reason_code.private_input_rejected += 1``、
        ``by_intent.business += 1``；不进入归属失败 family 聚合（归属窗口只统计
        ``group_attribution_unavailable``）。``event_family`` 为必填关键字，签名与
        裁决入口一致。
        """
        result = AdmissionResult(
            qualification=AdmissionQualification.REJECTED,
            effective_revision=self._aggregate.state.effective_revision,
            reason_code="private_input_rejected",
        )
        self._record_adjudication(result, AdmissionIntent.BUSINESS, event_family)

    # --------------------------- pending 通知投递 ---------------------------

    def _safe_online_bots(self) -> list[object]:
        """安全枚举当前在线 Bot；provider 异常 / 不可迭代时返回空。

        返回 ``Mapping`` 时取 ``values``（NoneBot ``get_bots()`` 形态），其余
        按 Iterable 顺序转列表；一律保持提供者注入顺序，投递按此顺序 fallback。
        """
        try:
            raw = self._online_bots_provider()
        except Exception:
            return []
        if isinstance(raw, Mapping):
            return list(raw.values())
        if isinstance(raw, Iterable):
            return list(raw)
        return []

    def _resolve_superuser_ids(self) -> list[int]:
        """解析合法正整数 SUPERUSER QQ 号；provider 异常返回空。

        只接受正 ``int``（排除 ``bool``）与纯数字 ``str``，非法条目直接跳过；
        数值去重并按升序返回，供每收件人按 Bot 顺序 fallback 投递。
        """
        try:
            raw = self._superusers_provider()
        except Exception:
            return []
        if not isinstance(raw, Iterable):
            return []
        resolved: set[int] = set()
        for item in raw:
            parsed = _parse_superuser_id(item)
            if parsed is not None:
                resolved.add(parsed)
        return sorted(resolved)

    async def _deliver_pending_notifications(self) -> None:
        """尽力把 pending 通知描述投递给 SUPERUSER。

        - 无在线 Bot 或无合法收件人：整批保留（不冻结、不移除），由后续 tick 重试；
        - 锁序：``_pending_lock`` 只保护队列与描述符可变投递状态，绝不持线程锁跨
          ``await send_private_msg``；本轮在锁内完成「快照 + render + 初始化/拷贝收件
          人」后立即释放锁再 await；
        - 首次投递才把收件人快照冻结到 ``pending_recipients``（渲染成功后冻结、渲染
          异常不改变任何 pending 状态）；已成功发件人逐个从描述符移除，全部送达后连
          同描述符一起从全局队列移除（原地 remove、不重建列表，避免与并发 append 竞
          态）；部分失败保留等待下一轮，已成功收件人绝不重复投递；
        - 每收件人按 Bot 注入顺序 fallback：只尝试提供 ``send_private_msg`` 的 Bot，
          发送异常吞掉、首个成功即中断剩余循环，全部失败保留该收件人；本接缝不发日
          志、不触碰存储（无 outbox / PG / Redis）。
        """
        bots = self._safe_online_bots()
        recipients = self._resolve_superuser_ids()
        if not bots or not recipients:
            return
        # 锁内构造本轮投递清单：快照描述符、渲染文本、冻结/拷贝收件人。
        snapshot: list[tuple[_PendingNotification, list[int], str]] = []
        with self._pending_lock:
            for notification in list(self._pending_notifications):
                try:
                    text = self._render_notification(notification)
                except Exception:
                    continue  # renderer 异常：不冻结、不改任何 pending 状态
                if notification.pending_recipients is None:
                    notification.pending_recipients = list(recipients)
                if not notification.pending_recipients:
                    # 防御：收件人已空但未移除（非正常路径），锁内清理描述符。
                    self._pending_notifications.remove(notification)
                    continue
                snapshot.append(
                    (notification, list(notification.pending_recipients), text)
                )
        # 释放锁后再 await 投递：绝不持 threading 锁跨 ``await send_private_msg``。
        for notification, frozen_recipients, text in snapshot:
            for recipient in frozen_recipients:
                completed = False
                for bot in bots:
                    if not isinstance(bot, _SendableBot):
                        continue
                    if not callable(getattr(bot, "send_private_msg", None)):
                        continue  # 只尝试具备 send callable 的 Bot
                    try:
                        await bot.send_private_msg(
                            user_id=recipient, message=text
                        )
                    except Exception:
                        continue  # 本 Bot 失败：吞掉并 fallback 下一个 Bot
                    # 成功：锁内条件移除该收件人；收件人清空即锁内就地移除描述符。
                    with self._pending_lock:
                        current = notification.pending_recipients
                        if current is not None and recipient in current:
                            current.remove(recipient)
                            if not current:
                                self._pending_notifications.remove(notification)
                                completed = True
                    break  # 首个成功即中断本收件人的 Bot fallback 循环
                if completed:
                    break  # 描述符已全部送达并移除，跳到下一个描述符

    async def process_observability(self) -> None:
        """可观测性接缝：窗口摘要、故障提醒 / 恢复与 pending 投递。

        - 由 ``_obs_process_lock`` 串行化并发调度；``close`` 后直接返回；
        - 满 300 秒时对归属失败窗口与乱序 / 重复修订窗口各输出一条安全摘要，然后结算
          该窗口（重复调用幂等，下一窗口重新首条）；
        - 持续 failed/degraded 每 1800 秒边界出一条提醒（级别随当前状态、同 tick
          幂等）；READY 连续稳定 60 秒恰一次恢复日志并结束 episode；
        - 窗口 / 故障描述符产出之后，在**同一把** ``_obs_process_lock`` 内 ``await``
          ``_deliver_pending_notifications`` 尽力投递 pending 通知卡；
        - 只输出低基数安全摘要，不面向用户产出诊断正文，也不落任何存储。
        """
        async with self._obs_process_lock:
            if self._closed:
                return
            now = self._clock()

            # 归属失败窗口（每 event_family 独立）：满窗输出摘要并移除窗口。
            with self._telemetry_lock:
                for event_family in list(self._attribution_windows):
                    window = self._attribution_windows[event_family]
                    if (
                        now - window.window_start
                    ).total_seconds() >= _OBSERVABILITY_WINDOW_SECONDS:
                        self._attribution_windows.pop(event_family, None)
                        state = self._aggregate.state
                        logger.bind(
                            event=_EVENT_ATTRIBUTION_UNAVAILABLE,
                            status=state.status.value,
                            problem_code=state.problem_code,
                            configured_revision=state.configured_revision,
                            effective_revision=state.effective_revision,
                            using_last_known_good=state.using_last_known_good,
                            event_family=event_family,
                            window_seconds=_OBSERVABILITY_WINDOW_SECONDS,
                            suppressed_count=window.count - 1,
                        ).warning("准入归属失败窗口汇总")

            # 乱序 / 重复修订窗口：满窗输出安全摘要并结束窗口。
            with self._publish_lock:
                stale_start = self._stale_window_start
                if (
                    stale_start is not None
                    and (now - stale_start).total_seconds()
                    >= _STALE_REVISION_WINDOW_SECONDS
                ):
                    state = self._aggregate.state
                    logger.bind(
                        event=_EVENT_STALE_REVISION_IGNORED,
                        status=state.status.value,
                        problem_code=state.problem_code,
                        configured_revision=state.configured_revision,
                        effective_revision=state.effective_revision,
                        using_last_known_good=state.using_last_known_good,
                        suppressed_count=self._stale_suppressed_count,
                    ).warning("乱序/重复策略修订窗口汇总")
                    self._stale_window_start = None
                    self._stale_suppressed_count = 0

            # 故障 episode：持续期提醒 + 60 秒稳定恢复。
            episode = self._episode
            if episode is not None:
                state = self._aggregate.state
                if state.is_ready:
                    ready_since = episode.ready_since
                    if (
                        ready_since is not None
                        and (now - ready_since).total_seconds()
                        >= _RECOVERY_STABILITY_SECONDS
                    ):
                        self._emit_recovered_locked(episode, now)
                elif now >= episode.next_reminder_at:
                    self._emit_reminder_locked(episode, now)
                    # 推进到严格大于当前 now 的原 30 分钟网格边界：同 tick 重复
                    # process 幂等；跨越多个间隔时每次调用至多一条、不爆发循环。
                    episode.next_reminder_at = _next_reminder_boundary(
                        episode.started_at, now
                    )

            # 窗口 / 故障描述符产出之后再在同一把锁内尽力投递 pending
            # SUPERUSER 卡（无 Bot / 无合法收件人整次保留，下次 process 重试）。
            await self._deliver_pending_notifications()

    # --------------------------- 快照发布 ---------------------------

    def _on_snapshot(self, snapshot: ConfigSnapshot) -> None:
        """ConfigManager 快照 listener：管理严格 CAS 投递分流 source。

        只接纳严格更高修订（``_publish_snapshot``）；管理面预登记的修订归因
        source=management，其余一律 watcher。
        """
        with self._publish_lock:
            if self._startup_phase:
                source = _POLICY_SOURCE_STARTUP
            elif snapshot.revision in self._management_source_revisions:
                source = _POLICY_SOURCE_MANAGEMENT
            else:
                source = _POLICY_SOURCE_WATCHER
        self._publish_snapshot(snapshot, source=source)

    def _publish_snapshot(
        self, snapshot: ConfigSnapshot, *, source: str
    ) -> None:
        """接纳严格更高修订：合法编译后 READY 并记发布日志，否则按 LKG 收敛。

        乱序/重复（``watcher`` 投递）先经抑制窗口记账，不改变 state/problem；
        ``startup`` 显式消费的缓存快照等同修订不视为 stale。"""
        with self._publish_lock:
            if self._closed:
                return
            configured = self._aggregate.state.configured_revision
            old_revision = configured
            if configured is not None and snapshot.revision <= configured:
                if source == _POLICY_SOURCE_WATCHER:
                    self._record_stale_revision_locked(snapshot.revision)
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
            self._mark_recovery_ready_locked(now)
            self._log_policy_published_locked(
                old_revision=old_revision,
                new_revision=snapshot.revision,
                policy=policy,
                source=source,
            )
            # problem_since 由稳定恢复窗口延迟清空；合法发布只记录
            # episode 稳定期起点（``ready_since``），不在此立即结束 episode。

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
            self._record_problem_occurrence_locked(problem_code, now)

    def _log_policy_published_locked(
        self,
        *,
        old_revision: int | None,
        new_revision: int,
        policy: _CompiledPolicy,
        source: str,
    ) -> None:
        """policy_published INFO：精确 5 个 extra 键 + 规范化指纹。

        extra 固定为 event / old_revision / new_revision / policy_fingerprint / source；
        指纹是规范化策略的 SHA-256 摘要，绝不含策略正文或群号明文。
        """
        logger.bind(
            event=_EVENT_POLICY_PUBLISHED,
            old_revision=old_revision,
            new_revision=new_revision,
            policy_fingerprint=_canonical_policy_fingerprint(
                policy.mode, policy.group_ids
            ),
            source=source,
        ).info("群聊准入策略已发布")

    def _record_stale_revision_locked(self, revision: int) -> None:
        """乱序 / 重复修订：窗口首条 WARNING，300 秒内只累计，不改聚合。

        stale 快照只是被忽略的观测，运行时仍按当前聚合裁决；满窗由 ``process_observability`` 输出摘要并结算，
        越窗后的下一条按新窗口首条处理。
        """
        del revision  # stale 修订号不进入日志/状态（只保留低基数计数）。
        now = self._clock()
        window_start = self._stale_window_start
        if (
            window_start is not None
            and (now - window_start).total_seconds() < _STALE_REVISION_WINDOW_SECONDS
        ):
            self._stale_suppressed_count += 1
            return
        self._stale_window_start = now
        self._stale_suppressed_count = 0
        state = self._aggregate.state
        logger.bind(
            event=_EVENT_STALE_REVISION_IGNORED,
            status=state.status.value,
            problem_code=state.problem_code,
            configured_revision=state.configured_revision,
            effective_revision=state.effective_revision,
            using_last_known_good=state.using_last_known_good,
            suppressed_count=self._stale_suppressed_count,
        ).warning("收到非严格更高修订的群聊策略快照，已忽略")

    def _record_attribution_failure_window_locked(self, event_family: str) -> None:
        """归属失败按 event_family 独立 300 秒窗口：首条 WARNING + 排队，窗内只累计。

        窗口首条（含旧窗满后开新窗）创建一张 ``attribution`` 通知描述符
        （problem_code=None、occurrence_count=1、event_family 为归一后的封闭值）追加到
        ``_pending_notifications``；同窗内只递增 ``count`` 与描述符 occurrence_count，
        不重写、不重排；开新窗时旧描述符冻结不变，旧窗摘要由满窗结算输出。

        调用方必须已持有 ``_telemetry_lock``（与遥测 map 同一把锁，防止并发裁决丢计
        数）；描述符入队统一经 ``_pending_lock``（锁序 telemetry → pending）。关闭后在
        锁内复查并可靠中止，避免关闭后发生泄漏。
        """
        # 关闭后不另开窗口 / 不日志 / 不 append（telemetry 累计已在上层完成）。
        if self._closed:
            return
        now = self._clock()
        window = self._attribution_windows.get(event_family)
        if (
            window is not None
            and (now - window.window_start).total_seconds()
            < _OBSERVABILITY_WINDOW_SECONDS
        ):
            window.count += 1
            with self._pending_lock:
                window.notification.occurrence_count = window.count
            return
        state = self._aggregate.state
        notification = _PendingNotification(
            kind=_NOTICE_KIND_ATTRIBUTION,
            status=state.status.value,
            problem_code=None,
            started_at=now,
            occurrence_count=1,
            duration_seconds=None,
            event_family=event_family,
        )
        with self._pending_lock:
            # 锁内重查 _closed：防止与 close 的 pending 清空竞态漏append
            # （close 先在 publish 锁内置位再持 pending 锁清队列）。
            if self._closed:
                return
            self._pending_notifications.append(notification)
        self._attribution_windows[event_family] = _AttributionWindow(
            window_start=now,
            count=1,
            notification=notification,
        )
        logger.bind(
            event=_EVENT_ATTRIBUTION_UNAVAILABLE,
            status=state.status.value,
            problem_code=state.problem_code,
            configured_revision=state.configured_revision,
            effective_revision=state.effective_revision,
            using_last_known_good=state.using_last_known_good,
            event_family=event_family,
            window_seconds=_OBSERVABILITY_WINDOW_SECONDS,
            suppressed_count=0,
        ).warning("准入关联群归属不可用")

    def _converge_storage_failure(self) -> None:
        """冷启动存储异常收敛为故障关闭；已接纳快照不回退。"""
        with self._publish_lock:
            if self._closed or self._aggregate.state.configured_revision is not None:
                return
            self._aggregate = _INITIAL_AGGREGATE
            now = self._clock()
            self._record_problem_occurrence_locked("storage_unavailable", now)

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
            self._record_problem_occurrence_locked("storage_unavailable", now)

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
                self._publish_snapshot(snapshot, source=_POLICY_SOURCE_WATCHER)

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
            self._mark_recovery_ready_locked(now)
            # problem_since 由稳定恢复窗口延迟清空；同修订恢复只记录
            # 稳定期起点，不立即结束 episode。

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
        # 单 worker strict CAS 成功后新修订恒为 ``expected_revision + 1``（真实
        # 存储 ``revision = revision + 1`` 与 fake 同语义）。预登记该修订，使
        # listener 幂等投递时把 ``policy_published`` source 归因 management；
        # 失败/冲突路径在 finally 中摘除，不影响后续 watcher 投递归因。
        expected_new_revision = expected_revision + 1
        with self._publish_lock:
            self._management_source_revisions.add(expected_new_revision)
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
        finally:
            with self._publish_lock:
                self._management_source_revisions.discard(
                    expected_new_revision
                )

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
                self._record_problem_occurrence_locked(
                    "snapshot_publish_failed", now
                )
            return _ControlPlaneApplyResult(
                new_revision=new_snapshot.revision, published_locally=False
            )
        return _ControlPlaneApplyResult(
            new_revision=new_snapshot.revision, published_locally=True
        )

    # --------------------------- 内存状态投影 ---------------------------

    def _project_status(self) -> dict[str, object]:
        """内存私有状态投影（11 顶层键 + telemetry 深拷贝）。

        datetime 保持 aware UTC 以便 API 序列化；telemetry map 深拷贝，调用方不能通过
        返回值影响运行时。由管理 API ``/status`` 消费。
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
                self._record_attribution_failure_window_locked(
                    normalized_family
                )

    def _record_problem_occurrence_locked(
        self, problem_code: AdmissionProblemCode, now: datetime
    ) -> None:
        """记录一次运行时故障（episode 生命周期）。

        - 无活跃 episode（含上次已恢复）时开新 episode：置位 ``problem_since``、
          ``occurrence_count=1``、``next_reminder_at=开始+1800``，立即写
          ``runtime_failed`` / ``runtime_degraded`` 起始日志并排队一张 fault_start 卡；
        - episode 延续：``occurrence_count`` 累加、清除 60 秒稳定期 ``ready_since``，
          不重开 episode、不重写起始日志；
        - ``runtime_problem_occurrences`` 按问题码去重：同码只计一次、切新码才对
          新码 +1；
        - stale 快照 / CAS 冲突 / 正常裁决拒绝不进入本路径。

        调用方必须已更新聚合有效状态并持有 ``_publish_lock``。

        """
        episode = self._episode
        if episode is None:
            episode = _FaultEpisode(
                started_at=now,
                occurrence_count=1,
                next_reminder_at=now
                + timedelta(seconds=_REMINDER_INTERVAL_SECONDS),
                ready_since=None,
                problem_code=problem_code,
                counted_codes=set(),
            )
            self._episode = episode
            self._problem_since = now
        else:
            episode.occurrence_count += 1
            episode.ready_since = None

        if problem_code not in episode.counted_codes:
            episode.counted_codes.add(problem_code)
            with self._telemetry_lock:
                self._telemetry_problem[problem_code] += 1

        if episode.occurrence_count > 1:
            return  # episode 延续：不重写起始日志 / 不重复排队起始卡

        state = self._aggregate.state
        with self._pending_lock:
            self._pending_notifications.append(
                _PendingNotification(
                    kind=_NOTICE_KIND_START,
                    status=state.status.value,
                    problem_code=problem_code,
                    started_at=episode.started_at,
                    occurrence_count=1,
                    duration_seconds=0,
                )
            )
        if state.status is AdmissionRuntimeStatus.FAILED:
            logger.bind(
                event=_EVENT_RUNTIME_FAILED,
                status=state.status.value,
                problem_code=problem_code,
                configured_revision=state.configured_revision,
                effective_revision=state.effective_revision,
                using_last_known_good=state.using_last_known_good,
                occurrence_count=1,
            ).error("准入运行时故障")
        else:
            logger.bind(
                event=_EVENT_RUNTIME_DEGRADED,
                status=state.status.value,
                problem_code=problem_code,
                configured_revision=state.configured_revision,
                effective_revision=state.effective_revision,
                using_last_known_good=state.using_last_known_good,
                occurrence_count=1,
            ).warning("准入运行时降级")

    def _mark_recovery_ready_locked(self, now: datetime) -> None:
        """episode 活跃期间的连续 READY 记录稳定期起点（不结束 episode）。

        合法快照 / 同修订刷新使业务状态立即 READY，但故障 episode 需 READY 连
        续稳定满 60 秒才由 ``process_observability`` 显式结束；期间再次故障会
        清除 ``ready_since`` 重新计算稳定窗口。
        """
        episode = self._episode
        if episode is not None and episode.ready_since is None:
            episode.ready_since = now

    def _emit_reminder_locked(
        self, episode: _FaultEpisode, now: datetime
    ) -> None:
        """episode 持续到每个 1800 秒边界记一条提醒（级别随**当前**状态）。

        只追加一条提醒描述符（``duration_seconds`` / ``occurrence_count`` 精确
        携带）；``next_reminder_at`` 由调用方推进到严格大于当前 ``now`` 的原
        30 分钟网格边界，保证同一时点重复 process 幂等、跨越多个间隔不爆发
        循环。``_pending_notifications`` 的 append 经 ``_pending_lock``。
        调用方必须已持 ``_obs_process_lock``。
        """
        state = self._aggregate.state
        duration_seconds = int((now - episode.started_at).total_seconds())
        with self._pending_lock:
            self._pending_notifications.append(
                _PendingNotification(
                    kind=_NOTICE_KIND_REMINDER,
                    status=state.status.value,
                    problem_code=state.problem_code,
                    started_at=episode.started_at,
                    occurrence_count=episode.occurrence_count,
                    duration_seconds=duration_seconds,
                )
            )
        if state.status is AdmissionRuntimeStatus.FAILED:
            logger.bind(
                event=_EVENT_RUNTIME_REMINDER,
                status=state.status.value,
                problem_code=state.problem_code,
                configured_revision=state.configured_revision,
                effective_revision=state.effective_revision,
                using_last_known_good=state.using_last_known_good,
                duration_seconds=duration_seconds,
                occurrence_count=episode.occurrence_count,
            ).error("准入运行时故障持续")
        else:
            logger.bind(
                event=_EVENT_RUNTIME_REMINDER,
                status=state.status.value,
                problem_code=state.problem_code,
                configured_revision=state.configured_revision,
                effective_revision=state.effective_revision,
                using_last_known_good=state.using_last_known_good,
                duration_seconds=duration_seconds,
                occurrence_count=episode.occurrence_count,
            ).warning("准入运行时降级持续")

    def _emit_recovered_locked(
        self, episode: _FaultEpisode, now: datetime
    ) -> None:
        """READY 连续稳定 60 秒：恰一次 ``runtime_recovered`` INFO + 排队恢复卡。

        offline fault→recovery 合并：若当前 ``_safe_online_bots()`` 为空（完全
        离线）且 pending 中存在同 episode ``fault_start``（``started_at`` 与
        episode 一致、``pending_recipients is None`` 从未尝试投递），则原地把
        该描述符转为 ``combined`` / status=ready（保留原 problem_code，更新
        occurrence/duration/resolved_at），不再 append 独立 recovered 卡——离
        线期间故障开始又恢复只补一张合并卡；否则（在线，或 start 已部分/全部
        投递）保持现有 append 独立恢复卡。不得合并已部分/全部投递的 start，也
        不影响正常在线 start+recovery 两卡。

        episode 随之结束：清空 ``_episode`` / ``problem_since``，使后续新故障
        开启新 episode（新起始日志 / problem occurrence +1）。
        """
        state = self._aggregate.state
        duration_seconds = int((now - episode.started_at).total_seconds())

        merged_start: _PendingNotification | None = None
        if not self._safe_online_bots():
            # 完全离线：只允许从未尝试投递的同 episode fault_start 原地合并。
            for notification in self._pending_notifications:
                if (
                    notification.kind == _NOTICE_KIND_START
                    and notification.started_at == episode.started_at
                    and notification.pending_recipients is None
                ):
                    merged_start = notification
                    break
        if merged_start is not None:
            merged_start.kind = _NOTICE_KIND_COMBINED
            merged_start.status = AdmissionRuntimeStatus.READY.value
            # problem_code 保留原 fault_start 的故障身份（= episode.problem_code）。
            merged_start.occurrence_count = episode.occurrence_count
            merged_start.duration_seconds = duration_seconds
            merged_start.resolved_at = now
        else:
            self._pending_notifications.append(
                _PendingNotification(
                    kind=_NOTICE_KIND_RECOVERED,
                    status=AdmissionRuntimeStatus.READY.value,
                    problem_code=episode.problem_code,
                    started_at=episode.started_at,
                    occurrence_count=episode.occurrence_count,
                    duration_seconds=duration_seconds,
                    resolved_at=now,
                )
            )
        logger.bind(
            event=_EVENT_RUNTIME_RECOVERED,
            status=AdmissionRuntimeStatus.READY.value,
            problem_code=None,
            configured_revision=state.configured_revision,
            effective_revision=state.effective_revision,
            using_last_known_good=state.using_last_known_good,
            duration_seconds=duration_seconds,
            occurrence_count=episode.occurrence_count,
        ).info("准入运行时已恢复")
        self._episode = None
        self._problem_since = None

    def _render_notification(self, notification: _PendingNotification) -> str:
        """把 pending 描述符渲染为发给 SUPERUSER 私聊卡的安全文本（固定安全行 + 固定占位）。

        ``attribution`` 保留 event / status / occurrence_count / window_started_at 等安
        全行；``fault_start`` 按当前状态选 failed/degraded 事件名，``reminder`` 恒为提醒
        事件名，``recovered`` / ``combined`` 恒为恢复名；``resolved_at`` 非 None 时追加；
        ``combined`` 首行为固定合并文案；未知 kind 返回固定安全占位文本 ``群聊准入运
        行时状态通知``。
        """
        if notification.kind == _NOTICE_KIND_ATTRIBUTION:
            return "\n".join(
                (
                    "reason_code: group_attribution_unavailable",
                    f"event_family: {notification.event_family}",
                    f"occurrence_count: {notification.occurrence_count}",
                    "window_started_at: "
                    f"{_format_rfc3339(notification.started_at)}",
                )
            )
        if notification.kind == _NOTICE_KIND_START:
            if notification.status == AdmissionRuntimeStatus.FAILED.value:
                event = _EVENT_RUNTIME_FAILED
            else:
                event = _EVENT_RUNTIME_DEGRADED
            lines = [f"event: {event}"]
        elif notification.kind == _NOTICE_KIND_REMINDER:
            lines = [f"event: {_EVENT_RUNTIME_REMINDER}"]
        elif notification.kind in (
            _NOTICE_KIND_RECOVERED,
            _NOTICE_KIND_COMBINED,
        ):
            lines = [f"event: {_EVENT_RUNTIME_RECOVERED}"]
            if notification.kind == _NOTICE_KIND_COMBINED:
                lines.insert(0, "群聊准入运行时故障期间发生且现已恢复")
        else:
            return "群聊准入运行时状态通知"

        lines.extend(
            (
                f"status: {notification.status}",
                f"problem_code: {notification.problem_code}",
                f"occurrence_count: {notification.occurrence_count}",
                f"duration_seconds: {notification.duration_seconds}",
                "started_at: "
                f"{_format_rfc3339(notification.started_at)}",
            )
        )
        if notification.resolved_at is not None:
            lines.append(
                "resolved_at: " f"{_format_rfc3339(notification.resolved_at)}"
            )
        return "\n".join(lines)


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
