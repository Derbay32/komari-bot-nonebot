"""判定持久化：事件占位、共享调度、单次交接与进程所有权。

设计要点（TSK-180 §6、TSK-182、182-core-design）：

- 事件身份（协议/账号/群/消息）唯一；重复插入的失败方**不执行**，即使胜者
  已经崩溃。身份行先于任何引用补取与 SDK 调用写入。
- 所有会改变队列/占位/交接状态的写入都在同一个 PostgreSQL 事务级 advisory
  lock 下完成，与迁移中的停用触发器使用同一把锁，因此停用撤销与调度不会
  交错。
- 每个引擎进程持有一条**专用**连接的会话级 advisory lock 作为存活证明；其余
  事务用普通 ORM 会话。绝不为每个等待任务各开一条租约连接，也不使用 TTL
  心跳回收活着的持有者：只有**取得**别人的会话锁才证明对方已停止。
- 队列截止、授权时间与装填比较一律使用数据库时钟（``clock_timestamp()``），
  不做跨主机墙钟运算。
- 完成与两种交接都带「实例/分发/代数」围栏；外部效果前由调用方提供的
  ``effect_check`` 在锁内、紧贴写入前复核准入裁决。
"""

from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic
from typing import TYPE_CHECKING, Any, Final, cast

from sqlalchemy import (
    ColumnElement,
    CursorResult,
    and_,
    func,
    select,
    text,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession
from sqlalchemy.orm import aliased
from sqlmodel import col

from komari_bot.decision.jev import EventIdentity
from komari_bot.plugins.komari_decision.config_schema import (
    KomariDecisionConfigSchema,
)
from komari_bot.plugins.komari_decision.orm_models import (
    DecisionDispatchRow,
    DecisionGroupRow,
    DecisionProcessRow,
    DecisionSchedulerRow,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence

SCHEDULER_LOCK_NAMESPACE: Final = 1262765379
"""与迁移的停用触发器共用的 advisory lock 命名空间。"""

SCHEDULER_LOCK_KEY: Final = 0
"""调度事务锁键（全局唯一，序列化身份/调度/撤销/交接）。"""

STATE_QUEUED: Final = "queued"
STATE_RUNNING: Final = "running"
STATE_SUCCESS: Final = "success"
STATE_FILTERED: Final = "filtered"
STATE_BYPASS: Final = "bypass"
STATE_FAILED: Final = "failed"
STATE_CANCELLED: Final = "cancelled"

_UNHANDED_STATES: Final = (
    STATE_QUEUED,
    STATE_RUNNING,
    STATE_SUCCESS,
    STATE_BYPASS,
    STATE_FAILED,
)

_LIVE_STATES: Final = (STATE_QUEUED, STATE_RUNNING)

_IN_FLIGHT_REASON: Final = "queued"
"""调度中间态行的持久化原因码（不进入业务结果）。"""


def _db_now() -> ColumnElement[datetime]:
    """数据库时钟表达式（事务内真实时刻，非事务开始时间）。"""
    return cast("ColumnElement[datetime]", func.clock_timestamp())


def _current_generation() -> ColumnElement[int]:
    """调度器当前代数（停用触发器会自增它）。"""
    return cast(
        "ColumnElement[int]",
        select(col(DecisionSchedulerRow.generation))
        .where(col(DecisionSchedulerRow.id) == 1)
        .scalar_subquery(),
    )


def _queue_ms(row: DecisionDispatchRow) -> float | None:
    """用持久时间戳计算真实排队时长（数据库时钟写入，毫秒）。"""
    if row.enqueued_at is None:
        return None
    end = row.granted_at or row.finished_at
    if end is None:
        return None
    return max(0.0, (end - row.enqueued_at).total_seconds() * 1000.0)


def _rowcount(result: object) -> int:
    """读取 DML 影响行数（SQLAlchemy 的 Result 联合类型收窄）。"""
    return int(cast("CursorResult[Any]", result).rowcount)


class ReservationKind(StrEnum):
    """身份占位结果。"""

    ENQUEUED = "enqueued"
    TERMINAL = "terminal"
    REUSED = "reused"
    DUPLICATE = "duplicate"
    QUEUE_FULL = "queue_full"
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True, kw_only=True)
class ReservePlan:
    """调用方在进入串行化边界前冻结的占位意图。"""

    decision_id: str
    dispatch_id: str
    wants_slot: bool
    queue_timeout_seconds: float
    terminal_state: str | None = None
    terminal_reason: str | None = None
    failure_notification: bool = False
    """是否为需要一次性失败通知资格的事件（总结候选的技术失败）。"""


@dataclass(frozen=True, slots=True, kw_only=True)
class Reservation:
    """一次占位的结果（不携带任何业务分类）。"""

    kind: ReservationKind
    row_id: int | None = None
    decision_id: str | None = None
    generation: int | None = None
    reason_code: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class GrantOutcome:
    """等待执行名额的结果（含数据库时钟测得的排队时长）。"""

    granted: bool
    state: str
    reason_code: str | None = None
    queue_ms: float | None = None
    """从持久 enqueued_at 到 granted_at（或终结时刻）的真实排队毫秒数。"""


@dataclass(frozen=True, slots=True, kw_only=True)
class SchedulerObservation:
    """一次调度事务观察到的部署级执行/等待数量。"""

    running: int
    queued: int


@dataclass(frozen=True, slots=True, kw_only=True)
class OwnedWorkRow:
    """本实例仍登记为在途、但可能已被撤销的一行工作。"""

    identity: EventIdentity
    dispatch_id: str
    state: str
    generation: int
    current_generation: int
    slot_held: bool
    handed_off: bool

    @property
    def generation_current(self) -> bool:
        """该行仍属于调度器当前代数（停用会推进代数）。"""
        return self.generation == self.current_generation

    @property
    def current(self) -> bool:
        """仍是本实例当前代数的在途行（未被撤销/中断）。"""
        return self.state in _LIVE_STATES and self.generation_current


@dataclass(frozen=True, slots=True, kw_only=True)
class RecentSettlementFact:
    """一次 provisional 定案所需的持久事实（无正文、无分类推断）。"""

    state: str
    reason_code: str
    recent_retained: bool | None
    handed_off: bool

    @property
    def settled(self) -> bool:
        """已终结的持久行（不再可能由活着的所有者继续推进）。"""
        return self.state not in _LIVE_STATES


@dataclass(frozen=True, slots=True, kw_only=True)
class _LiveControl:
    """调度事务内读取的实时部署控制值。"""

    plugin_enable: bool
    max_concurrent_decisions: int
    max_queued_messages_per_group: int
    generation: int


class JevDecisionRepository:
    """判定持久化与调度仓储。"""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
    ) -> None:
        self._session_factory = session_factory
        self._process_id: int | None = None
        self._pseudonym_salt: bytes | None = None
        self._connection: AsyncConnection | None = None
        self._closed = False
        self.last_observation: SchedulerObservation | None = None
        self.last_observation_at: datetime | None = None

    # ------------------------------ 生命周期 ------------------------------

    @property
    def ready(self) -> bool:
        """是否已成功取得持久身份与所有权。"""
        return self._process_id is not None and self._connection is not None

    @property
    def process_id(self) -> int:
        """本进程实例 id（未初始化时明确失败）。"""
        if self._process_id is None:
            msg = "判定存储尚未取得进程实例 id"
            raise RuntimeError(msg)
        return self._process_id

    @property
    def pseudonym_salt(self) -> bytes:
        """部署私有代号盐（32 字节）。"""
        if self._pseudonym_salt is None:
            msg = "判定存储尚未读取调度器代号盐"
            raise RuntimeError(msg)
        return self._pseudonym_salt

    async def initialize(self) -> None:
        """取得持久身份与所有权；失败时不留任何自有连接或锁。

        所有权（连接与进程 id）只在调度事务**提交成功之后**才发布为实例状态：
        提交失败或被取消时，这条自有连接仍归本方法的清理逻辑所有。
        """
        if self.ready:
            return
        await self._ensure_scheduler_row()
        pending: AsyncConnection | None = None
        acquired: tuple[AsyncConnection, int, bytes] | None = None
        try:
            async with self._scheduler() as session:
                await self._reconcile_dead_owners(session)
                salt = await self._read_salt(session)
                pending = await self._open_ownership_connection()
                process_id = await self._allocate_process_id(session)
                if not await _try_session_lock(pending, process_id):
                    msg = "无法取得判定进程所有权锁"
                    raise RuntimeError(msg)
                acquired = (pending, process_id, salt)
                pending = None  # 提交前不发布任何实例状态
            self._connection, self._process_id, self._pseudonym_salt = acquired
            acquired = None
        finally:
            if pending is not None:
                # 取锁之后失败或取消：显式解锁/作废这条自有连接，绝不漏锁。
                await _release_ownership_connection(pending, None)
            if acquired is not None:
                await _release_ownership_connection(acquired[0], acquired[1])

    async def close(self) -> None:
        """撤销本进程尚未交接的资格，并释放自有租约与连接。"""
        if self._closed:
            return
        self._closed = True
        process_id = self._process_id
        connection = self._connection
        self._connection = None
        self._process_id = None
        if process_id is not None:
            with suppress(Exception):
                async with self._scheduler() as session:
                    await self._revoke_own_unhanded(session, process_id)
                    await session.execute(
                        update(DecisionProcessRow)
                        .where(col(DecisionProcessRow.id) == process_id)
                        .values(stopped_at=_db_now())
                    )
        if connection is not None:
            await _release_ownership_connection(connection, process_id)

    async def check_lease(self) -> bool:
        """检查本进程租约连接与所有权锁是否仍有效。"""
        connection = self._connection
        process_id = self._process_id
        if connection is None or process_id is None:
            return False
        try:
            result = await connection.execute(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
                    " AND pid = pg_backend_pid() AND classid = :namespace"
                    " AND objid = :key AND objsubid = 2"
                ),
                {"namespace": SCHEDULER_LOCK_NAMESPACE, "key": process_id},
            )
            return int(result.scalar() or 0) > 0
        except Exception:
            return False

    # ------------------------------ 串行化边界 ------------------------------

    @asynccontextmanager
    async def _scheduler(self) -> AsyncIterator[AsyncSession]:
        """事务级串行化边界：BEGIN + 调度 advisory lock。"""
        session = self._session_factory()
        try:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:namespace, :key)"),
                {"namespace": SCHEDULER_LOCK_NAMESPACE, "key": SCHEDULER_LOCK_KEY},
            )
            yield session
            await session.commit()
        except BaseException:
            with suppress(Exception):
                await session.rollback()
            raise
        finally:
            await session.close()

    @asynccontextmanager
    async def ingress(self) -> AsyncIterator[Ingress]:
        """串行化入口：身份占位、入队与 Redis 输入快照在同一临界区内。"""
        async with self._scheduler() as session:
            yield Ingress(repository=self, session=session)

    # ------------------------------ 只读查询与等待 ------------------------------

    async def read_row(self, row_id: int) -> DecisionDispatchRow | None:
        """读取一行占位记录的最新状态。"""
        session = self._session_factory()
        try:
            return await session.get(DecisionDispatchRow, row_id)
        finally:
            await session.close()

    async def recent_facts(
        self,
        session: AsyncSession,
        *,
        protocol: str,
        account_id: str,
        group_id: str,
        message_ids: Sequence[str],
    ) -> dict[str, RecentSettlementFact]:
        """读取作用域内这些消息的持久定案事实（元数据，不读正文）。

        必须在调度事务内调用：此前的死主回收已经保证仍为 queued/running 的行
        只属于活着的所有者。
        """
        if not message_ids:
            return {}
        rows = await session.scalars(
            select(DecisionDispatchRow).where(
                col(DecisionDispatchRow.protocol) == protocol,
                col(DecisionDispatchRow.account_id) == account_id,
                col(DecisionDispatchRow.group_id) == group_id,
                col(DecisionDispatchRow.message_id).in_(list(message_ids)),
            )
        )
        return {
            row.message_id: RecentSettlementFact(
                state=row.state,
                reason_code=row.reason_code,
                recent_retained=(
                    None if row.recent_retained is None else bool(row.recent_retained)
                ),
                handed_off=row.handed_off_at is not None,
            )
            for row in rows
        }

    async def is_current(
        self,
        *,
        identity: EventIdentity,
        decision_id: str,
        dispatch_id: str,
    ) -> bool:
        """本实例的同次分发仍是在途、当前代数且未交接（外部效果前的校验）。"""
        if not self.ready:
            return False
        session = self._session_factory()
        try:
            found = await session.scalar(
                select(func.count())
                .select_from(DecisionDispatchRow)
                .where(
                    col(DecisionDispatchRow.protocol) == identity.protocol,
                    col(DecisionDispatchRow.account_id) == identity.account_id,
                    col(DecisionDispatchRow.group_id) == identity.group_id,
                    col(DecisionDispatchRow.message_id) == identity.message_id,
                    col(DecisionDispatchRow.decision_id) == decision_id,
                    col(DecisionDispatchRow.dispatch_id) == dispatch_id,
                    col(DecisionDispatchRow.owner_process_id) == self.process_id,
                    col(DecisionDispatchRow.state).in_(_LIVE_STATES),
                    col(DecisionDispatchRow.handed_off_at).is_(None),
                    col(DecisionDispatchRow.generation) == _current_generation(),
                )
            )
            return bool(int(found or 0))
        finally:
            await session.close()

    async def observe_owned_work(
        self,
        tracked: Sequence[tuple[EventIdentity, str]],
    ) -> tuple[OwnedWorkRow, ...]:
        """读取本实例**本地仍跟踪**的工作行（不扫描全部历史，不做写入）。

        只按调用方给出的 (事件身份, dispatch id) 查询，避免反复扫描该 owner 的
        永久历史；行一旦不在本地跟踪集合中就不再查询。
        """
        process_id = self._process_id
        if process_id is None or not tracked:
            return ()
        identities = {identity for identity, _dispatch in tracked}
        session = self._session_factory()
        try:
            generation = int(
                await session.scalar(
                    select(col(DecisionSchedulerRow.generation)).where(
                        col(DecisionSchedulerRow.id) == 1
                    )
                )
                or 0
            )
            rows = (
                await session.scalars(
                    select(DecisionDispatchRow).where(
                        col(DecisionDispatchRow.owner_process_id) == process_id,
                        tuple_(
                            col(DecisionDispatchRow.protocol),
                            col(DecisionDispatchRow.account_id),
                            col(DecisionDispatchRow.group_id),
                            col(DecisionDispatchRow.message_id),
                        ).in_(
                            [
                                (
                                    identity.protocol,
                                    identity.account_id,
                                    identity.group_id,
                                    identity.message_id,
                                )
                                for identity in identities
                            ]
                        ),
                    )
                )
            ).all()
            handled = {
                (EventIdentity(
                    protocol=row.protocol,
                    account_id=row.account_id,
                    group_id=row.group_id,
                    message_id=row.message_id,
                ), row.dispatch_id): row
                for row in rows
            }
            return tuple(
                OwnedWorkRow(
                    identity=identity,
                    dispatch_id=dispatch_id,
                    state=row.state,
                    generation=int(row.generation),
                    current_generation=generation,
                    slot_held=bool(row.slot_held),
                    handed_off=row.handed_off_at is not None,
                )
                for (identity, dispatch_id), row in handled.items()
                if (identity, dispatch_id) in set(tracked)
            )
        finally:
            await session.close()

    async def wait_for_grant(
        self,
        *,
        row_id: int,
        dispatch_id: str,
        timeout_seconds: float,
        failure_notification: bool = False,
        poll_seconds: float = 0.02,
    ) -> GrantOutcome:
        """等待本行取得执行名额（或用完排队截止）。"""
        loop_deadline = monotonic() + timeout_seconds + 1.0
        poll_deadline = monotonic() + timeout_seconds
        next_pass = monotonic()
        while True:
            if monotonic() >= next_pass:
                # 共享容量上调、或他进程释放名额时，等待方也需要重新触发分配。
                next_pass = monotonic() + 0.1
                with suppress(Exception):
                    await self._grant_pass(None)
            row = await self.read_row(row_id)
            if row is None:
                return GrantOutcome(
                    granted=False, state=STATE_FAILED, reason_code="interrupted"
                )
            if row.dispatch_id != dispatch_id:
                return GrantOutcome(
                    granted=False,
                    state=STATE_CANCELLED,
                    reason_code="interrupted",
                    queue_ms=_queue_ms(row),
                )
            if row.state == STATE_RUNNING and row.granted_at is not None:
                return GrantOutcome(
                    granted=True, state=row.state, queue_ms=_queue_ms(row)
                )
            if row.state in {STATE_CANCELLED, STATE_FAILED, STATE_SUCCESS,
                             STATE_BYPASS, STATE_FILTERED}:
                return GrantOutcome(
                    granted=False,
                    state=row.state,
                    reason_code=row.reason_code,
                    queue_ms=_queue_ms(row),
                )
            if monotonic() >= poll_deadline:
                if await self.expire_own(
                    row_id=row_id,
                    dispatch_id=dispatch_id,
                    failure_notification=failure_notification,
                ):
                    expired = await self.read_row(row_id)
                    return GrantOutcome(
                        granted=False,
                        state=STATE_FAILED,
                        reason_code="queue_timeout",
                        queue_ms=None if expired is None else _queue_ms(expired),
                    )
                # 数据库时钟尚未到期（跨主机墙钟不可信）：继续等待。
                poll_deadline = monotonic() + 0.05
            if monotonic() >= loop_deadline:
                return GrantOutcome(
                    granted=False, state=STATE_FAILED, reason_code="queue_timeout"
                )
            await asyncio.sleep(poll_seconds)

    async def expire_own(
        self,
        *,
        row_id: int,
        dispatch_id: str,
        failure_notification: bool = False,
    ) -> bool:
        """把已过数据库截止的本行标记为排队超时。"""
        async with self._scheduler() as session:
            result = await session.execute(
                update(DecisionDispatchRow)
                .where(
                    col(DecisionDispatchRow.id) == row_id,
                    col(DecisionDispatchRow.owner_process_id) == self.process_id,
                    col(DecisionDispatchRow.dispatch_id) == dispatch_id,
                    col(DecisionDispatchRow.state) == STATE_QUEUED,
                    col(DecisionDispatchRow.queue_deadline) <= _db_now(),
                )
                .values(
                    state=STATE_FAILED,
                    reason_code="queue_timeout",
                    finished_at=_db_now(),
                    notification_eligible=failure_notification,
                )
            )
            return bool(_rowcount(result))

    # ------------------------------ 调度内部 ------------------------------

    async def _ensure_scheduler_row(self) -> None:
        """确保调度单例行存在（数据初始化，不是运行时 DDL）。"""
        statement = (
            postgres_insert(DecisionSchedulerRow)
            .values(
                id=1,
                pseudonym_salt=secrets.token_bytes(32),
                next_grant_sequence=0,
                generation=0,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        async with self._scheduler() as session:
            await session.execute(statement)

    async def _read_salt(self, session: AsyncSession) -> bytes:
        salt = await session.scalar(
            select(col(DecisionSchedulerRow.pseudonym_salt)).where(
                col(DecisionSchedulerRow.id) == 1
            )
        )
        if salt is None:
            msg = "判定调度单例行缺少代号盐"
            raise RuntimeError(msg)
        return bytes(salt)

    async def _allocate_process_id(self, session: AsyncSession) -> int:
        """在调度锁仍然排除回收时分配并登记本进程实例 id。"""
        process_id = await session.scalar(
            postgres_insert(DecisionProcessRow)
            .values(started_at=_db_now())
            .returning(col(DecisionProcessRow.id))
        )
        if process_id is None:
            msg = "无法分配判定进程实例 id"
            raise RuntimeError(msg)
        return int(process_id)

    async def _open_ownership_connection(self) -> AsyncConnection:
        """从共享 ORM 绑定的公开 API 取得一条**自有**专用连接。"""
        session = self._session_factory()
        try:
            bind = session.bind
        finally:
            await session.close()
        if isinstance(bind, AsyncEngine):
            return await bind.connect()
        if isinstance(bind, AsyncConnection):
            # 会话绑定在调用方自有连接上：从它的公开 engine 另取一条，
            # 绝不借用（更不会关闭）调用方的连接。
            return await bind.engine.connect()
        msg = "判定存储无法从会话绑定取得异步引擎"
        raise RuntimeError(msg)

    async def _reconcile_dead_owners(self, session: AsyncSession) -> None:
        """探测已停止实例：**取得**其会话锁才算已死，活着的一律不动。"""
        owners = await session.scalars(
            select(col(DecisionProcessRow.id)).where(
                col(DecisionProcessRow.stopped_at).is_(None),
                col(DecisionProcessRow.id) != (self._process_id or 0),
            )
        )
        for candidate in list(owners):
            if candidate is None:
                continue
            owner_id = int(candidate)
            acquired = await session.scalar(
                text("SELECT pg_try_advisory_xact_lock(:namespace, :key)"),
                {"namespace": SCHEDULER_LOCK_NAMESPACE, "key": owner_id},
            )
            if not acquired:
                # 该实例的会话锁仍被持有：活着，绝不回收它的工作与名额。
                continue
            await session.execute(
                update(DecisionDispatchRow)
                .where(
                    col(DecisionDispatchRow.owner_process_id) == owner_id,
                    col(DecisionDispatchRow.handed_off_at).is_(None),
                    col(DecisionDispatchRow.state).in_(_UNHANDED_STATES),
                )
                .values(
                    state=STATE_CANCELLED,
                    reason_code="interrupted",
                    business_route="none",
                    notification_eligible=False,
                    slot_held=False,
                    finished_at=_db_now(),
                )
            )
            await self._release_retired_owner_slots(session, owner_id)
            await session.execute(
                update(DecisionProcessRow)
                .where(col(DecisionProcessRow.id) == owner_id)
                .values(stopped_at=_db_now())
            )

    async def _live_control(self, session: AsyncSession) -> _LiveControl | None:
        """读取实时部署控制（关闭状态与共享容量）。"""
        config = await session.get(KomariDecisionConfigSchema, 1)
        scheduler = await session.get(DecisionSchedulerRow, 1)
        if config is None or scheduler is None:
            return None
        return _LiveControl(
            plugin_enable=bool(config.plugin_enable),
            max_concurrent_decisions=int(config.max_concurrent_decisions),
            max_queued_messages_per_group=int(
                config.max_queued_messages_per_group
            ),
            generation=int(scheduler.generation),
        )

    async def _expire_overdue(self, session: AsyncSession) -> None:
        """用数据库时钟把超过排队截止的行标记为超时。"""
        await session.execute(
            update(DecisionDispatchRow)
            .where(
                col(DecisionDispatchRow.state) == STATE_QUEUED,
                col(DecisionDispatchRow.queue_deadline) <= _db_now(),
            )
            .values(
                state=STATE_FAILED,
                reason_code="queue_timeout",
                finished_at=_db_now(),
            )
        )

    async def _next_sequence(self, session: AsyncSession) -> int:
        value = await session.scalar(
            select(col(DecisionSchedulerRow.next_grant_sequence)).where(
                col(DecisionSchedulerRow.id) == 1
            )
        )
        return int(value or 0)

    async def _grant_pass(
        self,
        session: AsyncSession | None,
        *,
        control: _LiveControl | None = None,
    ) -> SchedulerObservation | None:
        """在调度锁内按群轮转分配执行名额。"""
        if session is None:
            async with self._scheduler() as owned:
                return await self._grant_pass(owned, control=control)
        if control is None:
            control = await self._live_control(session)
        if control is None:
            return None
        await self._expire_overdue(session)
        running = int(
            await session.scalar(
                select(func.count())
                .select_from(DecisionDispatchRow)
                .where(col(DecisionDispatchRow.slot_held).is_(True))
            )
            or 0
        )
        free = control.max_concurrent_decisions - running
        sequence = await self._next_sequence(session)
        while free > 0:
            candidate = await self._next_candidate(session)
            if candidate is None:
                break
            protocol, account_id, group_id, oldest = candidate
            sequence += 1
            await session.execute(
                update(DecisionSchedulerRow)
                .where(col(DecisionSchedulerRow.id) == 1)
                .values(next_grant_sequence=sequence)
            )
            await session.execute(
                update(DecisionGroupRow)
                .where(
                    col(DecisionGroupRow.protocol) == protocol,
                    col(DecisionGroupRow.account_id) == account_id,
                    col(DecisionGroupRow.group_id) == group_id,
                )
                .values(last_grant_sequence=sequence)
            )
            result = await session.execute(
                update(DecisionDispatchRow)
                .where(
                    col(DecisionDispatchRow.id) == oldest,
                    col(DecisionDispatchRow.state) == STATE_QUEUED,
                )
                .values(
                    state=STATE_RUNNING,
                    granted_at=_db_now(),
                    slot_held=True,
                )
            )
            if not _rowcount(result):
                break
            running += 1
            free -= 1
        queued = int(
            await session.scalar(
                select(func.count())
                .select_from(DecisionDispatchRow)
                .where(col(DecisionDispatchRow.state) == STATE_QUEUED)
            )
            or 0
        )
        observation = SchedulerObservation(running=running, queued=queued)
        self.last_observation = observation
        self.last_observation_at = datetime.now(UTC)
        return observation

    async def _next_candidate(
        self, session: AsyncSession
    ) -> tuple[str, str, str, int] | None:
        """选出下一个应被授权的群及其最早等待行。"""
        group = aliased(DecisionGroupRow)
        dispatch = aliased(DecisionDispatchRow)
        busy = aliased(DecisionDispatchRow)
        busy_in_group = (
            select(1)
            .where(
                col(busy.slot_held).is_(True),
                col(busy.protocol) == col(group.protocol),
                col(busy.account_id) == col(group.account_id),
                col(busy.group_id) == col(group.group_id),
            )
            .exists()
        )
        statement = (
            select(
                col(group.protocol),
                col(group.account_id),
                col(group.group_id),
                func.min(col(dispatch.id)).label("oldest"),
            )
            .join(
                dispatch,
                and_(
                    col(dispatch.protocol) == col(group.protocol),
                    col(dispatch.account_id) == col(group.account_id),
                    col(dispatch.group_id) == col(group.group_id),
                ),
            )
            .where(
                col(dispatch.state) == STATE_QUEUED,
                col(dispatch.queue_deadline) > _db_now(),
                ~busy_in_group,
            )
            .group_by(
                col(group.protocol),
                col(group.account_id),
                col(group.group_id),
                col(group.last_grant_sequence),
            )
            .order_by(
                col(group.last_grant_sequence).asc(),
                func.min(col(dispatch.id)).asc(),
            )
            .limit(1)
        )
        row = (await session.execute(statement)).first()
        if row is None:
            return None
        return (str(row[0]), str(row[1]), str(row[2]), int(row[3]))

    async def _revoke_own_unhanded(
        self, session: AsyncSession, process_id: int
    ) -> None:
        """撤销本进程尚未交接的全部资格（关闭与中断语义）。"""
        await session.execute(
            update(DecisionDispatchRow)
            .where(
                col(DecisionDispatchRow.owner_process_id) == process_id,
                col(DecisionDispatchRow.handed_off_at).is_(None),
                col(DecisionDispatchRow.state).in_(_UNHANDED_STATES),
            )
            .values(
                state=STATE_CANCELLED,
                reason_code="interrupted",
                business_route="none",
                notification_eligible=False,
                slot_held=False,
                finished_at=_db_now(),
            )
        )

        await self._release_retired_owner_slots(session, process_id)

    async def _release_retired_owner_slots(
        self, session: AsyncSession, process_id: int
    ) -> None:
        """所有者已停止后释放残留名额，保留已取消行的撤权原因与缓存处置。"""
        await session.execute(
            update(DecisionDispatchRow)
            .where(
                col(DecisionDispatchRow.owner_process_id) == process_id,
                col(DecisionDispatchRow.slot_held).is_(True),
            )
            .values(slot_held=False)
        )

    # ------------------------------ 完成 / 交接 / 撤销 ------------------------------

    async def _row_for(
        self,
        session: AsyncSession,
        identity: EventIdentity,
        decision_id: str,
    ) -> DecisionDispatchRow | None:
        return (
            await session.scalars(
                select(DecisionDispatchRow).where(
                    col(DecisionDispatchRow.protocol) == identity.protocol,
                    col(DecisionDispatchRow.account_id) == identity.account_id,
                    col(DecisionDispatchRow.group_id) == identity.group_id,
                    col(DecisionDispatchRow.message_id) == identity.message_id,
                    col(DecisionDispatchRow.decision_id) == decision_id,
                )
            )
        ).first()

    def _fence(
        self,
        row: DecisionDispatchRow,
        *,
        dispatch_id: str,
        decision_id: str,
    ) -> tuple[ColumnElement[bool], ...]:
        """归属/分发/身份/代数围栏：只有本实例当前代数的同次分发可行事。"""
        return (
            col(DecisionDispatchRow.id) == int(row.id or 0),
            col(DecisionDispatchRow.owner_process_id) == self.process_id,
            col(DecisionDispatchRow.dispatch_id) == dispatch_id,
            col(DecisionDispatchRow.decision_id) == decision_id,
            col(DecisionDispatchRow.generation) == _current_generation(),
        )

    async def finalize(
        self,
        *,
        identity: EventIdentity,
        decision_id: str,
        dispatch_id: str,
        status: str,
        reason_code: str,
        business_route: str,
        notification_eligible: bool,
        recent_retained: bool | None = None,
    ) -> bool:
        """写入终态（含允许路由与输入缓存处置事实）并释放名额。"""
        if not self.ready:
            return False
        async with self._scheduler() as session:
            row = await self._row_for(session, identity, decision_id)
            if row is None:
                return False
            result = await session.execute(
                update(DecisionDispatchRow)
                .where(
                    *self._fence(
                        row, dispatch_id=dispatch_id, decision_id=decision_id
                    ),
                    col(DecisionDispatchRow.state).in_(_LIVE_STATES),
                )
                .values(
                    state=status,
                    reason_code=reason_code,
                    finished_at=_db_now(),
                    slot_held=False,
                    business_route=business_route,
                    notification_eligible=notification_eligible,
                    recent_retained=recent_retained,
                )
            )
            completed = bool(_rowcount(result))
            await self._release_revoked_slot(
                session, row, dispatch_id=dispatch_id, decision_id=decision_id
            )
            await self._grant_pass(session)
            return completed

    async def release_slot(
        self,
        *,
        identity: EventIdentity,
        decision_id: str,
        dispatch_id: str,
    ) -> None:
        """只释放本实例仍持有的执行名额（不改写终态）。"""
        if not self.ready:
            return
        async with self._scheduler() as session:
            row = await self._row_for(session, identity, decision_id)
            if row is None:
                return
            await session.execute(
                update(DecisionDispatchRow)
                .where(
                    col(DecisionDispatchRow.id) == int(row.id or 0),
                    col(DecisionDispatchRow.owner_process_id) == self.process_id,
                    col(DecisionDispatchRow.dispatch_id) == dispatch_id,
                    col(DecisionDispatchRow.decision_id) == decision_id,
                    col(DecisionDispatchRow.slot_held).is_(True),
                )
                .values(slot_held=False)
            )
            await self._grant_pass(session)

    async def _release_revoked_slot(
        self,
        session: AsyncSession,
        row: DecisionDispatchRow,
        *,
        dispatch_id: str,
        decision_id: str,
    ) -> None:
        """撤销后的名额只在本地工作真正结束后释放。"""
        await session.execute(
            update(DecisionDispatchRow)
            .where(
                col(DecisionDispatchRow.id) == int(row.id or 0),
                col(DecisionDispatchRow.owner_process_id) == self.process_id,
                col(DecisionDispatchRow.dispatch_id) == dispatch_id,
                col(DecisionDispatchRow.decision_id) == decision_id,
                col(DecisionDispatchRow.state) == STATE_CANCELLED,
                col(DecisionDispatchRow.slot_held).is_(True),
            )
            .values(slot_held=False)
        )

    async def _revoke_locked(
        self,
        session: AsyncSession,
        *,
        identity: EventIdentity,
        decision_id: str,
        dispatch_id: str,
    ) -> bool:
        """在**已有**调度事务内撤销未交接资格（不释放仍在执行的执行名额）。"""
        row = await self._row_for(session, identity, decision_id)
        if row is None:
            return False
        result = await session.execute(
            update(DecisionDispatchRow)
            .where(
                col(DecisionDispatchRow.id) == int(row.id or 0),
                col(DecisionDispatchRow.owner_process_id) == self.process_id,
                col(DecisionDispatchRow.dispatch_id) == dispatch_id,
                col(DecisionDispatchRow.decision_id) == decision_id,
                col(DecisionDispatchRow.handed_off_at).is_(None),
                col(DecisionDispatchRow.state).in_(_UNHANDED_STATES),
            )
            .values(
                state=STATE_CANCELLED,
                reason_code="admission_revoked",
                business_route="none",
                notification_eligible=False,
                finished_at=func.coalesce(
                    col(DecisionDispatchRow.finished_at), _db_now()
                ),
            )
        )
        return bool(_rowcount(result))

    async def persist_revocation(
        self,
        *,
        identity: EventIdentity,
        decision_id: str,
        dispatch_id: str,
    ) -> None:
        """把观察到的撤销持久化：恢复准入也不会复活该事件。

        撤销只取消资格，**不释放执行名额**：名额由真正结束/清理完自有引用与
        SDK 工作的那条路径释放，避免替代任务在旧工作收尾期间进入外部效果。
        """
        if not self.ready:
            return
        async with self._scheduler() as session:
            await self._revoke_locked(
                session,
                identity=identity,
                decision_id=decision_id,
                dispatch_id=dispatch_id,
            )

    async def mark_failure_notification(
        self,
        *,
        identity: EventIdentity,
        decision_id: str,
        dispatch_id: str,
    ) -> bool:
        """给已终态的技术失败补上一次性失败通知资格（幂等）。"""
        if not self.ready:
            return False
        async with self._scheduler() as session:
            row = await self._row_for(session, identity, decision_id)
            if row is None:
                return False
            result = await session.execute(
                update(DecisionDispatchRow)
                .where(
                    *self._fence(
                        row, dispatch_id=dispatch_id, decision_id=decision_id
                    ),
                    col(DecisionDispatchRow.state) == STATE_FAILED,
                    col(DecisionDispatchRow.handed_off_at).is_(None),
                    col(DecisionDispatchRow.notification_eligible).is_(False),
                )
                .values(notification_eligible=True)
            )
            return bool(_rowcount(result))

    async def claim_handoff(
        self,
        *,
        identity: EventIdentity,
        decision_id: str,
        dispatch_id: str,
        route: str,
        status: str,
        reason_code: str,
        effect_check: Callable[[], bool],
    ) -> bool:
        """领取一次聊天/总结交接资格（同一行最多成功一次）。"""
        if not self.ready:
            return False
        async with self._scheduler() as session:
            row = await self._row_for(session, identity, decision_id)
            if row is None:
                return False
            # 锁内在写入前复核编排层裁决：撤权后既不得产生外部效果，也要把这次
            # 观察到的撤销持久化（同一事务内，不递归获取调度锁）。
            if not effect_check():
                await self._revoke_locked(
                    session,
                    identity=identity,
                    decision_id=decision_id,
                    dispatch_id=dispatch_id,
                )
                return False
            result = await session.execute(
                update(DecisionDispatchRow)
                .where(
                    *self._fence(
                        row, dispatch_id=dispatch_id, decision_id=decision_id
                    ),
                    col(DecisionDispatchRow.state) == status,
                    col(DecisionDispatchRow.reason_code) == reason_code,
                    col(DecisionDispatchRow.business_route) == route,
                    col(DecisionDispatchRow.handed_off_at).is_(None),
                )
                .values(handed_off_at=_db_now())
            )
            return bool(_rowcount(result))

    async def claim_notification(
        self,
        *,
        identity: EventIdentity,
        decision_id: str,
        dispatch_id: str,
        status: str,
        reason_code: str,
        effect_check: Callable[[], bool],
    ) -> bool:
        """领取一次失败通知资格（独立于业务交接的一次性资格）。"""
        if not self.ready:
            return False
        async with self._scheduler() as session:
            row = await self._row_for(session, identity, decision_id)
            if row is None:
                return False
            if not effect_check():
                await self._revoke_locked(
                    session,
                    identity=identity,
                    decision_id=decision_id,
                    dispatch_id=dispatch_id,
                )
                return False
            result = await session.execute(
                update(DecisionDispatchRow)
                .where(
                    *self._fence(
                        row, dispatch_id=dispatch_id, decision_id=decision_id
                    ),
                    col(DecisionDispatchRow.state) == status,
                    col(DecisionDispatchRow.reason_code) == reason_code,
                    col(DecisionDispatchRow.notification_eligible).is_(True),
                    col(DecisionDispatchRow.notified_at).is_(None),
                    col(DecisionDispatchRow.handed_off_at).is_(None),
                )
                .values(notified_at=_db_now())
            )
            return bool(_rowcount(result))


class Ingress:
    """串行化入口对象：在同一个事务里完成占位、入队与首次授权。"""

    def __init__(
        self, *, repository: JevDecisionRepository, session: AsyncSession
    ) -> None:
        self._repository = repository
        self._session = session

    async def reconcile(self) -> None:
        """在串行化入口内回收已停止实例的工作（必须先于输入定案与占位）。"""
        await self._repository._reconcile_dead_owners(self._session)  # pyright: ignore[reportPrivateUsage]

    async def settlement_facts(
        self,
        *,
        protocol: str,
        account_id: str,
        group_id: str,
        message_ids: Sequence[str],
    ) -> dict[str, RecentSettlementFact]:
        """在同一调度事务内读取 provisional 定案所需的持久元数据。"""
        return await self._repository.recent_facts(
            self._session,
            protocol=protocol,
            account_id=account_id,
            group_id=group_id,
            message_ids=message_ids,
        )

    async def reserve(
        self,
        *,
        request_identity: EventIdentity,
        plan: ReservePlan,
    ) -> Reservation:
        """写入事件身份并决定本次分发是否取得执行资格。

        调用方**必须**先调用 :meth:`reconcile`（死主回收）与输入定案，再占位。
        """
        session = self._session
        existing = await self._find_identity(request_identity)
        if existing is not None:
            return self._existing_reservation(existing, plan)
        control = await self._repository._live_control(session)  # pyright: ignore[reportPrivateUsage]
        if control is None:
            return Reservation(
                kind=ReservationKind.UNAVAILABLE, decision_id=plan.decision_id
            )
        terminal = self._terminal_plan(plan, control)
        if terminal is not None:
            state, reason, kind = terminal
            return await self._insert_terminal(
                request_identity=request_identity,
                plan=plan,
                state=state,
                reason=reason,
                generation=control.generation,
                kind=kind,
            )
        if await self._queue_is_full(session, request_identity, control):
            return await self._insert_terminal(
                request_identity=request_identity,
                plan=plan,
                state=STATE_FAILED,
                reason="queue_full",
                generation=control.generation,
                kind=ReservationKind.QUEUE_FULL,
            )
        return await self._enqueue(
            session=session,
            request_identity=request_identity,
            plan=plan,
            control=control,
        )

    def _existing_reservation(
        self, existing: DecisionDispatchRow, plan: ReservePlan
    ) -> Reservation:
        """同一次分发复用持久行；其余一律按重复事件处理。"""
        if (
            existing.owner_process_id == self._repository.process_id
            and existing.dispatch_id == plan.dispatch_id
        ):
            return Reservation(
                kind=ReservationKind.REUSED,
                row_id=int(existing.id or 0),
                decision_id=existing.decision_id,
                generation=int(existing.generation),
            )
        return Reservation(
            kind=ReservationKind.DUPLICATE,
            row_id=int(existing.id or 0),
            decision_id=existing.decision_id,
        )

    @staticmethod
    def _terminal_plan(
        plan: ReservePlan, control: _LiveControl
    ) -> tuple[str, str, ReservationKind] | None:
        """无需执行名额的入口路径（过滤/直通/配置失败/停用）。"""
        if plan.terminal_state == STATE_BYPASS:
            return (
                STATE_BYPASS,
                plan.terminal_reason or "platform_bypass",
                ReservationKind.TERMINAL,
            )
        if plan.terminal_state == STATE_FILTERED:
            return (
                STATE_FILTERED,
                plan.terminal_reason or "filtered",
                ReservationKind.TERMINAL,
            )
        if plan.terminal_state == STATE_CANCELLED:
            return (
                STATE_CANCELLED,
                plan.terminal_reason or "admission_revoked",
                ReservationKind.TERMINAL,
            )
        if plan.terminal_state == STATE_FAILED:
            return (
                STATE_FAILED,
                plan.terminal_reason or "internal_error",
                ReservationKind.TERMINAL,
            )
        if not control.plugin_enable:
            return (STATE_FAILED, "disabled", ReservationKind.DISABLED)
        return None

    async def _queue_is_full(
        self,
        session: AsyncSession,
        identity: EventIdentity,
        control: _LiveControl,
    ) -> bool:
        """用数据库时钟清理过期等待后判断该群队列是否已满。"""
        await self._repository._expire_overdue(session)  # pyright: ignore[reportPrivateUsage]
        waiting = int(
            await session.scalar(
                select(func.count())
                .select_from(DecisionDispatchRow)
                .where(
                    col(DecisionDispatchRow.state) == STATE_QUEUED,
                    col(DecisionDispatchRow.protocol) == identity.protocol,
                    col(DecisionDispatchRow.account_id) == identity.account_id,
                    col(DecisionDispatchRow.group_id) == identity.group_id,
                )
            )
            or 0
        )
        return waiting >= control.max_queued_messages_per_group

    async def _enqueue(
        self,
        *,
        session: AsyncSession,
        request_identity: EventIdentity,
        plan: ReservePlan,
        control: _LiveControl,
    ) -> Reservation:
        """写入等待行并立刻尝试一次授权（同群 FIFO + 群间轮转）。"""
        await self._ensure_group(request_identity, session=session)
        row_id = await session.scalar(
            postgres_insert(DecisionDispatchRow)
            .values(
                decision_id=plan.decision_id,
                protocol=request_identity.protocol,
                account_id=request_identity.account_id,
                group_id=request_identity.group_id,
                message_id=request_identity.message_id,
                owner_process_id=self._repository.process_id,
                dispatch_id=plan.dispatch_id,
                generation=control.generation,
                state=STATE_QUEUED,
                reason_code=_IN_FLIGHT_REASON,
                enqueued_at=_db_now(),
                queue_deadline=func.clock_timestamp()
                + func.make_interval(0, 0, 0, 0, 0, 0, plan.queue_timeout_seconds),
                slot_held=False,
                business_route="none",
                notification_eligible=False,
            )
            .returning(col(DecisionDispatchRow.id))
        )
        if row_id is None:
            msg = "判定事件身份写入后未返回行 id"
            raise RuntimeError(msg)
        await self._repository._grant_pass(session, control=control)  # pyright: ignore[reportPrivateUsage]
        return Reservation(
            kind=ReservationKind.ENQUEUED,
            row_id=int(row_id),
            decision_id=plan.decision_id,
            generation=control.generation,
        )

    async def finish_reserved(
        self,
        *,
        request_identity: EventIdentity,
        plan: ReservePlan,
    ) -> Reservation:
        """在外部执行前终止刚登记的事件，保留身份并释放尚未使用的名额。"""
        if plan.terminal_state not in {STATE_FAILED, STATE_CANCELLED}:
            msg = "入口终止必须指定失败或取消状态"
            raise ValueError(msg)
        session = self._session
        existing = await self._find_identity(request_identity)
        if existing is None:
            return Reservation(
                kind=ReservationKind.UNAVAILABLE, decision_id=plan.decision_id
            )
        await session.execute(
            update(DecisionDispatchRow)
            .where(
                col(DecisionDispatchRow.id) == int(existing.id or 0),
                col(DecisionDispatchRow.owner_process_id) == self._repository.process_id,
                col(DecisionDispatchRow.dispatch_id) == plan.dispatch_id,
                col(DecisionDispatchRow.decision_id) == plan.decision_id,
                col(DecisionDispatchRow.handed_off_at).is_(None),
            )
            .values(
                state=plan.terminal_state,
                reason_code=plan.terminal_reason,
                finished_at=_db_now(),
                slot_held=False,
                business_route="none",
                notification_eligible=plan.failure_notification,
                recent_retained=None,
            )
        )
        await self._repository._grant_pass(session)  # pyright: ignore[reportPrivateUsage]
        return Reservation(
            kind=ReservationKind.TERMINAL,
            row_id=int(existing.id or 0),
            decision_id=plan.decision_id,
            generation=int(existing.generation),
            reason_code=plan.terminal_reason,
        )

    async def _find_identity(
        self, identity: EventIdentity
    ) -> DecisionDispatchRow | None:
        return (
            await self._session.scalars(
                select(DecisionDispatchRow).where(
                    col(DecisionDispatchRow.protocol) == identity.protocol,
                    col(DecisionDispatchRow.account_id) == identity.account_id,
                    col(DecisionDispatchRow.group_id) == identity.group_id,
                    col(DecisionDispatchRow.message_id) == identity.message_id,
                )
            )
        ).first()

    async def _ensure_group(
        self, identity: EventIdentity, *, session: AsyncSession
    ) -> None:
        """新群进入轮转时排在当前轮转尾部（不获得永久优先）。

        已存在但当前没有在途工作的群（空闲群）重新活跃时同样移到尾部；
        已经活跃的群在每次入队时都不动位置。
        """
        current = await session.scalar(
            select(col(DecisionSchedulerRow.next_grant_sequence)).where(
                col(DecisionSchedulerRow.id) == 1
            )
        )
        inserted = await session.execute(
            postgres_insert(DecisionGroupRow)
            .values(
                protocol=identity.protocol,
                account_id=identity.account_id,
                group_id=identity.group_id,
                last_grant_sequence=int(current or 0),
            )
            .on_conflict_do_nothing(
                index_elements=["protocol", "account_id", "group_id"]
            )
        )
        if _rowcount(inserted):
            return
        active = aliased(DecisionDispatchRow)
        idle = ~(
            select(1)
            .where(
                col(active.protocol) == identity.protocol,
                col(active.account_id) == identity.account_id,
                col(active.group_id) == identity.group_id,
                (
                    (col(active.state) == STATE_QUEUED)
                    | col(active.slot_held).is_(True)
                ),
            )
            .exists()
        )
        await session.execute(
            update(DecisionGroupRow)
            .where(
                col(DecisionGroupRow.protocol) == identity.protocol,
                col(DecisionGroupRow.account_id) == identity.account_id,
                col(DecisionGroupRow.group_id) == identity.group_id,
                idle,
            )
            .values(last_grant_sequence=int(current or 0))
        )

    async def _insert_terminal(
        self,
        *,
        request_identity: EventIdentity,
        plan: ReservePlan,
        state: str,
        reason: str,
        generation: int,
        kind: ReservationKind = ReservationKind.TERMINAL,
    ) -> Reservation:
        row_id = await self._session.scalar(
            postgres_insert(DecisionDispatchRow)
            .values(
                decision_id=plan.decision_id,
                protocol=request_identity.protocol,
                account_id=request_identity.account_id,
                group_id=request_identity.group_id,
                message_id=request_identity.message_id,
                owner_process_id=self._repository.process_id,
                dispatch_id=plan.dispatch_id,
                generation=generation,
                state=state,
                reason_code=reason,
                enqueued_at=_db_now(),
                queue_deadline=_db_now(),
                finished_at=_db_now(),
                slot_held=False,
                business_route=("chat" if state == STATE_BYPASS else "none"),
                notification_eligible=plan.failure_notification,
            )
            .returning(col(DecisionDispatchRow.id))
        )
        if row_id is None:
            msg = "判定事件身份写入后未返回行 id"
            raise RuntimeError(msg)
        return Reservation(
            kind=kind,
            row_id=int(row_id),
            decision_id=plan.decision_id,
            generation=generation,
            reason_code=reason,
        )


async def _try_session_lock(connection: AsyncConnection, process_id: int) -> bool:
    result = await connection.execute(
        text("SELECT pg_try_advisory_lock(:namespace, :key)"),
        {"namespace": SCHEDULER_LOCK_NAMESPACE, "key": process_id},
    )
    return bool(result.scalar())


async def _release_ownership_connection(
    connection: AsyncConnection, process_id: int | None
) -> None:
    """显式解锁后归还连接；解锁或连接健康失败则作废连接。"""
    healthy = False
    if process_id is not None:
        try:
            result = await connection.execute(
                text("SELECT pg_advisory_unlock(:namespace, :key)"),
                {"namespace": SCHEDULER_LOCK_NAMESPACE, "key": process_id},
            )
            healthy = bool(result.scalar())
        except Exception:
            healthy = False
    if healthy:
        with suppress(Exception):
            await connection.close()
            return
    with suppress(Exception):
        await connection.invalidate()


__all__ = [
    "SCHEDULER_LOCK_KEY",
    "SCHEDULER_LOCK_NAMESPACE",
    "STATE_BYPASS",
    "STATE_CANCELLED",
    "STATE_FAILED",
    "STATE_FILTERED",
    "STATE_QUEUED",
    "STATE_RUNNING",
    "STATE_SUCCESS",
    "GrantOutcome",
    "Ingress",
    "JevDecisionRepository",
    "OwnedWorkRow",
    "Reservation",
    "ReservationKind",
    "ReservePlan",
    "SchedulerObservation",
]
