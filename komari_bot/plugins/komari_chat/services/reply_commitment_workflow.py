"""送达后承诺执行器（独立兑现）。

TSK-82：回复确认送达后，在单一父租约内按固定顺序串行尝试冻结的
四项送达后承诺：主动回复确认、好感度调整、角色回复历史、互动历史。
单项失败独立保存尝试、退避、稳定错误码与完成时间，失败不阻塞其他
当前到期项；只有全部适用子项完成才完成父履约。

本模块当前只由验收测试直接构造，不接线生产入口；正常聊天继续只走
旧 outbox，禁止双写、双读或 fallback。TSK-87 之前不得提前 cutover，
也不清理 tombstone、不迁移载荷最小化之外的生命周期。
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Protocol

import asyncpg
from nonebot import logger
from redis.exceptions import RedisError

from komari_bot.plugins.komari_memory import MessageSchema

from ..reply_fulfillment_domain import (
    _COMMITMENT_PAYLOAD_TYPES,
    COMMITMENT_TYPES,
    ReplyFulfillmentConflictError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

# 承诺类型到冻结领域值对象的固定映射，与领域模块保持单一事实来源。
_COMMITMENT_ORDER = {
    commitment_type: index for index, commitment_type in enumerate(COMMITMENT_TYPES)
}


class _CommitmentExecutorRepository(Protocol):
    """执行器消费的父子履约仓库窄接口。"""

    async def claim_pending(
        self,
        *,
        owner_token: str,
        limit: int,
        lease_seconds: int,
    ) -> list[dict[str, Any]]: ...

    async def load_claimed_commitments(
        self,
        fulfillment_id: str,
        *,
        owner_token: str,
    ) -> list[dict[str, Any]] | None: ...

    async def renew_lease(
        self,
        fulfillment_id: str,
        *,
        owner_token: str,
        lease_seconds: int,
    ) -> bool: ...

    async def release_lease(
        self,
        fulfillment_id: str,
        *,
        owner_token: str,
    ) -> bool: ...

    async def mark_commitment_completed(
        self,
        fulfillment_id: str,
        *,
        commitment_type: str,
        owner_token: str,
    ) -> bool: ...

    async def mark_commitment_failed(
        self,
        fulfillment_id: str,
        *,
        commitment_type: str,
        owner_token: str,
        error_code: str,
        max_attempts: int,
        retry_base_seconds: int,
        retry_max_seconds: int,
    ) -> str | None: ...

    async def complete_fulfillment(
        self,
        fulfillment_id: str,
        *,
        owner_token: str,
    ) -> bool: ...


def _build_payload(commitment_type: str, raw: object) -> Any:
    """把持久载荷构造成冻结领域值对象（invalid_payload 的分类点）。

    在调用下游之前完成解析；解析失败属于载荷/JSON 字段校验错误，
    与下游调用抛出的 ``TypeError``（protocol_violation）严格区分。
    """
    payload_type = _COMMITMENT_PAYLOAD_TYPES.get(commitment_type)
    if payload_type is None or not isinstance(raw, dict):
        msg = "承诺载荷必须是冻结值对象可解析的 JSON 对象"
        raise TypeError(msg)
    return payload_type(**raw)


def _is_service_unavailable(error: BaseException) -> bool:
    """user_data 动态关闭、正在关闭或连接池未初始化。

    按 user_data 插件的公开异常类名识别，避免在模块导入期 require
    该插件；错误码仍只含稳定标识，不含异常正文。
    """
    error_type = type(error).__name__
    if error_type in {"UserDataDisabledError", "UserDataStoppingError"}:
        return True
    return isinstance(error, RuntimeError) and "连接池未初始化" in str(error)


def _is_idempotency_conflict(error: BaseException) -> bool:
    """好感度幂等账本冲突的公开异常文本；持久化只写稳定码。

    只对 user_data 已有的公开文本做窄匹配，普通未知 ``ValueError``
    不会被误判为幂等冲突。
    """
    return isinstance(error, ValueError) and "operation_id 与既有请求载荷冲突" in str(
        error
    )


def _classify_error(error: BaseException) -> tuple[bool, str]:
    """把异常翻译成稳定错误码；只持久化码本身，禁止异常正文。

    返回值形如 ``(永久, 错误码)``：永久错误立即耗尽（FAILED），
    瞬态错误按动态策略退避重试。
    """
    if isinstance(error, ReplyFulfillmentConflictError) or _is_idempotency_conflict(
        error
    ):
        permanent, code = True, "idempotency_conflict"
    elif isinstance(error, TypeError):
        permanent, code = True, "protocol_violation"
    elif isinstance(error, TimeoutError):
        permanent, code = False, "transient_timeout"
    elif isinstance(error, RedisError):
        permanent, code = False, "redis_unavailable"
    elif isinstance(error, (ConnectionError, OSError)):
        permanent, code = False, "connection_error"
    elif isinstance(
        error,
        (asyncpg.PostgresConnectionError, asyncpg.InterfaceError),
    ):
        permanent, code = False, "database_unavailable"
    elif _is_service_unavailable(error):
        permanent, code = False, "service_unavailable"
    else:
        permanent, code = False, "unexpected_error"
    return permanent, code


def _redis_dedupe_ttl(config: Any) -> int:
    """由冻结配置推导下游去重 TTL，不进入冻结载荷。"""
    return max(1, int(config.reply_commit_tombstone_retention_days) + 1) * 86_400


class ReplyCommitmentWorkflow:
    """在单一父租约内按固定顺序串行兑现送达后承诺。"""

    def __init__(
        self,
        repository: _CommitmentExecutorRepository,
        redis: Any,
        proactive_reservation: Any,
        user_data: Any,
        config_getter: Callable[[], Any],
    ) -> None:
        self.repository = repository
        self.redis = redis
        self.proactive_reservation = proactive_reservation
        self.user_data = user_data
        self.config_getter = config_getter
        self._owner_token = f"commit-{uuid.uuid4().hex}"

    async def recover_pending(self) -> int:
        """领取 DELIVERED 父项并兑现其当前到期承诺，返回完成父项数。

        每项履约在领取到的单一父租约内处理；租约丢失立即中止当前
        履约，由下一次领取恢复。不清理 tombstone。单父控制面异常被
        隔离为本轮失败，不炸停同批其他父履约。
        """
        config = self.config_getter()
        claimed = await self.repository.claim_pending(
            owner_token=self._owner_token,
            limit=int(config.reply_commit_batch_size),
            lease_seconds=int(config.reply_commit_lease_seconds),
        )
        completed = 0
        for record in claimed:
            if await self._finish_claimed(
                str(record["fulfillment_id"]),
                config,
            ):
                completed += 1
        return completed

    async def _finish_claimed(self, fulfillment_id: str, config: Any) -> bool:
        """在一轮父租约内兑现一个履约，返回是否完成父记录。

        领取时的配置快照用于本轮的领取/心跳租约；失败策略由
        ``_mark_failed`` 在记录失败时动态读取。取消原样传播；其他
        控制面异常（如完成标记数据库暂不可用）隔离为本轮失败并尽力
        释放仍归自己的父租约，不消费子项失败预算。
        """
        lease_seconds = max(1, int(config.reply_commit_lease_seconds))
        lease_lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat(
                fulfillment_id,
                lease_seconds=lease_seconds,
                lost=lease_lost,
            )
        )
        completed = False
        try:
            try:
                completed = await self._process_claimed(
                    fulfillment_id,
                    config,
                    lease_lost,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # 完成标记等控制面异常：外部结果可能已成功但标记未知，
                # 不调用 mark_commitment_failed，依赖幂等 ID 下轮恢复。
                logger.warning(
                    "[KomariChat] 履约兑现控制面异常，本轮按失败处理: "
                    "fulfillment={} error_type={}",
                    fulfillment_id,
                    type(error).__name__,
                )
                completed = False
        finally:
            await self._stop_task(heartbeat)
            if not completed:
                # 尽力释放仍归自己的父租约（心跳续租暂时失败但 CAS 仍
                # 有效时同样可回收）；真正丢失 owner 时 CAS 自然返回
                # False，不能等自然过期。
                with suppress(Exception):
                    await self.repository.release_lease(
                        fulfillment_id,
                        owner_token=self._owner_token,
                    )
        return completed

    async def _process_claimed(
        self,
        fulfillment_id: str,
        config: Any,
        lease_lost: asyncio.Event,
    ) -> bool:
        """按固定顺序处理当前到期子项，并在全部完成后门控父完成。"""
        rows = await self.repository.load_claimed_commitments(
            fulfillment_id,
            owner_token=self._owner_token,
        )
        if rows is None:
            # 父租约已不属于自己（丢失或被回收），立即中止本轮。
            return False
        for row in sorted(
            rows,
            key=lambda item: _COMMITMENT_ORDER.get(
                str(item["commitment_type"]), len(_COMMITMENT_ORDER)
            ),
        ):
            if lease_lost.is_set() or not await self._process_commitment_row(
                fulfillment_id,
                row,
                config,
                lease_lost,
            ):
                return False
        if lease_lost.is_set():
            return False
        return bool(
            await self.repository.complete_fulfillment(
                fulfillment_id,
                owner_token=self._owner_token,
            )
        )

    async def _process_commitment_row(
        self,
        fulfillment_id: str,
        row: dict[str, Any],
        config: Any,
        lease_lost: asyncio.Event,
    ) -> bool:
        """兑现单个当前到期承诺；返回 False 表示租约已丢失需中止。

        解析持久载荷失败记为 ``invalid_payload``（不调用下游）；下游
        异常按稳定错误码分类后独立记失败预算并继续其他当前到期项。
        外部调用后、任何 ``mark_commitment_*`` 之前检查心跳租约：已
        丢失则不落标记、不消费失败预算，外部结果由幂等 ID 下轮恢复。
        """
        commitment_type = str(row["commitment_type"])
        try:
            payload = _build_payload(commitment_type, row["payload"])
        except (TypeError, ValueError):
            if lease_lost.is_set():
                return False
            return await self._mark_failed(
                fulfillment_id,
                commitment_type,
                "invalid_payload",
                permanent=True,
            )
        try:
            await self._execute_commitment(
                fulfillment_id,
                commitment_type,
                payload,
                config,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if lease_lost.is_set():
                return False
            permanent, error_code = _classify_error(error)
            return await self._mark_failed(
                fulfillment_id,
                commitment_type,
                error_code,
                permanent=permanent,
            )
        if lease_lost.is_set():
            return False
        # CAS 失败视为租约丢失：不消费本项失败预算。
        return await self.repository.mark_commitment_completed(
            fulfillment_id,
            commitment_type=commitment_type,
            owner_token=self._owner_token,
        )

    async def _mark_failed(
        self,
        fulfillment_id: str,
        commitment_type: str,
        error_code: str,
        *,
        permanent: bool,
    ) -> bool:
        """记录单项失败并应用当时的动态策略；返回 False 表示租约已丢失。

        每次记录失败时重新读取配置，让 max attempts、退避基数/上限在
        下一次失败时动态生效，不沿用领取时快照、不进入冻结载荷。
        永久错误以 max_attempts=1 直接耗尽。
        """
        config = self.config_getter()
        state = await self.repository.mark_commitment_failed(
            fulfillment_id,
            commitment_type=commitment_type,
            owner_token=self._owner_token,
            error_code=error_code,
            max_attempts=(
                1 if permanent else max(1, int(config.reply_commit_max_attempts))
            ),
            retry_base_seconds=max(1, int(config.reply_commit_retry_base_seconds)),
            retry_max_seconds=max(
                1,
                int(getattr(config, "reply_fulfillment_retry_max_seconds", 3600)),
            ),
        )
        return state is not None

    async def _execute_commitment(
        self,
        fulfillment_id: str,
        commitment_type: str,
        payload: Any,
        config: Any,
    ) -> None:
        """串行调用对应下游承诺能力；外部副作用依赖既有幂等 ID 恢复。"""
        if commitment_type == "proactive_reply_confirmation":
            await self.proactive_reservation.confirm(
                payload.group_id,
                payload.reservation_id,
                cooldown_seconds=int(payload.cooldown_seconds),
            )
            return
        if commitment_type == "favorability_adjustment":
            await self.user_data.adjust_user_favorability(
                payload.user_id,
                int(payload.delta),
                operation_id=f"{fulfillment_id}:favorability",
            )
            return
        if commitment_type == "assistant_reply_history":
            bot_message = MessageSchema(
                user_id="bot",
                user_nickname=payload.bot_nickname,
                group_id=payload.group_id,
                content=payload.reply_content,
                timestamp=payload.reply_timestamp,
                message_id=f"bot_{fulfillment_id[-32:]}",
                is_bot=True,
            )
            await self.redis.push_message_once(
                payload.group_id,
                bot_message,
                operation_id=fulfillment_id,
                dedupe_ttl_seconds=_redis_dedupe_ttl(config),
            )
            return
        if commitment_type == "interaction_history":
            interaction_record: dict[str, object] = {
                "version": 1,
                **dict(payload.record),
                "display_name": payload.display_name,
                "timestamp": payload.reply_timestamp,
                "message_id": payload.trigger_message_id,
            }
            await self.redis.push_global_interaction_once(
                user_id=payload.user_id,
                record=interaction_record,
                trigger_size=int(payload.trigger_size),
                operation_id=fulfillment_id,
                dedupe_ttl_seconds=_redis_dedupe_ttl(config),
            )
            return
        msg = f"不支持的承诺类型: {commitment_type!r}"
        raise TypeError(msg)

    async def _heartbeat(
        self,
        fulfillment_id: str,
        *,
        lease_seconds: int,
        lost: asyncio.Event,
    ) -> None:
        """处理送达后承诺期间的父级租约续期；失败即视为租约丢失。"""
        interval = max(1.0, lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self.repository.renew_lease(
                    fulfillment_id,
                    owner_token=self._owner_token,
                    lease_seconds=lease_seconds,
                )
            except Exception:
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


__all__ = ["ReplyCommitmentWorkflow"]
