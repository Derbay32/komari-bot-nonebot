"""聊天回复履约工作流。

工作流统一拥有回复从准备、发送到送达后承诺完成的生命周期；旧宽表仓库只
作为本模块内部的持久化 adapter 使用。发送能力以窄边界注入：先持久准备、
再持久登记发送开始，最后才调用平台发送；平台结果只以
``ReplyDeliveryResult`` 翻译后的送达事实进入领域状态机。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from nonebot import logger

from komari_bot.plugins.komari_memory import MessageSchema

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
from ..repositories.reply_commit_repository import (
    PendingReplyCommit,
    ReplyCommitRepository,
    ReplyCommitStep,
)
from .reply_delivery_onebot import ReplyDeliveryResult

if TYPE_CHECKING:
    from collections.abc import Mapping

ReplySender = Callable[[object], Awaitable[object]]
BotIdentity = tuple[str, str]


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
    async def has_active_operation(self, operation_id: str) -> bool: ...

    async def prepare(self, draft: ReplyFulfillmentDraft) -> bool: ...

    async def cancel_prepared(self, operation_id: str) -> bool: ...

    async def mark_send_started(self, operation_id: str) -> bool: ...

    async def mark_delivered(
        self,
        operation_id: str,
        *,
        platform_message_id: str | None = None,
    ) -> bool: ...

    async def mark_not_delivered(self, operation_id: str) -> bool: ...

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

    async def claim_operation(
        self,
        operation_id: str,
        *,
        owner_token: str,
        lease_seconds: int,
    ) -> dict[str, Any] | None: ...

    async def claim_pending(
        self,
        *,
        owner_token: str,
        limit: int,
        lease_seconds: int,
    ) -> list[dict[str, Any]]: ...

    async def renew_lease(
        self,
        operation_id: str,
        *,
        owner_token: str,
        lease_seconds: int,
    ) -> bool: ...

    async def mark_step(
        self,
        operation_id: str,
        *,
        owner_token: str,
        step: ReplyCommitStep,
    ) -> bool: ...

    async def complete(self, operation_id: str, *, owner_token: str) -> bool: ...

    async def mark_failure(
        self,
        operation_id: str,
        *,
        owner_token: str,
        error_code: str,
        max_attempts: int,
        retry_base_seconds: int,
    ) -> str | None: ...

    async def cleanup_tombstones(self, *, retention_days: int) -> int: ...


class ReplyFulfillmentQueryProtocol(Protocol):
    """消息生成阶段使用的回复履约查询窄接口。"""

    async def is_duplicate_event(self, operation_id: str) -> bool: ...


class _LegacyReplyFulfillmentRepository:
    """contract 前把冻结 Draft 适配到旧宽表。"""

    def __init__(self, repository: ReplyCommitRepository) -> None:
        self.repository = repository

    @staticmethod
    def _to_legacy_payload(draft: ReplyFulfillmentDraft) -> PendingReplyCommit:
        payloads = {
            item.commitment_type: item.payload for item in draft.commitments
        }
        favorability = payloads.get("favorability_adjustment")
        assistant_history = payloads.get("assistant_reply_history")
        if not isinstance(
            favorability, FavorabilityAdjustmentPayload
        ) or not isinstance(assistant_history, AssistantReplyHistoryPayload):
            msg = "回复履约缺少固定的好感度或角色回复历史承诺"
            raise TypeError(msg)

        proactive = payloads.get("proactive_reply_confirmation")
        interaction = payloads.get("interaction_history")
        return PendingReplyCommit(
            operation_id=draft.fulfillment_id,
            request_trace_id=draft.request_trace_id,
            source_message_id=draft.trigger_message_id,
            group_id=draft.group_id,
            user_id=draft.trigger_user_id,
            user_nickname=(
                interaction.display_name
                if isinstance(interaction, InteractionHistoryPayload)
                else draft.trigger_user_id
            ),
            bot_nickname=assistant_history.bot_nickname,
            reply_content=draft.reply_content,
            reply_timestamp=assistant_history.reply_timestamp,
            favorability_delta=favorability.delta,
            favorability_reason=favorability.reason,
            interaction_history=(
                dict(interaction.record)
                if isinstance(interaction, InteractionHistoryPayload)
                else {}
            ),
            proactive_reservation_id=(
                proactive.reservation_id
                if isinstance(proactive, ProactiveReplyConfirmationPayload)
                else None
            ),
            proactive_cooldown_seconds=(
                proactive.cooldown_seconds
                if isinstance(proactive, ProactiveReplyConfirmationPayload)
                else 0
            ),
            global_interaction_enabled=isinstance(
                interaction, InteractionHistoryPayload
            ),
            global_interaction_trigger_size=(
                interaction.trigger_size
                if isinstance(interaction, InteractionHistoryPayload)
                else 1
            ),
            bot_self_id=draft.bot_self_id,
            adapter_name=draft.adapter_name,
            reply_target_message_id=draft.reply_target_message_id,
            frozen_payload_hash=draft.payload_hash,
        )

    async def has_active_operation(self, operation_id: str) -> bool:
        return await self.repository.has_active_operation(operation_id)

    async def prepare(self, draft: ReplyFulfillmentDraft) -> bool:
        return await self.repository.prepare(self._to_legacy_payload(draft))

    async def cancel_prepared(self, operation_id: str) -> bool:
        return await self.repository.cancel_prepared(operation_id)

    async def mark_send_started(self, operation_id: str) -> bool:
        return await self.repository.mark_send_started(operation_id)

    async def mark_delivered(
        self,
        operation_id: str,
        *,
        platform_message_id: str | None = None,
    ) -> bool:
        return await self.repository.mark_delivered(
            operation_id,
            platform_message_id=platform_message_id,
        )

    async def mark_not_delivered(self, operation_id: str) -> bool:
        return await self.repository.mark_not_delivered(operation_id)

    async def claim_fresh_not_started(
        self,
        *,
        bot_self_id: str,
        adapter_name: str,
        freshness_seconds: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        return await self.repository.claim_fresh_not_started(
            bot_self_id=bot_self_id,
            adapter_name=adapter_name,
            freshness_seconds=freshness_seconds,
            limit=limit,
        )

    async def expire_stale_not_started(
        self,
        *,
        freshness_seconds: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        return await self.repository.expire_stale_not_started(
            freshness_seconds=freshness_seconds,
            limit=limit,
        )

    async def claim_operation(
        self,
        operation_id: str,
        *,
        owner_token: str,
        lease_seconds: int,
    ) -> dict[str, Any] | None:
        return await self.repository.claim_operation(
            operation_id,
            owner_token=owner_token,
            lease_seconds=lease_seconds,
        )

    async def claim_pending(
        self,
        *,
        owner_token: str,
        limit: int,
        lease_seconds: int,
    ) -> list[dict[str, Any]]:
        return await self.repository.claim_pending(
            owner_token=owner_token,
            limit=limit,
            lease_seconds=lease_seconds,
        )

    async def renew_lease(
        self,
        operation_id: str,
        *,
        owner_token: str,
        lease_seconds: int,
    ) -> bool:
        return await self.repository.renew_lease(
            operation_id,
            owner_token=owner_token,
            lease_seconds=lease_seconds,
        )

    async def mark_step(
        self,
        operation_id: str,
        *,
        owner_token: str,
        step: ReplyCommitStep,
    ) -> bool:
        return await self.repository.mark_step(
            operation_id,
            owner_token=owner_token,
            step=step,
        )

    async def complete(self, operation_id: str, *, owner_token: str) -> bool:
        return await self.repository.complete(operation_id, owner_token=owner_token)

    async def mark_failure(
        self,
        operation_id: str,
        *,
        owner_token: str,
        error_code: str,
        max_attempts: int,
        retry_base_seconds: int,
    ) -> str | None:
        return await self.repository.mark_failure(
            operation_id,
            owner_token=owner_token,
            error_code=error_code,
            max_attempts=max_attempts,
            retry_base_seconds=retry_base_seconds,
        )

    async def cleanup_tombstones(self, *, retention_days: int) -> int:
        return await self.repository.cleanup_tombstones(
            retention_days=retention_days
        )


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
    """统一执行单条回复履约。"""

    def __init__(
        self,
        repository: _ReplyFulfillmentRepository,
        redis: Any,
        proactive_reservation: Any,
        user_data: Any,
        config_getter: Callable[[], Any],
        recovery_senders_getter: Callable[[], Mapping[BotIdentity, ReplySender]],
    ) -> None:
        self.repository = repository
        self.redis = redis
        self.proactive_reservation = proactive_reservation
        self.user_data = user_data
        self.config_getter = config_getter
        self.recovery_senders_getter = recovery_senders_getter
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
        领域状态机；待确认结果不释放预占、不自动重发；发送开始后任何
        异常（含 ``CancelledError``）原样传播并保持待确认。
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
            return await self._mark_delivered_and_finish(
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
        return False

    async def _mark_delivered_and_finish(
        self,
        pending_reply: _PendingReply,
        *,
        platform_message_id: str | None,
    ) -> bool:
        """已送达回复：持久化送达事实并继续提交送达后承诺。"""
        delivered = await self.repository.mark_delivered(
            pending_reply.operation_id,
            platform_message_id=platform_message_id,
        )
        if not delivered:
            msg = "回复已发送，但 outbox 无法标记为 DELIVERED"
            raise RuntimeError(msg)

        record = await self.repository.claim_operation(
            pending_reply.operation_id,
            owner_token=self._owner_token,
            lease_seconds=int(self.config_getter().reply_commit_lease_seconds),
        )
        if record is not None:
            await self._finish_claimed(record)
        logger.info(
            "[KomariChat] 回复已送达并进入持久副作用提交: group={} operation={}",
            pending_reply.message.group_id,
            pending_reply.operation_id,
        )
        return True

    async def _heartbeat(
        self,
        operation_id: str,
        *,
        lease_seconds: int,
        lost: asyncio.Event,
    ) -> None:
        """处理送达后承诺期间的父级租约续期。"""
        interval = max(1.0, lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self.repository.renew_lease(
                    operation_id,
                    owner_token=self._owner_token,
                    lease_seconds=lease_seconds,
                )
            except Exception:
                logger.exception("[KomariChat] 回复 outbox 租约续期失败")
                lost.set()
                return
            if not renewed:
                lost.set()
                return

    @staticmethod
    async def _stop_task(task: asyncio.Task[None] | None) -> None:
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _mark_step(
        self,
        operation_id: str,
        *,
        step: ReplyCommitStep,
        lease_lost: asyncio.Event,
    ) -> None:
        if lease_lost.is_set():
            msg = "回复 outbox 处理租约已丢失"
            raise RuntimeError(msg)
        marked = await self.repository.mark_step(
            operation_id,
            owner_token=self._owner_token,
            step=step,
        )
        if not marked:
            msg = "回复 outbox 子步骤确认失败，租约可能已丢失"
            raise RuntimeError(msg)

    @staticmethod
    def _parse_interaction_history(value: object) -> dict[str, str]:
        decoded = json.loads(value) if isinstance(value, str) else value
        if not isinstance(decoded, dict):
            msg = "回复 outbox interaction_history 不是对象"
            raise TypeError(msg)
        return {
            "event": str(decoded.get("event", "")).strip(),
            "result": str(decoded.get("result", "")).strip(),
            "emotion": str(decoded.get("emotion", "")).strip(),
        }

    async def _process_claimed(self, record: dict[str, Any]) -> None:
        """按固定顺序幂等执行四项送达后承诺。"""
        operation_id = str(record["operation_id"])
        config = self.config_getter()
        lease_seconds = int(config.reply_commit_lease_seconds)
        lease_lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat(
                operation_id,
                lease_seconds=lease_seconds,
                lost=lease_lost,
            )
        )
        redis_dedupe_ttl_seconds = (
            max(1, int(config.reply_commit_tombstone_retention_days) + 1) * 86_400
        )
        try:
            if record.get("proactive_confirmed_at") is None:
                reservation_id = record.get("proactive_reservation_id")
                if reservation_id is not None:
                    await self.proactive_reservation.confirm(
                        str(record["group_id"]),
                        str(reservation_id),
                        cooldown_seconds=int(record["proactive_cooldown_seconds"]),
                    )
                await self._mark_step(
                    operation_id,
                    step="proactive_confirmed",
                    lease_lost=lease_lost,
                )

            if record.get("favorability_applied_at") is None:
                await self.user_data.adjust_user_favorability(
                    str(record["user_id"]),
                    int(record["favorability_delta"]),
                    operation_id=f"{operation_id}:favorability",
                )
                await self._mark_step(
                    operation_id,
                    step="favorability_applied",
                    lease_lost=lease_lost,
                )

            if record.get("ai_history_stored_at") is None:
                reply_content = record.get("reply_content")
                bot_nickname = record.get("bot_nickname")
                if not isinstance(reply_content, str) or not isinstance(
                    bot_nickname, str
                ):
                    msg = "回复 outbox AI 历史载荷缺失"
                    raise ValueError(msg)
                bot_message = MessageSchema(
                    user_id="bot",
                    user_nickname=bot_nickname,
                    group_id=str(record["group_id"]),
                    content=reply_content,
                    timestamp=float(record["reply_timestamp"]),
                    message_id=f"bot_{operation_id[-32:]}",
                    is_bot=True,
                )
                await self.redis.push_message_once(
                    bot_message.group_id,
                    bot_message,
                    operation_id=operation_id,
                    dedupe_ttl_seconds=redis_dedupe_ttl_seconds,
                )
                await self._mark_step(
                    operation_id,
                    step="ai_history_stored",
                    lease_lost=lease_lost,
                )

            if record.get("interaction_stored_at") is None:
                if bool(record["global_interaction_enabled"]):
                    history = self._parse_interaction_history(
                        record.get("interaction_history")
                    )
                    global_record: dict[str, object] = {
                        "version": 1,
                        **history,
                        "display_name": str(
                            record.get("user_nickname") or record["user_id"]
                        ),
                        "timestamp": float(record["reply_timestamp"]),
                        "message_id": str(record["source_message_id"]),
                    }
                    await self.redis.push_global_interaction_once(
                        user_id=str(record["user_id"]),
                        record=global_record,
                        trigger_size=int(record["global_interaction_trigger_size"]),
                        operation_id=operation_id,
                        dedupe_ttl_seconds=redis_dedupe_ttl_seconds,
                    )
                await self._mark_step(
                    operation_id,
                    step="interaction_stored",
                    lease_lost=lease_lost,
                )

            if lease_lost.is_set():
                msg = "回复 outbox 完成前租约已丢失"
                raise RuntimeError(msg)
            completed = await self.repository.complete(
                operation_id,
                owner_token=self._owner_token,
            )
            if not completed:
                msg = "回复 outbox 未满足完成条件或租约已丢失"
                raise RuntimeError(msg)
        finally:
            await self._stop_task(heartbeat)

    async def _finish_claimed(self, record: dict[str, Any]) -> bool:
        """失败时保存无正文错误码并交给 adapter 计算退避。"""
        operation_id = str(record["operation_id"])
        try:
            await self._process_claimed(record)
        except Exception as error:
            config = self.config_getter()
            status = await self.repository.mark_failure(
                operation_id,
                owner_token=self._owner_token,
                error_code=type(error).__name__,
                max_attempts=int(config.reply_commit_max_attempts),
                retry_base_seconds=int(config.reply_commit_retry_base_seconds),
            )
            if status == "FAILED":
                logger.error(
                    "[KomariChat] 回复 outbox 已耗尽重试，保留待人工对账: operation={}",
                    operation_id,
                )
            else:
                logger.warning(
                    "[KomariChat] 回复 outbox 提交失败，已安排重试: operation={} error_type={}",
                    operation_id,
                    type(error).__name__,
                )
            return False
        return True

    async def recover_pending(self) -> int:
        """恢复中断的回复履约：发送前恢复与已送达承诺续跑。"""
        config = self.config_getter()
        records = await self.repository.claim_pending(
            owner_token=self._owner_token,
            limit=int(config.reply_commit_batch_size),
            lease_seconds=int(config.reply_commit_lease_seconds),
        )
        completed = await self._recover_not_started_deliveries()
        for record in records:
            if await self._finish_claimed(record):
                completed += 1

        now = time.monotonic()
        if now - self._last_cleanup >= 3600:
            self._last_cleanup = now
            retention_days = int(config.reply_commit_tombstone_retention_days)
            await self.repository.cleanup_tombstones(retention_days=retention_days)
            cleanup = getattr(self.user_data, "cleanup_favorability_operations", None)
            if callable(cleanup):
                await cast("Callable[..., Awaitable[object]]", cleanup)(
                    retention_days=retention_days
                )
        return completed

    async def _recover_not_started_deliveries(self) -> int:
        """恢复发送前中断：精确身份匹配的恢复发送与时效终止。

        只有仍在时效内、且 Bot 与适配器精确匹配的 NOT_STARTED 回复才
        允许恢复发送；满时效按未送达终止并释放持久预占。仓库未提供
        领取/过期能力（旧 adapter 或测试替身）时跳过本阶段。领取与
        过期在同一进程内锁内原子完成，避免与直接路径的
        prepare → mark_send_started 区间竞争；平台发送在锁外执行。
        """
        claim_fresh = getattr(self.repository, "claim_fresh_not_started", None)
        expire_stale = getattr(self.repository, "expire_stale_not_started", None)
        if claim_fresh is None or expire_stale is None:
            return 0
        config = self.config_getter()
        freshness_seconds = int(config.reply_fulfillment_freshness_seconds)
        limit = int(config.reply_commit_batch_size)
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
        预占；恢复发送本身异常时保守保持待确认。
        """
        operation_id = str(record["operation_id"])
        try:
            delivery_result = await sender(self._recovered_reply(record))
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
            claimed = await self.repository.claim_operation(
                operation_id,
                owner_token=self._owner_token,
                lease_seconds=int(self.config_getter().reply_commit_lease_seconds),
            )
            if claimed is not None:
                return await self._finish_claimed(claimed)
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
) -> ReplyFulfillmentWorkflow:
    """在 composition root 创建工作流并隐藏旧宽表 adapter。"""
    return ReplyFulfillmentWorkflow(
        repository=_LegacyReplyFulfillmentRepository(ReplyCommitRepository(pg_pool)),
        redis=redis,
        proactive_reservation=proactive_reservation,
        user_data=user_data,
        config_getter=config_getter,
        recovery_senders_getter=recovery_senders_getter,
    )


__all__ = [
    "ReplyFulfillmentQueryProtocol",
    "ReplyFulfillmentWorkflow",
    "build_reply_fulfillment_id",
    "build_reply_fulfillment_workflow",
]
