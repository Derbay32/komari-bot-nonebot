"""TSK-182 判定日志仓储：元数据/正文分离的持久化与受权查询边界。

设计要点（TSK-180 §9、182-log-store-api-contract）：

- 判定诊断日志与回复 Agent Run 日志、记忆表互相独立；表结构由迁移 0023 建好，
  运行时没有任何 DDL、没有自建连接池。
- 元数据读取只选取显式列，永不 ``SELECT`` ``body``；正文只能经独立的受权方法
  读取，且与元数据共用同一个保留期判定。
- 保留期由调用方提供的 provider **每次操作读取一次**，合法范围 1..30 天；比较
  一律使用数据库 ``CURRENT_TIMESTAMP``，绝不使用主机墙钟做跨进程推断。超过当前
  保留期的行在物理清理之前就已经不出现在列表、计数、详情与正文结果里。
- ``details`` JSONB 只承载 ``DecisionResult`` 的字段；读取时重建类型化结果，遇到
  未知字段、类型不符或 ``decision_id`` 不一致一律拒绝，绝不把任意 JSON 当作元数据
  返回。
- 写入使用 ``ON CONFLICT(id) DO NOTHING``：同一次判定被重复记录时不会覆盖既有
  观测或正文，也不做任何自动重试。
- 清理只在有界批次内删除过期日志行，绝不删除或改写事件去重身份、调度行与进程行。
- 存储故障统一收敛为固定文案异常，不记录原始异常、SQL 参数、正文或凭据。
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, fields, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, cast, get_args
from uuid import UUID

from sqlalchemy import ColumnElement, CursorResult, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlmodel import col

from komari_bot.decision.jev import (
    BusinessRoute,
    DecisionLogMetadata,
    DecisionReason,
    DecisionResult,
    DecisionStatus,
    DecisionUsage,
    EventIdentity,
    PhaseDurations,
    ReferenceSummary,
    ReplyChoice,
    ReplyEligibility,
    RetentionChoice,
    RetentionPurpose,
)
from komari_bot.llm.diagnostic_sanitizer import sanitize_log_value
from komari_bot.plugins.komari_decision.orm_models import (
    DecisionDispatchRow,
    DecisionLogRow,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

STORAGE_UNAVAILABLE_MESSAGE: Final = "判定日志存储暂不可用"
"""存储故障的唯一对外文案（绝不携带原始异常、SQL 或参数）。"""

SUPPORTED_RETENTION_DAYS_MIN: Final = 1
SUPPORTED_RETENTION_DAYS_MAX: Final = 30
DEFAULT_PAGE_SIZE: Final = 50
MAX_PAGE_SIZE: Final = 100
DEFAULT_PRUNE_BATCH: Final = 500
MAX_PRUNE_BATCH: Final = 5000
_BODY_FREE_STATUSES: Final[frozenset[str]] = frozenset({"filtered", "duplicate"})
"""过滤与重投事件自身只记录安全元数据，正文必须为空。"""

_BINARY_TEXT_PLACEHOLDER: Final = "[已移除二进制内容]"
"""文本列不允许出现二进制摘要，命中时使用固定占位符。"""


def _literal_values(alias: Any) -> frozenset[str]:
    """读取 PEP 695 字面量类型别名的合法值集合（避免手抄枚举漂移）。"""
    return frozenset(str(value) for value in get_args(alias.__value__))


_STATUS_VALUES: Final = _literal_values(DecisionStatus)
_REASON_VALUES: Final = _literal_values(DecisionReason)
_ROUTE_VALUES: Final = _literal_values(BusinessRoute)
_REPLY_VALUES: Final = _literal_values(ReplyEligibility)
_RETENTION_VALUES: Final = _literal_values(RetentionPurpose)

_RESULT_FIELDS: Final[frozenset[str]] = frozenset(
    field.name for field in fields(DecisionResult)
)
_REFERENCE_FIELDS: Final[frozenset[str]] = frozenset(
    field.name for field in fields(ReferenceSummary)
)
_DURATION_FIELDS: Final[frozenset[str]] = frozenset(
    field.name for field in fields(PhaseDurations)
)
_USAGE_FIELDS: Final[frozenset[str]] = frozenset(
    field.name for field in fields(DecisionUsage)
)
_CHOICE_FIELDS: Final[frozenset[str]] = frozenset({"value", "confidence", "probabilities"})

#: details 与专用列共同承载的字段；两处必须一致，否则视为存储损坏。
_DETAIL_COLUMN_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("status", "status"),
    ("reason_code", "reason_code"),
    ("config_revision", "config_revision"),
    ("prompt_revision", "prompt_revision"),
    ("requested_model", "requested_model"),
    ("actual_model", "actual_model"),
)

#: 元数据查询的显式列投影：故意不包含 ``body``。
_METADATA_COLUMNS: Final[tuple[Any, ...]] = (
    col(DecisionLogRow.id),
    col(DecisionLogRow.protocol),
    col(DecisionLogRow.account_id),
    col(DecisionLogRow.group_id),
    col(DecisionLogRow.message_id),
    col(DecisionLogRow.created_at),
    col(DecisionLogRow.finished_at),
    col(DecisionLogRow.status),
    col(DecisionLogRow.reason_code),
    col(DecisionLogRow.config_revision),
    col(DecisionLogRow.prompt_revision),
    col(DecisionLogRow.requested_model),
    col(DecisionLogRow.actual_model),
    col(DecisionLogRow.details),
)


class DecisionLogStorageUnavailable(Exception):  # noqa: N818 -- 契约固定名
    """存储不可用；对外只暴露固定文案。"""

    def __init__(self) -> None:
        super().__init__(STORAGE_UNAVAILABLE_MESSAGE)


class DecisionLogRequestError(ValueError):
    """调用方提供的查询条件或待写元数据不合法（绝不静默降级）。"""


class DecisionLogRetentionError(ValueError):
    """保留期 provider 不可用或超出 1..30（绝不回退默认值）。"""


class DecisionLogCorruptRowError(ValueError):
    """已落库的元数据无法重建为类型化结果（绝不返回任意 JSON）。"""


@dataclass(frozen=True, slots=True)
class DecisionLogPage:
    """一页判定日志元数据；不含正文或预览。"""

    items: tuple[DecisionLogMetadata, ...]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True, slots=True)
class DecisionLogBody:
    """一次正文读取结果；``body`` 为 ``None`` 表示该行没有捕获正文。

    行本身不存在（或已超期）时返回 ``None`` 而不是本类型，从而与
    「存在但未捕获正文」明确区分。
    """

    decision_id: str
    body: dict[str, Any] | None


class DecisionLogRepository:
    """判定诊断日志的持久化与受权查询仓储。"""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        retention_days: Callable[[], int],
    ) -> None:
        self._session_factory = session_factory
        self._retention_days_provider = retention_days

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        """统一创建、使用与关闭会话的故障边界；取消仍原样传播。"""
        try:
            session = self._session_factory()
            try:
                yield session
            except BaseException:
                # 关闭会话会回滚未提交事务；清理故障不能覆盖原始取消。
                with suppress(Exception):
                    await session.close()
                raise
            else:
                await session.close()
        except Exception:
            raise DecisionLogStorageUnavailable from None

    # ------------------------------ 写入 ------------------------------

    async def record(
        self,
        metadata: DecisionLogMetadata,
        *,
        body: dict[str, Any] | None,
        known_secrets: tuple[str, ...] = (),
    ) -> None:
        """写入一条判定日志；重复的 decision_id 不覆盖既有记录。"""
        values = self._record_values(metadata, body=body, known_secrets=known_secrets)
        async with self._session() as session:
            statement = (
                postgres_insert(DecisionLogRow)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[col(DecisionLogRow.id)])
            )
            await session.execute(statement)
            await session.commit()

    def _record_values(
        self,
        metadata: DecisionLogMetadata,
        *,
        body: dict[str, Any] | None,
        known_secrets: tuple[str, ...],
    ) -> dict[str, Any]:
        decision_id = _required_decision_id(metadata.decision_id)
        if _optional_decision_id(metadata.result.decision_id) != decision_id:
            msg = "元数据与结果的 decision_id 不一致"
            raise DecisionLogRequestError(msg)
        status = _required_literal(
            metadata.result.status, _STATUS_VALUES, "结果状态非法"
        )
        reason_code = _required_literal(
            metadata.result.reason_code, _REASON_VALUES, "结果原因码非法"
        )
        created_at = _required_aware(metadata.created_at, "created_at")
        finished_at = _required_aware(metadata.finished_at, "finished_at")
        event = metadata.event
        observed = replace(
            metadata.result,
            # details 与专用列必须使用同一个规范 decision_id。
            decision_id=decision_id,
            requested_model=_sanitized_text(
                metadata.result.requested_model, "requested_model", known_secrets
            ),
            actual_model=_sanitized_text(
                metadata.result.actual_model, "actual_model", known_secrets
            ),
        )
        return {
            "id": decision_id,
            "dispatch_row_id": _dispatch_row_id_for(decision_id),
            "protocol": _required_sanitized_text(event.protocol, "protocol", known_secrets),
            "account_id": _required_sanitized_text(
                event.account_id, "account_id", known_secrets
            ),
            "group_id": _required_sanitized_text(
                event.group_id, "group_id", known_secrets
            ),
            "message_id": _required_sanitized_text(
                event.message_id, "message_id", known_secrets
            ),
            "created_at": created_at,
            "finished_at": finished_at,
            "status": status,
            "reason_code": reason_code,
            "config_revision": observed.config_revision,
            "prompt_revision": observed.prompt_revision,
            "requested_model": observed.requested_model,
            "actual_model": observed.actual_model,
            "details": _serialize_result(observed),
            "body": _sanitized_body(body, status=status, known_secrets=known_secrets),
        }

    # ------------------------------ 元数据查询 ------------------------------

    async def list_metadata(
        self,
        *,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
        created_from: datetime | None = None,
        created_before: datetime | None = None,
        status: DecisionStatus | None = None,
        model: str | None = None,
    ) -> DecisionLogPage:
        """按保留期与过滤条件返回一页元数据（不含正文）。"""
        current_page = _required_page(page)
        current_size = _required_page_size(page_size)
        lower = _optional_aware(created_from, "created_from")
        upper = _optional_aware(created_before, "created_before")
        if lower is not None and upper is not None and lower >= upper:
            msg = "created_from 必须早于 created_before"
            raise DecisionLogRequestError(msg)
        if status is not None and status not in _STATUS_VALUES:
            msg = "状态过滤值非法"
            raise DecisionLogRequestError(msg)
        if model is not None and not isinstance(model, str):
            msg = "模型过滤值必须是字符串"
            raise DecisionLogRequestError(msg)

        conditions = self._visible_conditions(
            created_from=lower,
            created_before=upper,
            status=status,
            model=model,
        )
        page_statement = (
            select(*_METADATA_COLUMNS)
            .where(*conditions)
            .order_by(
                col(DecisionLogRow.created_at).desc(),
                col(DecisionLogRow.id).asc(),
            )
            .limit(current_size)
            .offset((current_page - 1) * current_size)
        )
        count_statement = (
            select(func.count()).select_from(DecisionLogRow).where(*conditions)
        )

        rows: list[Mapping[str, Any]] = []
        total = 0
        async with self._session() as session:
            total = int(await session.scalar(count_statement) or 0)
            result = await session.execute(page_statement)
            rows = [dict(row) for row in result.mappings().all()]

        return DecisionLogPage(
            items=tuple(_metadata_from_row(row) for row in rows),
            total=total,
            page=current_page,
            page_size=current_size,
        )

    async def get_metadata(self, decision_id: str) -> DecisionLogMetadata | None:
        """读取一条未超期的元数据；不存在或已超期时返回 ``None``。"""
        normalized = _optional_decision_id(decision_id)
        if normalized is None:
            return None
        statement = select(*_METADATA_COLUMNS).where(
            col(DecisionLogRow.id) == normalized,
            col(DecisionLogRow.created_at) >= self._retention_cutoff(),
        )
        async with self._session() as session:
            row = (await session.execute(statement)).mappings().first()
        return None if row is None else _metadata_from_row(dict(row))

    # ------------------------------ 正文查询 ------------------------------

    async def get_body(self, decision_id: str) -> DecisionLogBody | None:
        """读取一条未超期日志的正文；行不存在或已超期时返回 ``None``。"""
        normalized = _optional_decision_id(decision_id)
        if normalized is None:
            return None
        statement = select(
            col(DecisionLogRow.id), col(DecisionLogRow.body)
        ).where(
            col(DecisionLogRow.id) == normalized,
            col(DecisionLogRow.created_at) >= self._retention_cutoff(),
        )
        async with self._session() as session:
            row = (await session.execute(statement)).mappings().first()
        if row is None:
            return None
        body = row["body"]
        if body is not None and not isinstance(body, dict):
            msg = "日志正文不是 JSON 对象"
            raise DecisionLogCorruptRowError(msg)
        return DecisionLogBody(decision_id=str(row["id"]), body=body)

    # ------------------------------ 过期清理 ------------------------------

    async def prune(self, *, limit: int = DEFAULT_PRUNE_BATCH) -> int:
        """在有界批次内删除过期日志行；返回本次删除行数。"""
        batch = _required_prune_limit(limit)
        cutoff = self._retention_cutoff()
        expired = (
            select(col(DecisionLogRow.id))
            .where(col(DecisionLogRow.created_at) < cutoff)
            .order_by(
                col(DecisionLogRow.created_at).asc(),
                col(DecisionLogRow.id).asc(),
            )
            .limit(batch)
        )
        statement = delete(DecisionLogRow).where(col(DecisionLogRow.id).in_(expired))
        async with self._session() as session:
            result = await session.execute(statement)
            await session.commit()
            return int(cast("CursorResult[Any]", result).rowcount)

    # ------------------------------ 保留期与条件 ------------------------------

    def _visible_conditions(
        self,
        *,
        created_from: datetime | None,
        created_before: datetime | None,
        status: DecisionStatus | str | None,
        model: str | None,
    ) -> list[ColumnElement[bool]]:
        conditions: list[ColumnElement[bool]] = [
            col(DecisionLogRow.created_at) >= self._retention_cutoff()
        ]
        if created_from is not None:
            conditions.append(col(DecisionLogRow.created_at) >= created_from)
        if created_before is not None:
            conditions.append(col(DecisionLogRow.created_at) < created_before)
        if status is not None:
            conditions.append(col(DecisionLogRow.status) == status)
        if model is not None:
            conditions.append(
                or_(
                    col(DecisionLogRow.requested_model) == model,
                    col(DecisionLogRow.actual_model) == model,
                )
            )
        return conditions

    def _retention_cutoff(self) -> ColumnElement[datetime]:
        """保留期下界；以数据库 ``CURRENT_TIMESTAMP`` 计算，不看主机时钟。"""
        days = self._retention_window_days()
        interval = func.make_interval(0, 0, 0, days)
        return cast(
            "ColumnElement[datetime]",
            func.current_timestamp() - interval,
        )

    def _retention_window_days(self) -> int:
        """每次操作读取一次保留期；不可用或越界都不回退默认值。"""
        try:
            raw = self._retention_days_provider()
        except Exception:
            msg = "保留天数不可用"
            raise DecisionLogRetentionError(msg) from None
        if isinstance(raw, bool) or not isinstance(raw, int):
            msg = "保留天数必须是整数"
            raise DecisionLogRetentionError(msg)
        if not SUPPORTED_RETENTION_DAYS_MIN <= raw <= SUPPORTED_RETENTION_DAYS_MAX:
            msg = "保留天数必须在 1 至 30 天之间"
            raise DecisionLogRetentionError(msg)
        return raw


# ------------------------------ 写入辅助 ------------------------------


def _dispatch_row_id_for(decision_id: str) -> Any:
    """按 decision_id 反查事件占位行 id；未登记（如重复尝试）时为 ``NULL``。"""
    return (
        select(col(DecisionDispatchRow.id))
        .where(col(DecisionDispatchRow.decision_id) == decision_id)
        .limit(1)
        .scalar_subquery()
    )


def _required_decision_id(value: object) -> str:
    normalized = _optional_decision_id(value)
    if normalized is None:
        msg = "decision_id 必须是 UUID"
        raise DecisionLogRequestError(msg)
    return normalized


def _optional_decision_id(value: object) -> str | None:
    """规范化 decision_id；非法值返回 ``None``（查询侧据此视为不存在）。"""
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _required_aware(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        msg = f"{field} 必须是带时区的时间"
        raise DecisionLogRequestError(msg)
    return value


def _optional_aware(value: object, field: str) -> datetime | None:
    if value is None:
        return None
    return _required_aware(value, field)


def _required_page(page: object) -> int:
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        msg = "page 必须是大于等于 1 的整数"
        raise DecisionLogRequestError(msg)
    return page


def _required_page_size(page_size: object) -> int:
    if (
        isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or not 1 <= page_size <= MAX_PAGE_SIZE
    ):
        msg = f"page_size 必须在 1 至 {MAX_PAGE_SIZE} 之间"
        raise DecisionLogRequestError(msg)
    return page_size


def _required_prune_limit(limit: object) -> int:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_PRUNE_BATCH
    ):
        msg = f"清理批次必须在 1 至 {MAX_PRUNE_BATCH} 之间"
        raise DecisionLogRequestError(msg)
    return limit


def _required_literal(value: object, allowed: frozenset[str], detail: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise DecisionLogRequestError(detail)
    return value


def _sanitized_text(
    value: str | None, field: str, known_secrets: tuple[str, ...]
) -> str | None:
    if value is None:
        return None
    sanitized = sanitize_log_value(value, key=field, known_secrets=known_secrets)
    return sanitized if isinstance(sanitized, str) else _BINARY_TEXT_PLACEHOLDER


def _required_sanitized_text(
    value: object, field: str, known_secrets: tuple[str, ...]
) -> str:
    if not isinstance(value, str) or not value.strip():
        msg = f"{field} 必须是非空字符串"
        raise DecisionLogRequestError(msg)
    return _sanitized_text(value, field, known_secrets) or _BINARY_TEXT_PLACEHOLDER


def _sanitized_body(
    body: dict[str, Any] | None,
    *,
    status: str,
    known_secrets: tuple[str, ...],
) -> dict[str, Any] | None:
    """正文只来自调用方显式提供的内容，过滤/重投事件强制为空。"""
    if body is None or status in _BODY_FREE_STATUSES:
        return None
    sanitized = sanitize_log_value(body, known_secrets=known_secrets)
    if not isinstance(sanitized, dict):
        msg = "正文必须是 JSON 对象"
        raise DecisionLogRequestError(msg)
    return sanitized


# ------------------------------ details 编解码 ------------------------------


def _serialize_result(result: DecisionResult) -> dict[str, Any]:
    """把类型化结果投影为 ``details`` JSONB（只含 DecisionResult 字段）。"""
    return {
        "decision_id": result.decision_id,
        "status": result.status,
        "reason_code": result.reason_code,
        "route": result.route,
        "force_reply": bool(result.force_reply),
        "reply_choice": _serialize_choice(result.reply_choice),
        "retention_choice": _serialize_choice(result.retention_choice),
        "summary_probability": result.summary_probability,
        "effective_reply": result.effective_reply,
        "retain_recent": result.retain_recent,
        "memory_candidate": result.memory_candidate,
        "optional_selected": result.optional_selected,
        "references": {
            "complete": bool(result.references.complete),
            "resolved_count": int(result.references.resolved_count),
            "missing_count": int(result.references.missing_count),
            "cycle_count": int(result.references.cycle_count),
            "timed_out": bool(result.references.timed_out),
        },
        "durations": {
            "queue_ms": result.durations.queue_ms,
            "references_ms": result.durations.references_ms,
            "request_ms": result.durations.request_ms,
        },
        "usage": {
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
        },
        "config_revision": result.config_revision,
        "prompt_revision": result.prompt_revision,
        "requested_model": result.requested_model,
        "actual_model": result.actual_model,
    }


def _serialize_choice(choice: ReplyChoice | RetentionChoice | None) -> Any:
    if choice is None:
        return None
    return {
        "value": choice.value,
        "confidence": choice.confidence,
        "probabilities": [
            [label, probability] for label, probability in choice.probabilities
        ],
    }


def _metadata_from_row(row: Mapping[str, Any]) -> DecisionLogMetadata:
    """把一行显式列投影重建为类型化元数据（结构不符即拒绝）。"""
    decision_id = str(row["id"])
    result = _deserialize_result(row["details"], decision_id=decision_id)
    for column_key, field_name in _DETAIL_COLUMN_FIELDS:
        if row[column_key] != getattr(result, field_name):
            msg = "details 与元数据列不一致"
            raise DecisionLogCorruptRowError(msg)
    try:
        event = EventIdentity(
            protocol=str(row["protocol"]),
            account_id=str(row["account_id"]),
            group_id=str(row["group_id"]),
            message_id=str(row["message_id"]),
        )
    except ValueError:
        msg = "日志事件身份无效"
        raise DecisionLogCorruptRowError(msg) from None
    return DecisionLogMetadata(
        decision_id=decision_id,
        event=event,
        created_at=_row_datetime(row["created_at"], "日志开始时间无效"),
        finished_at=_row_datetime(row["finished_at"], "日志结束时间无效"),
        result=result,
    )


def _row_datetime(value: object, detail: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise DecisionLogCorruptRowError(detail)
    return value


def _deserialize_result(payload: object, *, decision_id: str) -> DecisionResult:
    """严格重建 ``DecisionResult``；任何未知/非法结构都拒绝。"""
    if not isinstance(payload, Mapping):
        msg = "details 不是 JSON 对象"
        raise DecisionLogCorruptRowError(msg)
    if set(payload) - _RESULT_FIELDS:
        msg = "details 含未知字段"
        raise DecisionLogCorruptRowError(msg)
    if payload.get("decision_id") != decision_id:
        msg = "details 的 decision_id 与行 id 不一致"
        raise DecisionLogCorruptRowError(msg)

    status = _row_literal(payload.get("status"), _STATUS_VALUES, "状态非法")
    reason_code = _row_literal(payload.get("reason_code"), _REASON_VALUES, "原因码非法")
    route = _row_literal(payload.get("route", "none"), _ROUTE_VALUES, "业务路由非法")
    effective_reply = _row_optional_literal(
        payload.get("effective_reply"), _REPLY_VALUES, "有效回应资格非法"
    )
    reply_choice = _row_choice(payload.get("reply_choice"), _REPLY_VALUES)
    retention_choice = _row_choice(payload.get("retention_choice"), _RETENTION_VALUES)
    references = _row_mapping(payload.get("references"), _REFERENCE_FIELDS, "引用摘要非法")
    durations = _row_mapping(payload.get("durations"), _DURATION_FIELDS, "阶段耗时非法")
    usage = _row_mapping(payload.get("usage"), _USAGE_FIELDS, "用量非法")

    return DecisionResult(
        decision_id=decision_id,
        status=cast("DecisionStatus", status),
        reason_code=cast("DecisionReason", reason_code),
        route=cast("BusinessRoute", route),
        force_reply=_row_bool(payload.get("force_reply", False), "强制回复标记非法"),
        reply_choice=(
            None
            if reply_choice is None
            else ReplyChoice(
                value=cast("ReplyEligibility", reply_choice[0]),
                confidence=reply_choice[1],
                probabilities=cast(
                    "tuple[tuple[ReplyEligibility, float], ...]", reply_choice[2]
                ),
            )
        ),
        retention_choice=(
            None
            if retention_choice is None
            else RetentionChoice(
                value=cast("RetentionPurpose", retention_choice[0]),
                confidence=retention_choice[1],
                probabilities=cast(
                    "tuple[tuple[RetentionPurpose, float], ...]", retention_choice[2]
                ),
            )
        ),
        summary_probability=_row_optional_float(
            payload.get("summary_probability"), "总结概率非法"
        ),
        effective_reply=cast("ReplyEligibility | None", effective_reply),
        retain_recent=_row_optional_bool(payload.get("retain_recent"), "保留标记非法"),
        memory_candidate=_row_optional_bool(
            payload.get("memory_candidate"), "记忆候选标记非法"
        ),
        optional_selected=_row_optional_bool(
            payload.get("optional_selected"), "抽签标记非法"
        ),
        references=ReferenceSummary(
            complete=_row_bool(references.get("complete", True), "引用完整性标记非法"),
            resolved_count=_row_count(references.get("resolved_count", 0)),
            missing_count=_row_count(references.get("missing_count", 0)),
            cycle_count=_row_count(references.get("cycle_count", 0)),
            timed_out=_row_bool(references.get("timed_out", False), "引用超时标记非法"),
        ),
        durations=PhaseDurations(
            queue_ms=_row_optional_float(durations.get("queue_ms"), "排队耗时非法"),
            references_ms=_row_optional_float(
                durations.get("references_ms"), "引用耗时非法"
            ),
            request_ms=_row_optional_float(durations.get("request_ms"), "请求耗时非法"),
        ),
        usage=DecisionUsage(
            input_tokens=_row_optional_int(usage.get("input_tokens"), "输入用量非法"),
            output_tokens=_row_optional_int(usage.get("output_tokens"), "输出用量非法"),
        ),
        config_revision=_row_optional_int(
            payload.get("config_revision"), "配置修订非法"
        ),
        prompt_revision=_row_optional_int(
            payload.get("prompt_revision"), "Prompt 修订非法"
        ),
        requested_model=_row_optional_text(
            payload.get("requested_model"), "请求模型非法"
        ),
        actual_model=_row_optional_text(payload.get("actual_model"), "实际模型非法"),
    )


def _row_literal(value: object, allowed: frozenset[str], detail: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise DecisionLogCorruptRowError(detail)
    return value


def _row_optional_literal(
    value: object, allowed: frozenset[str], detail: str
) -> str | None:
    if value is None:
        return None
    return _row_literal(value, allowed, detail)


def _row_mapping(
    value: object, allowed: frozenset[str], detail: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) - allowed:
        raise DecisionLogCorruptRowError(detail)
    return value


def _row_choice(
    value: object, allowed: frozenset[str]
) -> tuple[str, float, tuple[tuple[str, float], ...]] | None:
    if value is None:
        return None
    payload = _row_mapping(value, _CHOICE_FIELDS, "选择结果非法")
    chosen = _row_literal(payload.get("value"), allowed, "选择结果取值非法")
    confidence = _row_required_float(
        payload.get("confidence"), "选择结果置信度非法"
    )
    raw_probabilities = payload.get("probabilities")
    if not isinstance(raw_probabilities, (list, tuple)):
        raise DecisionLogCorruptRowError("选择结果概率分布非法")
    probabilities: list[tuple[str, float]] = []
    for item in raw_probabilities:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise DecisionLogCorruptRowError("选择结果概率分布非法")
        label, probability = item
        probabilities.append(
            (
                _row_literal(label, allowed, "选择结果概率标签非法"),
                _row_required_float(probability, "选择结果概率非法"),
            )
        )
    return chosen, confidence, tuple(probabilities)


def _row_bool(value: object, detail: str) -> bool:
    if not isinstance(value, bool):
        raise DecisionLogCorruptRowError(detail)
    return value


def _row_optional_bool(value: object, detail: str) -> bool | None:
    if value is None:
        return None
    return _row_bool(value, detail)


def _row_count(value: object) -> int:
    count = _row_optional_int(value, "引用计数非法")
    if count is None or count < 0:
        raise DecisionLogCorruptRowError("引用计数非法")
    return count


def _row_optional_int(value: object, detail: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise DecisionLogCorruptRowError(detail)
    return value


def _row_required_float(value: object, detail: str) -> float:
    number = _row_optional_float(value, detail)
    if number is None:
        raise DecisionLogCorruptRowError(detail)
    return number


def _row_optional_float(value: object, detail: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DecisionLogCorruptRowError(detail)
    return float(value)


def _row_optional_text(value: object, detail: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DecisionLogCorruptRowError(detail)
    return value


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_PRUNE_BATCH",
    "MAX_PAGE_SIZE",
    "MAX_PRUNE_BATCH",
    "STORAGE_UNAVAILABLE_MESSAGE",
    "SUPPORTED_RETENTION_DAYS_MAX",
    "SUPPORTED_RETENTION_DAYS_MIN",
    "DecisionLogBody",
    "DecisionLogCorruptRowError",
    "DecisionLogPage",
    "DecisionLogRepository",
    "DecisionLogRequestError",
    "DecisionLogRetentionError",
    "DecisionLogStorageUnavailable",
]
