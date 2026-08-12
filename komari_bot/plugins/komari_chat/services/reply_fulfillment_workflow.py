"""聊天回复履约统一工作流。

工作流统一拥有回复从准备、发送到全部送达后承诺完成的生命周期；
持久化只走新父子模型 adapter（``ReplyFulfillmentRepository``），
不再保留旧宽表兼容路径。发送能力以窄边界注入：先持久准备、再持久
登记发送开始，最后才调用平台发送；平台结果只以
``ReplyDeliveryResult`` 翻译后的送达事实进入领域状态机。

TSK-87 contract 后，送达后承诺推进、待处置续跑、终态清理与告警
全部复用本模块的统一边界：``fulfill`` 送达持久化后立即委托
``commitment_workflow.recover_fulfillment`` 并恢复告警；
``recover_pending`` 依次协调发送前恢复、批量承诺推进、告警恢复与
小时级终态清理。本模块不读取旧 outbox，不保留双读、双写或 fallback。
"""

from __future__ import annotations

import asyncio
import time as _system_time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from nonebot import logger

from ..reply_fulfillment_domain import (
    AssistantReplyHistoryPayload,
    FavorabilityAdjustmentPayload,
    InteractionHistoryPayload,
    ProactiveReplyConfirmationPayload,
    ReplyCommitmentInput,
    ReplyFulfillmentDraft,
    build_reply_fulfillment_id,
    build_reply_fulfillment_payload_hash,
)
from ..repositories.reply_fulfillment_repository import ReplyFulfillmentRepository
from .reply_commitment_workflow import ReplyCommitmentWorkflow
from .reply_delivery_onebot import ReplyDeliveryResult
from .reply_fulfillment_alert import (
    ReplyFulfillmentAlertBot,
    ReplyFulfillmentAlertService,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from komari_bot.plugins.komari_memory import MessageSchema

ReplySender = Callable[[object], Awaitable[object]]
BotIdentity = tuple[str, str]


class _MonotonicClock:
    """小时级清理决策的单调时钟 seam。

    独立包装 stdlib ``time.monotonic``：测试只替换本模块的
    ``time.monotonic`` 即可控制清理节奏，不会像直接 patch 全局
    ``time`` 模块那样泄漏到 asyncio 事件循环内部。
    """

    @staticmethod
    def monotonic() -> float:
        return _system_time.monotonic()


time = _MonotonicClock()


@dataclass(frozen=True)
class RecoveredReply:
    """从持久 NOT_STARTED 记录恢复的发送载荷（与 OneBot 边界对齐）。"""

    operation_id: str
    group_id: str
    reply: str
    reply_to_message_id: str | None
    source_message_id: str
    bot_self_id: str
    adapter_name: str
    proactive_reservation_id: str | None


class _PendingReply(Protocol):
    """工作流消费的待履约回复最小接口。"""

    @property
    def operation_id(self) -> str: ...

    @property
    def request_trace_id(self) -> str: ...

    @property
    def message(self) -> MessageSchema: ...

    @property
    def reply_result(self) -> Any: ...

    @property
    def reply(self) -> str: ...

    @property
    def reply_to_message_id(self) -> str: ...

    @property
    def bot_self_id(self) -> str: ...

    @property
    def adapter_name(self) -> str: ...

    @property
    def bot_nickname(self) -> str: ...

    @property
    def reply_timestamp(self) -> float: ...

    @property
    def proactive_reservation_id(self) -> str | None: ...

    @property
    def proactive_reservation(self) -> Any: ...


class _ReplyFulfillmentRepository(Protocol):
    """本工作流消费的父子履约仓库窄接口。"""

    async def has_active_operation(self, fulfillment_id: str) -> bool: ...

    async def prepare(self, draft: ReplyFulfillmentDraft) -> bool: ...

    async def mark_send_started(self, fulfillment_id: str) -> bool: ...

    async def mark_delivered(
        self,
        fulfillment_id: str,
        *,
        platform_message_id: str | None = None,
    ) -> bool: ...

    async def mark_not_delivered(self, fulfillment_id: str) -> bool: ...

    async def claim_fresh_not_started(
        self,
        *,
        bot_self_id: str,
        adapter_name: str,
        freshness_seconds: int,
        limit: int,
    ) -> list[dict[str, Any]]: ...

    async def expire_stale_not_started(
        self,
        *,
        freshness_seconds: int,
        limit: int,
    ) -> list[dict[str, Any]]: ...


class ReplyFulfillmentQueryProtocol(Protocol):
    """消息生成阶段使用的回复履约查询窄接口。"""

    async def is_duplicate_event(self, operation_id: str) -> bool: ...


def build_reply_fulfillment_commitments(
    *,
    group_id: str,
    user_id: str,
    bot_nickname: str,
    reply_content: str,
    reply_timestamp: float,
    trigger_message_id: str,
    display_name: str,
    favorability_delta: int,
    favorability_reason: str,
    interaction_history: dict[str, str],
    proactive_reservation_id: str | None,
    proactive_cooldown_seconds: int,
    global_interaction_enabled: bool,
    global_interaction_trigger_size: int,
) -> tuple[ReplyCommitmentInput, ...]:
    """按固定顺序冻结本次适用的承诺子集。

    主动回复确认只在存在生成期预占时适用；互动历史只在动态功能开启时
    适用；好感度调整与角色回复历史始终适用。不开放动态承诺注册。
    """
    commitments: list[ReplyCommitmentInput] = []
    if proactive_reservation_id is not None:
        commitments.append(
            ReplyCommitmentInput(
                commitment_type="proactive_reply_confirmation",
                payload=ProactiveReplyConfirmationPayload(
                    group_id=group_id,
                    reservation_id=proactive_reservation_id,
                    cooldown_seconds=proactive_cooldown_seconds,
                ),
            )
        )
    commitments.append(
        ReplyCommitmentInput(
            commitment_type="favorability_adjustment",
            payload=FavorabilityAdjustmentPayload(
                user_id=user_id,
                delta=favorability_delta,
                reason=favorability_reason,
            ),
        )
    )
    commitments.append(
        ReplyCommitmentInput(
            commitment_type="assistant_reply_history",
            payload=AssistantReplyHistoryPayload(
                group_id=group_id,
                bot_nickname=bot_nickname,
                reply_content=reply_content,
                reply_timestamp=reply_timestamp,
            ),
        )
    )
    if global_interaction_enabled:
        commitments.append(
            ReplyCommitmentInput(
                commitment_type="interaction_history",
                payload=InteractionHistoryPayload(
                    user_id=user_id,
                    display_name=display_name,
                    trigger_size=global_interaction_trigger_size,
                    reply_timestamp=reply_timestamp,
                    trigger_message_id=trigger_message_id,
                    record=dict(interaction_history),
                ),
            )
        )
    return tuple(commitments)


class ReplyFulfillmentWorkflow:
    """统一执行单条回复履约。

    ``commitment_workflow`` 与 ``alert_service`` 与调用方共享同一个
    ``ReplyFulfillmentRepository`` 实例（见 ``build_reply_fulfillment_workflow``），
    保证父子模型只有一份持久化 adapter，不保留旧宽表兼容路径。
    """

    def __init__(
        self,
        repository: _ReplyFulfillmentRepository,
        proactive_reservation: Any,
        config_getter: Callable[[], Any],
        recovery_senders_getter: Callable[[], Mapping[BotIdentity, ReplySender]],
        commitment_workflow: Any,
        alert_service: Any,
    ) -> None:
        self.repository = repository
        self.proactive_reservation = proactive_reservation
        self.config_getter = config_getter
        self.recovery_senders_getter = recovery_senders_getter
        self.commitment_workflow = commitment_workflow
        self.alert_service = alert_service
        self._owner_token = f"chat-{uuid.uuid4().hex}"
        self._last_cleanup = 0.0
        # 发送起始阶段进程内锁：直接路径从 prepare 前持有到
        # mark_send_started 完成，恢复路径在领取/过期阶段持有同一锁，
        # 阻止本进程 worker 在 prepare 与 mark 之间抢走刚插入的
        # NOT_STARTED 行；锁不跨平台发送，崩溃于 prepare 后 / mark 前
        # 仍保留可被恢复的可窗口。
        self._send_start_lock = asyncio.Lock()

    async def is_duplicate_event(self, operation_id: str) -> bool:
        """判断平台事件是否已有不可再次发送的履约记录。"""
        return await self.repository.has_active_operation(operation_id)

    @staticmethod
    def _resolve_display_name(message: MessageSchema) -> str:
        return str(message.user_nickname or message.user_id).strip() or message.user_id

    async def _prepare(self, pending_reply: _PendingReply) -> bool:
        """注入履约工作流配置后准备一条不可变履约 Draft。"""
        config = self.config_getter()
        favorability_delta = pending_reply.reply_result.favorability_delta
        if favorability_delta is None:
            msg = "favorability_delta missing"
            raise ValueError(msg)
        favorability_reason = pending_reply.reply_result.favorability_reason
        if not favorability_reason:
            msg = "favorability_reason missing"
            raise ValueError(msg)
        interaction_history = pending_reply.reply_result.interaction_history
        if interaction_history is None:
            msg = "interaction_history missing"
            raise ValueError(msg)
        interaction_record = {
            "event": str(interaction_history["event"]),
            "result": str(interaction_history["result"]),
            "emotion": str(interaction_history["emotion"]),
        }
        display_name = self._resolve_display_name(pending_reply.message)
        commitments = build_reply_fulfillment_commitments(
            group_id=pending_reply.message.group_id,
            user_id=pending_reply.message.user_id,
            bot_nickname=pending_reply.bot_nickname,
            reply_content=pending_reply.reply,
            reply_timestamp=pending_reply.reply_timestamp,
            trigger_message_id=pending_reply.message.message_id,
            display_name=display_name,
            favorability_delta=favorability_delta,
            favorability_reason=favorability_reason,
            interaction_history=interaction_record,
            proactive_reservation_id=pending_reply.proactive_reservation_id,
            proactive_cooldown_seconds=int(config.proactive_cooldown),
            global_interaction_enabled=bool(config.global_interaction_enabled),
            global_interaction_trigger_size=int(
                config.global_interaction_trigger_size
            ),
        )
        draft = ReplyFulfillmentDraft(
            fulfillment_id=pending_reply.operation_id,
            payload_hash=build_reply_fulfillment_payload_hash(
                fulfillment_id=pending_reply.operation_id,
                trigger_message_id=pending_reply.message.message_id,
                trigger_user_id=pending_reply.message.user_id,
                group_id=pending_reply.message.group_id,
                bot_self_id=pending_reply.bot_self_id,
                adapter_name=pending_reply.adapter_name,
                reply_target_message_id=pending_reply.reply_to_message_id,
                reply_content=pending_reply.reply,
                commitments=commitments,
            ),
            request_trace_id=pending_reply.request_trace_id,
            trigger_message_id=pending_reply.message.message_id,
            trigger_user_id=pending_reply.message.user_id,
            group_id=pending_reply.message.group_id,
            bot_self_id=pending_reply.bot_self_id,
            adapter_name=pending_reply.adapter_name,
            reply_target_message_id=pending_reply.reply_to_message_id,
            reply_content=pending_reply.reply,
            commitments=commitments,
        )
        return await self.repository.prepare(draft)

    async def _release_reservation(self, pending_reply: _PendingReply) -> None:
        reservation = pending_reply.proactive_reservation
        if reservation is None:
            return
        try:
            await reservation.release()
        except Exception:
            logger.exception(
                "[KomariChat] 主动回复预占释放失败，将等待 TTL 回收: group={}",
                pending_reply.message.group_id,
            )

    async def fulfill(
        self,
        pending_reply: _PendingReply,
        *,
        send_reply: Callable[[_PendingReply], Awaitable[object]],
    ) -> bool:
        """原子编排一次回复履约：先持久准备，再登记发送开始，最后发送。

        平台发送边界必须返回 ``ReplyDeliveryResult``，原始平台响应不进入
        领域状态机；已送达持久化后立即推进承诺并恢复告警；明确未送达
        终止并释放预占；待确认结果不释放预占、不自动重发、只恢复告警；
        发送开始后任何异常（含 ``CancelledError``）原样传播并保持待确认。
        """
        if not pending_reply.reply_result.content:
            await self._release_reservation(pending_reply)
            return False

        # 发送起始阶段持进程内锁：prepare 与 mark_send_started 之间不被
        # 本进程恢复 worker 领取；锁不跨平台发送。
        async with self._send_start_lock:
            try:
                prepared = await self._prepare(pending_reply)
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._release_reservation(pending_reply)
                raise

            if not prepared:
                logger.info(
                    "[KomariChat] 重复回复 operation 已存在，取消本次发送: operation={}",
                    pending_reply.operation_id,
                )
                await self._release_reservation(pending_reply)
                return False

            started = await self.repository.mark_send_started(
                pending_reply.operation_id
            )
            if not started:
                msg = "回复发送开始登记失败，未调用平台发送能力"
                raise RuntimeError(msg)

        try:
            delivery_result = await send_reply(pending_reply)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "[KomariChat] 发送开始后平台结果未知，保持待确认: operation={}",
                pending_reply.operation_id,
            )
            raise

        if not isinstance(delivery_result, ReplyDeliveryResult):
            msg = "平台发送边界必须返回 ReplyDeliveryResult"
            raise TypeError(msg)

        if delivery_result.state == "delivered":
            return await self._finish_delivered(
                pending_reply,
                platform_message_id=delivery_result.platform_message_id,
            )
        if delivery_result.state == "not_delivered":
            await self.repository.mark_not_delivered(pending_reply.operation_id)
            await self._release_reservation(pending_reply)
            logger.info(
                "[KomariChat] 平台明确拒绝发送，回复未送达: group={} operation={}",
                pending_reply.message.group_id,
                pending_reply.operation_id,
            )
            return False
        logger.info(
            "[KomariChat] 发送结果未知，回复进入待确认对账: operation={}",
            pending_reply.operation_id,
        )
        await self.alert_service.recover_alerts()
        return False

    async def _finish_delivered(
        self,
        pending_reply: _PendingReply,
        *,
        platform_message_id: str | None,
    ) -> bool:
        """已送达回复：持久化送达事实，立即推进承诺并恢复告警。"""
        delivered = await self.repository.mark_delivered(
            pending_reply.operation_id,
            platform_message_id=platform_message_id,
        )
        if not delivered:
            msg = "回复已发送，但履约无法标记为 DELIVERED"
            raise RuntimeError(msg)

        await self.commitment_workflow.recover_fulfillment(pending_reply.operation_id)
        await self.alert_service.recover_alerts()
        logger.info(
            "[KomariChat] 回复已送达并进入持久副作用提交: group={} operation={}",
            pending_reply.message.group_id,
            pending_reply.operation_id,
        )
        return True

    async def recover_pending(self) -> int:
        """恢复中断的回复履约：发送前恢复、承诺推进、告警与小时级清理。

        先恢复发送前中断的新鲜/过期履约，再批量推进已送达承诺，随后
        恢复两类告警；每小时执行一次终态两阶段清理。清理周期由进程内
        单调时钟控制，不读取任何持久状态。
        """
        completed = await self._recover_not_started_deliveries()
        completed += await self.commitment_workflow.recover_pending()
        await self.alert_service.recover_alerts()

        now = time.monotonic()
        if now - self._last_cleanup >= 3600:
            self._last_cleanup = now
            await self.commitment_workflow.cleanup_terminal_fulfillments()
        return completed

    async def _recover_not_started_deliveries(self) -> int:
        """恢复发送前中断：精确身份匹配的恢复发送与时效终止。

        只有仍在时效内、且 Bot 与适配器精确匹配的 NOT_STARTED 回复才
        允许恢复发送；满时效按未送达终止并释放持久预占。仓库未提供
        领取/过期能力（测试替身）时跳过本阶段。领取与过期在同一进程
        内锁内原子完成，避免与直接路径的 prepare → mark_send_started
        区间竞争；平台发送在锁外执行。
        """
        claim_fresh = getattr(self.repository, "claim_fresh_not_started", None)
        expire_stale = getattr(self.repository, "expire_stale_not_started", None)
        if claim_fresh is None or expire_stale is None:
            return 0
        config = self.config_getter()
        freshness_seconds = int(config.reply_fulfillment_freshness_seconds)
        limit = int(config.reply_fulfillment_batch_size)
        completed = 0
        claimed_records: list[tuple[dict[str, Any], ReplySender]] = []
        async with self._send_start_lock:
            for (bot_self_id, adapter_name), sender in (
                self.recovery_senders_getter().items()
            ):
                records = await claim_fresh(
                    bot_self_id=bot_self_id,
                    adapter_name=adapter_name,
                    freshness_seconds=freshness_seconds,
                    limit=limit,
                )
                claimed_records.extend((record, sender) for record in records)
            expired = await expire_stale(
                freshness_seconds=freshness_seconds,
                limit=limit,
            )
        for record, sender in claimed_records:
            if await self._finish_recovered(record, sender):
                completed += 1
        for record in expired:
            await self._terminate_expired(record)
        return completed

    async def _finish_recovered(
        self,
        record: dict[str, Any],
        sender: ReplySender,
    ) -> bool:
        """用恢复 sender 发送一条新鲜 NOT_STARTED 回复并翻译结果。

        待确认结果保持待确认且永不自动重发；明确未送达则终止并释放
        预占；已送达持久化后立即推进承诺；恢复发送本身异常时保守保持
        待确认。
        """
        operation_id = str(record["operation_id"])
        recovered_reply = self._recovered_reply(record)
        try:
            delivery_result = await sender(recovered_reply)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "[KomariChat] 恢复发送结果未知，保持待确认: operation={}",
                operation_id,
            )
            return False
        if not isinstance(delivery_result, ReplyDeliveryResult):
            msg = "平台发送边界必须返回 ReplyDeliveryResult"
            raise TypeError(msg)
        if delivery_result.state == "delivered":
            marked = await self.repository.mark_delivered(
                operation_id,
                platform_message_id=delivery_result.platform_message_id,
            )
            if not marked:
                logger.error(
                    "[KomariChat] 恢复发送后无法持久化送达事实: operation={}",
                    operation_id,
                )
                return False
            await self.commitment_workflow.recover_fulfillment(operation_id)
            return True
        if delivery_result.state == "not_delivered":
            await self.repository.mark_not_delivered(operation_id)
            await self._release_recovered_reservation(record)
            logger.info(
                "[KomariChat] 恢复发送被平台明确拒绝，回复未送达: operation={}",
                operation_id,
            )
            return False
        logger.info(
            "[KomariChat] 恢复发送结果未知，保持待确认对账: operation={}",
            operation_id,
        )
        return False

    async def _terminate_expired(self, record: dict[str, Any]) -> None:
        """满时效未发送的回复已由仓库转为未送达，只负责释放持久预占。"""
        await self._release_recovered_reservation(record)

    async def _release_recovered_reservation(
        self,
        record: dict[str, Any],
    ) -> None:
        """按持久化的群与预占 ID 幂等释放主动回复预占。"""
        reservation_id = record.get("proactive_reservation_id")
        if reservation_id is None:
            return
        try:
            await self.proactive_reservation.release(
                str(record["group_id"]),
                str(reservation_id),
            )
        except Exception:
            logger.exception(
                "[KomariChat] 恢复终止回复的主动预占释放失败，等待 TTL 回收: group={}",
                record.get("group_id"),
            )

    @staticmethod
    def _recovered_reply(record: dict[str, Any]) -> RecoveredReply:
        """把持久 NOT_STARTED 记录投影为恢复发送载荷。"""
        return RecoveredReply(
            operation_id=str(record["operation_id"]),
            group_id=str(record["group_id"]),
            reply=str(record["reply_content"]),
            reply_to_message_id=(
                str(record["reply_target_message_id"])
                if record.get("reply_target_message_id") is not None
                else None
            ),
            source_message_id=str(record["source_message_id"]),
            bot_self_id=str(record["bot_self_id"]),
            adapter_name=str(record["adapter_name"]),
            proactive_reservation_id=(
                str(record["proactive_reservation_id"])
                if record.get("proactive_reservation_id") is not None
                else None
            ),
        )


def build_reply_fulfillment_workflow(
    *,
    pg_pool: Any,
    redis: Any,
    proactive_reservation: Any,
    user_data: Any,
    config_getter: Callable[[], Any],
    recovery_senders_getter: Callable[[], Mapping[BotIdentity, ReplySender]],
    bots_provider: Callable[[], Iterable[object] | Mapping[object, object]],
    superusers_provider: Callable[[], Iterable[object]],
) -> ReplyFulfillmentWorkflow:
    """在 composition root 组装统一履约工作流。

    三个服务共享同一个 ``ReplyFulfillmentRepository`` 实例：父子模型
    只有一份持久化 adapter，不保留旧宽表、不双写、不 fallback。
    """
    repository = ReplyFulfillmentRepository(pg_pool)
    commitment_workflow = ReplyCommitmentWorkflow(
        repository=repository,
        redis=redis,
        proactive_reservation=proactive_reservation,
        user_data=user_data,
        config_getter=config_getter,
    )
    alert_service = ReplyFulfillmentAlertService(
        repository=repository,
        bots_provider=cast(
            "Callable[[], Iterable[ReplyFulfillmentAlertBot] | Mapping[object, ReplyFulfillmentAlertBot]]",
            bots_provider,
        ),
        superusers_provider=superusers_provider,
    )
    return ReplyFulfillmentWorkflow(
        repository=repository,
        proactive_reservation=proactive_reservation,
        config_getter=config_getter,
        recovery_senders_getter=recovery_senders_getter,
        commitment_workflow=commitment_workflow,
        alert_service=alert_service,
    )


__all__ = [
    "ReplyFulfillmentQueryProtocol",
    "ReplyFulfillmentWorkflow",
    "build_reply_fulfillment_id",
    "build_reply_fulfillment_workflow",
]
