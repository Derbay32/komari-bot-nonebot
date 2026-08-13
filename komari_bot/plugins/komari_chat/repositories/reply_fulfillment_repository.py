"""回复履约父子存储 adapter。

本模块只提供 TSK-79 的持久化边界，正常聊天路径仍由旧版回复提交
仓库负责。冻结领域值对象与纯构建函数统一复用内部
``reply_fulfillment_domain``；承诺载荷在进入 JSONB 前必须经过固定
值对象校验与序列化。
"""

from __future__ import annotations

import json
from contextlib import suppress
from typing import Any

from ..reply_fulfillment_domain import (
    _COMMITMENT_PAYLOAD_TYPES,
    COMMITMENT_TYPES,
    REPLY_FULFILLMENT_STATUSES,
    AssistantReplyHistoryPayload,
    CommitmentPayload,
    FavorabilityAdjustmentPayload,
    InteractionHistoryPayload,
    ProactiveReplyConfirmationPayload,
    ReplyCommitmentInput,
    ReplyFulfillmentConflictError,
    ReplyFulfillmentDraft,
    build_reply_fulfillment_status_case_sql,
    derive_reply_fulfillment_status,
)

# 承诺类型到冻结领域值对象的固定映射，与领域模块保持单一事实来源；
# 承诺固定顺序的唯一权威定义，回复承诺工作流引用同一映射。
_COMMITMENT_ORDER = {
    commitment_type: index for index, commitment_type in enumerate(COMMITMENT_TYPES)
}


class ReplyFulfillmentRepository:
    """回复履约父子记录的 PostgreSQL adapter。"""

    def __init__(self, pg_pool: Any) -> None:
        self.pg_pool = pg_pool

    async def prepare(self, draft: ReplyFulfillmentDraft) -> bool:
        """在一个事务中插入一个父记录及其固定适用子项。"""
        async with self.pg_pool.acquire() as connection, connection.transaction():
            inserted = await connection.fetchval(
                """
                INSERT INTO komari_chat_reply_fulfillments (
                    fulfillment_id,
                    payload_hash,
                    request_trace_id,
                    trigger_message_id,
                    trigger_user_id,
                    group_id,
                    bot_self_id,
                    adapter_name,
                    reply_target_message_id,
                    reply_content,
                    delivery_state
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                    'NOT_STARTED'
                )
                ON CONFLICT (fulfillment_id) DO NOTHING
                RETURNING fulfillment_id
                """,
                draft.fulfillment_id,
                draft.payload_hash,
                draft.request_trace_id,
                draft.trigger_message_id,
                draft.trigger_user_id,
                draft.group_id,
                draft.bot_self_id,
                draft.adapter_name,
                draft.reply_target_message_id,
                draft.reply_content,
            )
            if inserted is None:
                existing_hash = await connection.fetchval(
                    """
                    SELECT payload_hash
                    FROM komari_chat_reply_fulfillments
                    WHERE fulfillment_id = $1
                    FOR SHARE
                    """,
                    draft.fulfillment_id,
                )
                if existing_hash is None:
                    msg = "回复履约准备后未读取到持久身份"
                    raise RuntimeError(msg)
                if str(existing_hash) != draft.payload_hash:
                    msg = f"履约冲突: {draft.fulfillment_id}"
                    raise ReplyFulfillmentConflictError(msg)
                return False

            await connection.executemany(
                """
                INSERT INTO komari_chat_reply_fulfillment_commitments (
                    fulfillment_id,
                    commitment_type,
                    payload
                )
                VALUES ($1, $2, $3::jsonb)
                """,
                [
                    (
                        draft.fulfillment_id,
                        commitment.commitment_type,
                        json.dumps(
                            commitment.to_json(),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    )
                    for commitment in draft.commitments
                ],
            )
        return True

    async def mark_send_started(self, fulfillment_id: str) -> bool:
        """把回复从未发送推进到待确认送达。"""
        async with self.pg_pool.acquire() as connection:
            changed = await connection.fetchval(
                """
                UPDATE komari_chat_reply_fulfillments
                SET delivery_state = 'PENDING_CONFIRMATION',
                    send_started_at = COALESCE(send_started_at, NOW()),
                    updated_at = NOW()
                WHERE fulfillment_id = $1
                  AND delivery_state = 'NOT_STARTED'
                RETURNING fulfillment_id
                """,
                fulfillment_id,
            )
        return changed is not None

    async def mark_delivered(
        self,
        fulfillment_id: str,
        *,
        platform_message_id: str | None = None,
    ) -> bool:
        """把待确认送达的回复推进为已送达。

        同一平台消息 ID 可重复确认（幂等）；冲突平台消息 ID 明确抛错，
        不得覆盖既有送达事实。进入已送达的同时立即清除父
        ``reply_content``——完整正文只在准备/发送阶段需要，送达后各
        承诺继续消费各自冻结的子 payload。
        """
        outcome = await self._transition_to_delivered(
            fulfillment_id,
            platform_message_id=platform_message_id,
        )
        if outcome == "platform_message_conflict":
            msg = "平台消息 ID 冲突"
            raise ValueError(msg)
        return outcome in ("updated", "idempotent")

    async def mark_not_delivered(self, fulfillment_id: str) -> bool:
        """明确失败或时效终止：回复进入未送达互斥终态。

        允许从未发送直接进入未送达（发送开始前过期），此时
        ``send_started_at`` 保持 NULL。进入终态即清除父
        ``reply_content`` 与全部子 payload（未送达不产生任何送达后
        承诺），但保留子状态事实行，不物理删子行。
        """
        outcome, _, _ = await self._transition_to_not_delivered(
            fulfillment_id,
            entry_states=("NOT_STARTED", "PENDING_CONFIRMATION"),
        )
        return outcome == "updated"

    async def has_active_operation(self, fulfillment_id: str) -> bool:
        """履约身份一旦持久化，就阻止同一事件再次发送。"""
        async with self.pg_pool.acquire() as connection:
            found = await connection.fetchval(
                """
                SELECT 1
                FROM komari_chat_reply_fulfillments
                WHERE fulfillment_id = $1
                """,
                fulfillment_id,
            )
        return found is not None

    async def _project_proactive_reservation_identities(
        self,
        connection: Any,
        fulfillment_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        """在同一事务内从子 payload 投影预占身份（与对账路径同名键）。

        只取主动回复确认承诺子项的 ``group_id`` / ``reservation_id``；
        无该子项或 payload 为 NULL 的行不出现，调用方按缺失补 None。
        """
        if not fulfillment_ids:
            return {}
        rows = await connection.fetch(
            """
            SELECT fulfillment_id,
                   payload->>'group_id' AS proactive_group_id,
                   payload->>'reservation_id' AS proactive_reservation_id
            FROM komari_chat_reply_fulfillment_commitments
            WHERE fulfillment_id = ANY($1::text[])
              AND commitment_type = 'proactive_reply_confirmation'
            """,
            fulfillment_ids,
        )
        return {str(row["fulfillment_id"]): dict(row) for row in rows}

    async def claim_fresh_not_started(
        self,
        *,
        bot_self_id: str,
        adapter_name: str,
        freshness_seconds: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        """原子领取原 Bot 身份匹配、仍在时效内的未发送履约。

        只有 ``bot_self_id`` 与 ``adapter_name`` 精确匹配且
        ``prepared_at`` 距今未满时效的 NOT_STARTED 行才会被领取；
        领取即登记发送开始（``PENDING_CONFIRMATION``）。同一事务内对
        领取到的行从主动回复确认承诺子 payload 投影预占身份，随返回行
        带出 ``proactive_group_id`` / ``proactive_reservation_id`` 两键。
        """
        if limit <= 0:
            return []
        async with self.pg_pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                """
                WITH candidates AS (
                    SELECT fulfillment_id
                    FROM komari_chat_reply_fulfillments
                    WHERE delivery_state = 'NOT_STARTED'
                      AND bot_self_id = $1
                      AND adapter_name = $2
                      AND prepared_at > NOW() - ($3 * INTERVAL '1 second')
                    ORDER BY prepared_at, fulfillment_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT $4
                )
                UPDATE komari_chat_reply_fulfillments AS parent
                SET delivery_state = 'PENDING_CONFIRMATION',
                    send_started_at = COALESCE(send_started_at, NOW()),
                    updated_at = NOW()
                FROM candidates
                WHERE parent.fulfillment_id = candidates.fulfillment_id
                RETURNING parent.*
                """,
                bot_self_id,
                adapter_name,
                max(1, freshness_seconds),
                limit,
            )
            projection = await self._project_proactive_reservation_identities(
                connection,
                [row["fulfillment_id"] for row in rows],
            )
        claimed: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["proactive_group_id"] = (
                projection.get(str(row["fulfillment_id"]), {}).get(
                    "proactive_group_id"
                )
            )
            item["proactive_reservation_id"] = (
                projection.get(str(row["fulfillment_id"]), {}).get(
                    "proactive_reservation_id"
                )
            )
            claimed.append(item)
        return claimed

    async def expire_stale_not_started(
        self,
        *,
        freshness_seconds: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        """终止超过时效仍未发送的履约，返回被终止行供调用方释放预占。

        满时效（``prepared_at`` 距今 >= 时效）的 NOT_STARTED 行转为
        未送达终态，``send_started_at`` 保持 NULL；未送达回复永不重发。
        同一事务内按两段式顺序（与管理对账路径 ``reconcile_not_delivered``
        一致）：UPDATE 父表后、清除子 payload 前先取出预占身份投影，
        再执行子 payload 清除——超时分支同样进入未送达终态，不得长期
        保留冻结敏感内容；子状态事实行保留（state 保持 PENDING），
        返回行带出父身份与 ``proactive_group_id`` /
        ``proactive_reservation_id`` 投影键。
        """
        if limit <= 0:
            return []
        async with self.pg_pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                """
                WITH candidates AS (
                    SELECT fulfillment_id
                    FROM komari_chat_reply_fulfillments
                    WHERE delivery_state = 'NOT_STARTED'
                      AND prepared_at <= NOW() - ($1 * INTERVAL '1 second')
                    ORDER BY prepared_at, fulfillment_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT $2
                )
                UPDATE komari_chat_reply_fulfillments AS parent
                SET delivery_state = 'NOT_DELIVERED',
                    not_delivered_at = COALESCE(not_delivered_at, NOW()),
                    reply_content = NULL,
                    updated_at = NOW()
                FROM candidates
                WHERE parent.fulfillment_id = candidates.fulfillment_id
                RETURNING parent.*
                """,
                max(1, freshness_seconds),
                limit,
            )
            if rows:
                projection = await self._project_proactive_reservation_identities(
                    connection,
                    [row["fulfillment_id"] for row in rows],
                )
                await connection.execute(
                    """
                    UPDATE komari_chat_reply_fulfillment_commitments
                    SET payload = NULL,
                        updated_at = NOW()
                    WHERE fulfillment_id = ANY($1::text[])
                    """,
                    [row["fulfillment_id"] for row in rows],
                )
            else:
                projection = {}
        expired: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["proactive_group_id"] = (
                projection.get(str(row["fulfillment_id"]), {}).get(
                    "proactive_group_id"
                )
            )
            item["proactive_reservation_id"] = (
                projection.get(str(row["fulfillment_id"]), {}).get(
                    "proactive_reservation_id"
                )
            )
            expired.append(item)
        return expired

    async def claim_operation(
        self,
        fulfillment_id: str,
        *,
        owner_token: str,
        lease_seconds: int,
    ) -> dict[str, Any] | None:
        """领取单个到期履约，并原子回收过期父租约。

        领取条件：存在到期子项，或所有子项都已 COMPLETED 而父完成
        标记尚未写入（父终态崩溃窗口恢复，只补父完成不重复子项）。
        """
        async with self.pg_pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE komari_chat_reply_fulfillments AS parent
                SET lease_owner = $2,
                    lease_expires_at = NOW() + ($3 * INTERVAL '1 second'),
                    updated_at = NOW()
                WHERE parent.fulfillment_id = $1
                  AND parent.delivery_state = 'DELIVERED'
                  AND parent.completed_at IS NULL
                  AND (
                      parent.lease_owner IS NULL
                      OR parent.lease_expires_at <= NOW()
                  )
                  AND (
                      EXISTS (
                          SELECT 1
                          FROM komari_chat_reply_fulfillment_commitments AS child
                          WHERE child.fulfillment_id = parent.fulfillment_id
                            AND (
                                child.state = 'PENDING'
                                OR (
                                    child.state = 'RETRY_WAIT'
                                    AND child.next_retry_at <= NOW()
                                )
                            )
                      )
                      OR (
                          EXISTS (
                              SELECT 1
                              FROM komari_chat_reply_fulfillment_commitments
                              AS child
                              WHERE child.fulfillment_id = parent.fulfillment_id
                          )
                          AND NOT EXISTS (
                              SELECT 1
                              FROM komari_chat_reply_fulfillment_commitments
                              AS child
                              WHERE child.fulfillment_id = parent.fulfillment_id
                                AND child.state <> 'COMPLETED'
                          )
                      )
                  )
                RETURNING parent.*
                """,
                fulfillment_id,
                owner_token,
                max(1, lease_seconds),
            )
        return dict(row) if row is not None else None

    async def claim_pending(
        self,
        *,
        owner_token: str,
        limit: int,
        lease_seconds: int,
    ) -> list[dict[str, Any]]:
        """按稳定顺序批量领取待处理履约，并跳过已锁父记录。

        领取条件与 ``claim_operation`` 一致：存在到期子项，或所有子项
        都已 COMPLETED 而父完成标记尚未写入（补父终态，不重复子项）。
        """
        if limit <= 0:
            return []
        async with self.pg_pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                """
                WITH candidates AS (
                    SELECT parent.fulfillment_id
                    FROM komari_chat_reply_fulfillments AS parent
                    WHERE parent.delivery_state = 'DELIVERED'
                      AND parent.completed_at IS NULL
                      AND (
                          parent.lease_owner IS NULL
                          OR parent.lease_expires_at <= NOW()
                      )
                      AND (
                          EXISTS (
                              SELECT 1
                              FROM komari_chat_reply_fulfillment_commitments
                              AS child
                              WHERE child.fulfillment_id = parent.fulfillment_id
                                AND (
                                    child.state = 'PENDING'
                                    OR (
                                        child.state = 'RETRY_WAIT'
                                        AND child.next_retry_at <= NOW()
                                    )
                                )
                          )
                          OR (
                              EXISTS (
                                  SELECT 1
                                  FROM komari_chat_reply_fulfillment_commitments
                                  AS child
                                  WHERE child.fulfillment_id = parent.fulfillment_id
                              )
                              AND NOT EXISTS (
                                  SELECT 1
                                  FROM komari_chat_reply_fulfillment_commitments
                                  AS child
                                  WHERE child.fulfillment_id = parent.fulfillment_id
                                    AND child.state <> 'COMPLETED'
                              )
                          )
                      )
                    ORDER BY
                        COALESCE(parent.delivered_at, parent.prepared_at),
                        parent.fulfillment_id
                    FOR UPDATE OF parent SKIP LOCKED
                    LIMIT $1
                )
                UPDATE komari_chat_reply_fulfillments AS parent
                SET lease_owner = $2,
                    lease_expires_at = NOW() + ($3 * INTERVAL '1 second'),
                    updated_at = NOW()
                FROM candidates
                WHERE parent.fulfillment_id = candidates.fulfillment_id
                RETURNING parent.*
                """,
                limit,
                owner_token,
                max(1, lease_seconds),
            )
        claimed = [dict(row) for row in rows]
        return sorted(
            claimed,
            key=lambda row: (
                row["delivered_at"] or row["prepared_at"],
                str(row["fulfillment_id"]),
            ),
        )

    async def renew_lease(
        self,
        fulfillment_id: str,
        *,
        owner_token: str,
        lease_seconds: int,
    ) -> bool:
        """仅允许当前且未过期 owner 续租。"""
        async with self.pg_pool.acquire() as connection:
            renewed = await connection.fetchval(
                """
                UPDATE komari_chat_reply_fulfillments
                SET lease_expires_at = NOW() + ($3 * INTERVAL '1 second'),
                    updated_at = NOW()
                WHERE fulfillment_id = $1
                  AND lease_owner = $2
                  AND lease_expires_at > NOW()
                  AND completed_at IS NULL
                RETURNING fulfillment_id
                """,
                fulfillment_id,
                owner_token,
                max(1, lease_seconds),
            )
        return renewed is not None

    async def release_lease(self, fulfillment_id: str, *, owner_token: str) -> bool:
        """仅允许当前且未过期 owner 释放父租约。

        同时服务承诺执行（未完成履约）与终态清理（已解决终态）两条
        路径：已解决终态记录同样需要把租约归还给下一轮清理。
        """
        async with self.pg_pool.acquire() as connection:
            released = await connection.fetchval(
                """
                UPDATE komari_chat_reply_fulfillments
                SET lease_owner = NULL,
                    lease_expires_at = NULL,
                    updated_at = NOW()
                WHERE fulfillment_id = $1
                  AND lease_owner = $2
                  AND lease_expires_at > NOW()
                RETURNING fulfillment_id
                """,
                fulfillment_id,
                owner_token,
            )
        return released is not None

    async def load_claimed_commitments(
        self,
        fulfillment_id: str,
        *,
        owner_token: str,
    ) -> list[dict[str, Any]] | None:
        """在有效父 owner 下加载当前到期承诺及其经领域校验的载荷。

        只返回 ``PENDING`` 或已到退避时间的 ``RETRY_WAIT`` 子项；父
        租约不再属于 ``owner_token``（丢失或被回收）时返回 None，执行
        器据此立即中止本轮。asyncpg 默认返回的 JSONB 文本先解码，再经
        冻结领域值对象校验并规范化；解码或校验失败的损坏载荷保持原样
        返回，由执行器按 ``invalid_payload`` 独立处置。返回顺序按
        ``COMMITMENT_TYPES`` 稳定排序，不依赖存储返回顺序。
        """
        async with self.pg_pool.acquire() as connection:
            owned = await connection.fetchval(
                """
                SELECT 1
                FROM komari_chat_reply_fulfillments
                WHERE fulfillment_id = $1
                  AND lease_owner = $2
                  AND lease_expires_at > NOW()
                  AND completed_at IS NULL
                """,
                fulfillment_id,
                owner_token,
            )
            if owned is None:
                return None
            rows = await connection.fetch(
                """
                SELECT commitment_type, payload
                FROM komari_chat_reply_fulfillment_commitments
                WHERE fulfillment_id = $1
                  AND (
                      state = 'PENDING'
                      OR (state = 'RETRY_WAIT' AND next_retry_at <= NOW())
                  )
                """,
                fulfillment_id,
            )
        commitments: list[dict[str, Any]] = []
        for row in rows:
            commitment_type = str(row["commitment_type"])
            payload_type = _COMMITMENT_PAYLOAD_TYPES[commitment_type]
            canonical: Any = row["payload"]
            if isinstance(canonical, str):
                # asyncpg 默认把 JSONB 返回为文本，先解码再进入领域校验。
                with suppress(TypeError, ValueError):
                    canonical = json.loads(canonical)
            if isinstance(canonical, dict):
                with suppress(TypeError, ValueError):
                    canonical = payload_type(**canonical).to_json()
            commitments.append(
                {
                    "commitment_type": commitment_type,
                    "payload": canonical,
                }
            )
        return sorted(
            commitments,
            key=lambda item: _COMMITMENT_ORDER.get(
                str(item["commitment_type"]), len(_COMMITMENT_ORDER)
            ),
        )

    async def mark_commitment_completed(
        self,
        fulfillment_id: str,
        *,
        commitment_type: str,
        owner_token: str,
    ) -> bool:
        """由有效父 owner 独立完成一个承诺，并消费其敏感载荷。"""
        async with self.pg_pool.acquire() as connection:
            completed = await connection.fetchval(
                """
                UPDATE komari_chat_reply_fulfillment_commitments AS child
                SET state = 'COMPLETED',
                    attempt_count = child.attempt_count + 1,
                    next_retry_at = NULL,
                    last_error_code = NULL,
                    payload = NULL,
                    completed_at = COALESCE(child.completed_at, NOW()),
                    updated_at = NOW()
                FROM komari_chat_reply_fulfillments AS parent
                WHERE child.fulfillment_id = $1
                  AND child.commitment_type = $2
                  AND child.state IN ('PENDING', 'RETRY_WAIT')
                  AND parent.fulfillment_id = child.fulfillment_id
                  AND parent.delivery_state = 'DELIVERED'
                  AND parent.completed_at IS NULL
                  AND parent.lease_owner = $3
                  AND parent.lease_expires_at > NOW()
                RETURNING child.fulfillment_id
                """,
                fulfillment_id,
                commitment_type,
                owner_token,
            )
        return completed is not None

    async def mark_commitment_failed(
        self,
        fulfillment_id: str,
        *,
        commitment_type: str,
        owner_token: str,
        error_code: str,
        max_attempts: int,
        retry_base_seconds: int,
        retry_max_seconds: int = 3600,
    ) -> str | None:
        """记录一个承诺的独立失败、退避或耗尽状态。

        ``retry_max_seconds`` 是执行器动态传入的退避上限；不写死 3600，
        也不写入冻结载荷。
        """
        async with self.pg_pool.acquire() as connection:
            state = await connection.fetchval(
                """
                UPDATE komari_chat_reply_fulfillment_commitments AS child
                SET state = CASE
                        WHEN child.attempt_count + 1 >= $4 THEN 'FAILED'
                        ELSE 'RETRY_WAIT'
                    END,
                    attempt_count = child.attempt_count + 1,
                    next_retry_at = CASE
                        WHEN child.attempt_count + 1 >= $4 THEN NULL
                        ELSE NOW() + (
                            LEAST(
                                $5 * POWER(2, GREATEST(child.attempt_count, 0)),
                                $7
                            ) * INTERVAL '1 second'
                        )
                    END,
                    last_error_code = LEFT($3, 100),
                    completed_at = NULL,
                    updated_at = NOW()
                FROM komari_chat_reply_fulfillments AS parent
                WHERE child.fulfillment_id = $1
                  AND child.commitment_type = $2
                  AND child.state IN ('PENDING', 'RETRY_WAIT')
                  AND parent.fulfillment_id = child.fulfillment_id
                  AND parent.delivery_state = 'DELIVERED'
                  AND parent.completed_at IS NULL
                  AND parent.lease_owner = $6
                  AND parent.lease_expires_at > NOW()
                RETURNING child.state
                """,
                fulfillment_id,
                commitment_type,
                error_code,
                max(1, max_attempts),
                max(1, retry_base_seconds),
                owner_token,
                max(1, retry_max_seconds),
            )
        return str(state) if state is not None else None

    async def complete_fulfillment(
        self,
        fulfillment_id: str,
        *,
        owner_token: str,
    ) -> bool:
        """仅在所有适用承诺完成后完成父履约并清除租约。

        完成门禁落库时防御性再清父 ``reply_content`` 与全部子 payload
        ——即使此前某步崩溃残留了正文或载荷，进入完成终态也一并抹除，
        不依赖调用方顺序。
        """
        async with self.pg_pool.acquire() as connection, connection.transaction():
            completed = await connection.fetchval(
                """
                UPDATE komari_chat_reply_fulfillments AS parent
                SET completed_at = COALESCE(parent.completed_at, NOW()),
                    reply_content = NULL,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    updated_at = NOW()
                WHERE parent.fulfillment_id = $1
                  AND parent.delivery_state = 'DELIVERED'
                  AND parent.completed_at IS NULL
                  AND parent.lease_owner = $2
                  AND parent.lease_expires_at > NOW()
                  AND NOT EXISTS (
                      SELECT 1
                      FROM komari_chat_reply_fulfillment_commitments AS child
                      WHERE child.fulfillment_id = parent.fulfillment_id
                        AND child.state <> 'COMPLETED'
                  )
                RETURNING parent.fulfillment_id
                """,
                fulfillment_id,
                owner_token,
            )
            if completed is not None:
                await connection.execute(
                    """
                    UPDATE komari_chat_reply_fulfillment_commitments
                    SET payload = NULL,
                        updated_at = NOW()
                    WHERE fulfillment_id = $1
                    """,
                    fulfillment_id,
                )
        return completed is not None

    async def claim_terminal_cleanup_candidates(
        self,
        *,
        owner_token: str,
        limit: int,
        lease_seconds: int,
        protection_days: int,
    ) -> list[dict[str, Any]]:
        """领取超过履约身份保护期的已解决终态，供两阶段清理处置。

        候选只含 ``NOT_DELIVERED`` 或 ``DELIVERED + completed_at 非空``
        两种已解决终态，以 ``not_delivered_at``/``completed_at`` 为保护
        期起点；绝不含 ``PENDING_CONFIRMATION``、待处置
        （``DELIVERED + FAILED/未完成``）或未满保护期的记录。领取即
        登记终态清理租约。
        """
        if limit <= 0:
            return []
        async with self.pg_pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                """
                WITH candidates AS (
                    SELECT parent.fulfillment_id
                    FROM komari_chat_reply_fulfillments AS parent
                    WHERE (
                        parent.delivery_state = 'NOT_DELIVERED'
                        OR (
                            parent.delivery_state = 'DELIVERED'
                            AND parent.completed_at IS NOT NULL
                        )
                    )
                      AND COALESCE(
                          parent.completed_at,
                          parent.not_delivered_at
                      ) <= NOW() - ($1 * INTERVAL '1 second')
                      AND (
                          parent.lease_owner IS NULL
                          OR parent.lease_expires_at <= NOW()
                      )
                    ORDER BY
                        COALESCE(
                            parent.completed_at,
                            parent.not_delivered_at
                        ),
                        parent.fulfillment_id
                    FOR UPDATE OF parent SKIP LOCKED
                    LIMIT $2
                )
                UPDATE komari_chat_reply_fulfillments AS parent
                SET lease_owner = $3,
                    lease_expires_at = NOW() + ($4 * INTERVAL '1 second'),
                    updated_at = NOW()
                FROM candidates
                WHERE parent.fulfillment_id = candidates.fulfillment_id
                RETURNING parent.*
                """,
                max(1, protection_days) * 86_400,
                limit,
                owner_token,
                max(1, lease_seconds),
            )
        return [dict(row) for row in rows]

    async def mark_idempotency_evidence_cleared(
        self,
        fulfillment_id: str,
        *,
        owner_token: str,
    ) -> bool:
        """在终态清理租约内落下游幂等证据已清除标记。

        只在有效 owner 且已解决终态上落标记；返回 False 表示租约已
        丢失或记录不满足终态，调用方不得继续删除父身份。
        """
        async with self.pg_pool.acquire() as connection:
            marked = await connection.fetchval(
                """
                UPDATE komari_chat_reply_fulfillments
                SET idempotency_evidence_cleared_at = COALESCE(
                        idempotency_evidence_cleared_at,
                        NOW()
                    ),
                    updated_at = NOW()
                WHERE fulfillment_id = $1
                  AND lease_owner = $2
                  AND lease_expires_at > NOW()
                  AND (
                      delivery_state = 'NOT_DELIVERED'
                      OR (
                          delivery_state = 'DELIVERED'
                          AND completed_at IS NOT NULL
                      )
                  )
                RETURNING fulfillment_id
                """,
                fulfillment_id,
                owner_token,
            )
        return marked is not None

    async def delete_terminal_tombstone(
        self,
        fulfillment_id: str,
        *,
        owner_token: str,
    ) -> bool:
        """在证据已清除且租约有效时删除父 tombstone（FK cascade 删子行）。

        删除必须同时满足：已解决终态、``idempotency_evidence_cleared_at``
        非空、当前有效 owner。任何条件不满足都返回 False，保证清理
        顺序不可反转——下游证据先于父身份删除。
        """
        async with self.pg_pool.acquire() as connection:
            deleted = await connection.fetchval(
                """
                DELETE FROM komari_chat_reply_fulfillments
                WHERE fulfillment_id = $1
                  AND lease_owner = $2
                  AND lease_expires_at > NOW()
                  AND idempotency_evidence_cleared_at IS NOT NULL
                  AND (
                      delivery_state = 'NOT_DELIVERED'
                      OR (
                          delivery_state = 'DELIVERED'
                          AND completed_at IS NOT NULL
                      )
                  )
                RETURNING fulfillment_id
                """,
                fulfillment_id,
                owner_token,
            )
        return deleted is not None

    async def list_for_management(
        self,
        *,
        status: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """管理安全投影：按派生状态筛选的最小身份列表。

        只返回最小身份、派生状态、时间、稳定错误码与正文指纹；绝不
        返回父 ``reply_content`` 或任何子 payload，也不暴露触发用户、
        Bot 身份、适配器或内部租约。筛选与总数由 SQL 按派生状态计算，
        CASE 表达式由领域模块 ``build_reply_fulfillment_status_case_sql``
        单一生成（与 ``derive_reply_fulfillment_status`` 同源），返回行
        的 ``status`` 由同一 CASE 表达式全量派生，保证投影与筛选同源。
        """
        if status is not None and status not in REPLY_FULFILLMENT_STATUSES:
            msg = f"未知的回复履约派生状态: {status!r}"
            raise ValueError(msg)
        if limit <= 0:
            return [], 0
        async with self.pg_pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                WITH derived AS (
                    SELECT parent.fulfillment_id,
                           parent.request_trace_id,
                           parent.trigger_message_id,
                           parent.group_id,
                           parent.payload_hash AS reply_fingerprint,
                           parent.prepared_at,
                           parent.send_started_at,
                           parent.delivered_at,
                           parent.platform_message_id,
                           parent.not_delivered_at,
                           parent.completed_at,
                           parent.delivery_state,
                           {build_reply_fulfillment_status_case_sql()}
                           AS status
                    FROM komari_chat_reply_fulfillments AS parent
                )
                SELECT fulfillment_id, request_trace_id, trigger_message_id,
                       group_id, reply_fingerprint, prepared_at,
                       send_started_at, delivered_at, platform_message_id,
                       not_delivered_at, completed_at, status,
                       COUNT(*) OVER() AS total
                FROM derived
                WHERE status = $1 OR $1 IS NULL
                ORDER BY prepared_at, fulfillment_id
                LIMIT $2 OFFSET $3
                """,
                status,
                limit,
                max(0, offset),
            )
            if not rows:
                return [], 0
            children = await connection.fetch(
                """
                SELECT fulfillment_id, commitment_type, state, attempt_count,
                       next_retry_at, last_error_code, completed_at
                FROM komari_chat_reply_fulfillment_commitments
                WHERE fulfillment_id = ANY($1::text[])
                """,
                [row["fulfillment_id"] for row in rows],
            )
        merged: dict[str, list[dict[str, Any]]] = {}
        for child in children:
            merged.setdefault(str(child["fulfillment_id"]), []).append(
                {
                    "commitment_type": str(child["commitment_type"]),
                    "state": str(child["state"]),
                    "attempt_count": int(child["attempt_count"]),
                    "next_retry_at": child["next_retry_at"],
                    "last_error_code": child["last_error_code"],
                    "completed_at": child["completed_at"],
                }
            )
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item.pop("delivery_state", None)
            item.pop("total", None)
            item["commitments"] = sorted(
                merged.get(str(row["fulfillment_id"]), []),
                key=lambda child: _COMMITMENT_ORDER.get(
                    str(child["commitment_type"]), len(_COMMITMENT_ORDER)
                ),
            )
            result.append(item)
        return result, int(rows[0]["total"])

    async def get_for_management(
        self,
        fulfillment_id: str,
    ) -> dict[str, Any] | None:
        """管理安全详情：只有待确认送达返回核对正文，其他状态一律为空。

        即使父表残留崩溃正文（DELIVERED 后未清除），非待确认状态也
        强制返回 ``reply_content=None``；子 payload 一律不出现在投影。
        """
        async with self.pg_pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT fulfillment_id, request_trace_id, trigger_message_id,
                       group_id, payload_hash AS reply_fingerprint,
                       reply_target_message_id, reply_content,
                       prepared_at, send_started_at, delivered_at,
                       platform_message_id, not_delivered_at, completed_at,
                       delivery_state
                FROM komari_chat_reply_fulfillments
                WHERE fulfillment_id = $1
                """,
                fulfillment_id,
            )
            if row is None:
                return None
            children = await connection.fetch(
                """
                SELECT commitment_type, state, attempt_count,
                       next_retry_at, last_error_code, completed_at
                FROM komari_chat_reply_fulfillment_commitments
                WHERE fulfillment_id = $1
                """,
                fulfillment_id,
            )
        detail = dict(row)
        detail["commitments"] = sorted(
            [
                {
                    "commitment_type": str(child["commitment_type"]),
                    "state": str(child["state"]),
                    "attempt_count": int(child["attempt_count"]),
                    "next_retry_at": child["next_retry_at"],
                    "last_error_code": child["last_error_code"],
                    "completed_at": child["completed_at"],
                }
                for child in children
            ],
            key=lambda child: _COMMITMENT_ORDER.get(
                str(child["commitment_type"]), len(_COMMITMENT_ORDER)
            ),
        )
        # 优先保留已有 status（安全行携带时），缺失才按领域字段推导。
        if "status" not in detail:
            detail["status"] = derive_reply_fulfillment_status(detail)
        if detail["status"] != "pending_confirmation":
            detail["reply_content"] = None
        return detail

    async def reconcile_delivered(
        self,
        fulfillment_id: str,
        *,
        platform_message_id: str | None,
    ) -> str:
        """原子确认送达：只允许 PENDING_CONFIRMATION 进入 DELIVERED。

        同一平台消息 ID 重放幂等（``idempotent``），不同平台 ID 冲突
        （``platform_message_conflict``）；首次无平台 ID 允许。进入
        已送达的同时清除父 ``reply_content``，不同步批量执行任何承诺。
        其他状态返回 ``state_conflict``，身份不存在返回 ``not_found``。
        """
        return await self._transition_to_delivered(
            fulfillment_id,
            platform_message_id=platform_message_id,
            backfill_missing_platform_id=True,
        )

    async def _transition_to_delivered(
        self,
        fulfillment_id: str,
        *,
        platform_message_id: str | None,
        backfill_missing_platform_id: bool = False,
    ) -> str:
        """单一送达状态机原语：带入口守卫的原子迁移到 DELIVERED。

        运行时（``mark_delivered``）与对账（``reconcile_delivered``）
        两侧语义差异全部收敛为本方法参数，公开方法只做结果形态映射：
        ``backfill_missing_platform_id`` 控制已送达缺平台 ID 时是否
        原子后补证据（对账侧补写并返回 ``idempotent``，运行时侧
        不回填）；平台消息 ID 冲突统一上报 ``platform_message_conflict``，
        由运行时侧映射为 ``ValueError``。结果形态为 outcome 字符串：
        ``updated`` / ``idempotent`` / ``not_found`` / ``state_conflict`` /
        ``platform_message_conflict``。冲突核对、条件补写与落败重读
        保持在同一个 acquire 上下文内完成。
        """
        async with self.pg_pool.acquire() as connection:
            changed = await connection.fetchval(
                """
                UPDATE komari_chat_reply_fulfillments
                SET delivery_state = 'DELIVERED',
                    platform_message_id = COALESCE(
                        platform_message_id,
                        $2
                    ),
                    delivered_at = COALESCE(delivered_at, NOW()),
                    reply_content = NULL,
                    updated_at = NOW()
                WHERE fulfillment_id = $1
                  AND delivery_state = 'PENDING_CONFIRMATION'
                RETURNING fulfillment_id
                """,
                fulfillment_id,
                platform_message_id,
            )
            if changed is not None:
                return "updated"
            existing = await connection.fetchrow(
                """
                SELECT delivery_state, platform_message_id
                FROM komari_chat_reply_fulfillments
                WHERE fulfillment_id = $1
                """,
                fulfillment_id,
            )
            if existing is None:
                return "not_found"
            if existing["delivery_state"] == "DELIVERED":
                existing_platform_id = existing["platform_message_id"]
                if (
                    platform_message_id is not None
                    and existing_platform_id is not None
                    and str(existing_platform_id) != platform_message_id
                ):
                    return "platform_message_conflict"
                if (
                    backfill_missing_platform_id
                    and existing_platform_id is None
                    and platform_message_id is not None
                ):
                    # 已送达但缺平台 ID 时后补证据：原子条件补写，只在并发下
                    # 仍为 NULL 时生效；补写失败再核对最新证据决定冲突或幂等。
                    # 冲突核对、补写与重读必须保持在同一个 acquire 上下文内。
                    written = await connection.fetchval(
                        """
                        UPDATE komari_chat_reply_fulfillments
                        SET platform_message_id = $2,
                            updated_at = NOW()
                        WHERE fulfillment_id = $1
                          AND delivery_state = 'DELIVERED'
                          AND platform_message_id IS NULL
                        RETURNING fulfillment_id
                        """,
                        fulfillment_id,
                        platform_message_id,
                    )
                    if written is None:
                        latest = await connection.fetchrow(
                            """
                            SELECT platform_message_id
                            FROM komari_chat_reply_fulfillments
                            WHERE fulfillment_id = $1
                            """,
                            fulfillment_id,
                        )
                        latest_platform_id = (
                            latest["platform_message_id"]
                            if latest is not None
                            else None
                        )
                        if (
                            latest_platform_id is not None
                            and str(latest_platform_id) != platform_message_id
                        ):
                            return "platform_message_conflict"
                return "idempotent"
            return "state_conflict"

    async def reconcile_not_delivered(self, fulfillment_id: str) -> dict[str, Any]:
        """原子确认未送达：PENDING_CONFIRMATION 进入互斥终态。

        同一事务内先取得主动预占的 group/reservation 信息，再清除父
        正文与全部子 payload；已处于 NOT_DELIVERED 返回
        ``{"outcome": "idempotent"}``（不可翻案），其他状态返回
        ``{"outcome": "state_conflict"}``。返回的预占信息供调用方在
        终态落库后幂等释放。
        """
        outcome, group_id, reservation_id = await self._transition_to_not_delivered(
            fulfillment_id,
            entry_states=("PENDING_CONFIRMATION",),
            project_proactive_reservation=True,
        )
        if outcome != "updated":
            return {"outcome": outcome}
        return {
            "outcome": "updated",
            "proactive_group_id": group_id,
            "proactive_reservation_id": reservation_id,
        }

    async def _transition_to_not_delivered(
        self,
        fulfillment_id: str,
        *,
        entry_states: tuple[str, ...],
        project_proactive_reservation: bool = False,
    ) -> tuple[str, Any, Any]:
        """单一未送达状态机原语：带入口守卫的原子迁移到互斥终态。

        运行时（``mark_not_delivered``）与对账（``reconcile_not_delivered``）
        两侧语义差异全部收敛为本方法参数，公开方法只做结果形态映射：
        ``entry_states`` 控制入口状态集合（运行时允许未发送直接进入，
        对账仅 PENDING_CONFIRMATION）；``project_proactive_reservation``
        控制在清载荷前先投影主动预占身份。同一事务内迁移成功时清除父
        正文与全部子 payload；迁移失败按现状分类。返回
        ``(outcome, proactive_group_id, proactive_reservation_id)``，
        outcome 为 ``updated`` / ``idempotent`` / ``not_found`` /
        ``state_conflict``，非 updated 时预占两键为 None。
        """
        async with self.pg_pool.acquire() as connection, connection.transaction():
            changed = await connection.fetchval(
                """
                UPDATE komari_chat_reply_fulfillments
                SET delivery_state = 'NOT_DELIVERED',
                    not_delivered_at = COALESCE(not_delivered_at, NOW()),
                    reply_content = NULL,
                    updated_at = NOW()
                WHERE fulfillment_id = $1
                  AND delivery_state = ANY($2::text[])
                RETURNING fulfillment_id
                """,
                fulfillment_id,
                entry_states,
            )
            if changed is None:
                existing = await connection.fetchval(
                    """
                    SELECT delivery_state
                    FROM komari_chat_reply_fulfillments
                    WHERE fulfillment_id = $1
                    """,
                    fulfillment_id,
                )
                if existing is None:
                    return ("not_found", None, None)
                if existing == "NOT_DELIVERED":
                    return ("idempotent", None, None)
                return ("state_conflict", None, None)

            group_id: Any = None
            reservation_id: Any = None
            if project_proactive_reservation:
                # 两段式时序：先从子 payload 取预占投影，再清 payload。
                proactive = await connection.fetchrow(
                    """
                    SELECT payload
                    FROM komari_chat_reply_fulfillment_commitments
                    WHERE fulfillment_id = $1
                      AND commitment_type = 'proactive_reply_confirmation'
                    """,
                    fulfillment_id,
                )
                proactive_payload: Any = (
                    proactive["payload"] if proactive is not None else None
                )
                if isinstance(proactive_payload, str):
                    with suppress(TypeError, ValueError):
                        proactive_payload = json.loads(proactive_payload)
                if isinstance(proactive_payload, dict):
                    group_id = proactive_payload.get("group_id")
                    reservation_id = proactive_payload.get("reservation_id")
            await connection.execute(
                """
                UPDATE komari_chat_reply_fulfillment_commitments
                SET payload = NULL,
                    updated_at = NOW()
                WHERE fulfillment_id = $1
                """,
                fulfillment_id,
            )
        return ("updated", group_id, reservation_id)

    async def resume_failed_commitment(
        self,
        fulfillment_id: str,
        *,
        commitment_type: str,
    ) -> str:
        """原子续跑指定失败承诺：FAILED -> PENDING，父必须 DELIVERED 且未完成。

        ``attempt_count`` 归零并清除 ``next_retry_at`` / ``last_error_code``，
        冻结 payload 一律不修改。并发续跑只有一个成功（``updated``）；
        子状态为 PENDING / RETRY_WAIT / COMPLETED 或父未完成条件不满足
        都返回 ``state_conflict``，身份或承诺不存在返回 ``not_found``。
        续跑同时只复位本子项的 ``disposition_alerted_at``（同一承诺再次
        耗尽属于新告警代际，允许再告警一次），绝不修改兄弟项标记。
        """
        async with self.pg_pool.acquire() as connection:
            updated = await connection.fetchval(
                """
                UPDATE komari_chat_reply_fulfillment_commitments AS child
                SET state = 'PENDING',
                    attempt_count = 0,
                    next_retry_at = NULL,
                    last_error_code = NULL,
                    completed_at = NULL,
                    disposition_alerted_at = NULL,
                    updated_at = NOW()
                FROM komari_chat_reply_fulfillments AS parent
                WHERE child.fulfillment_id = $1
                  AND child.commitment_type = $2
                  AND child.state = 'FAILED'
                  AND parent.fulfillment_id = child.fulfillment_id
                  AND parent.delivery_state = 'DELIVERED'
                  AND parent.completed_at IS NULL
                RETURNING child.fulfillment_id
                """,
                fulfillment_id,
                commitment_type,
            )
            if updated is not None:
                return "updated"
            parent_exists = await connection.fetchval(
                """
                SELECT 1
                FROM komari_chat_reply_fulfillments
                WHERE fulfillment_id = $1
                """,
                fulfillment_id,
            )
            if parent_exists is None:
                return "not_found"
            child_exists = await connection.fetchval(
                """
                SELECT 1
                FROM komari_chat_reply_fulfillment_commitments
                WHERE fulfillment_id = $1
                  AND commitment_type = $2
                """,
                fulfillment_id,
                commitment_type,
            )
            if child_exists is None:
                return "not_found"
        return "state_conflict"

    async def claim_pending_confirmation_alerts(
        self,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        """原子领取首次进入待确认转换的履约，落父表告警去重时间戳。

        只有 ``PENDING_CONFIRMATION`` 且 ``pending_confirmation_alerted_at``
        为空的行才会被领取；领取即在同一事务内落时间戳，并发 worker
        与仓库重建都不会重复命中。返回行只含告警白名单字段，绝不携带
        父 ``reply_content``、群信息或任何内部身份。
        """
        if limit <= 0:
            return []
        async with self.pg_pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                """
                WITH candidates AS (
                    SELECT parent.fulfillment_id
                    FROM komari_chat_reply_fulfillments AS parent
                    WHERE parent.delivery_state = 'PENDING_CONFIRMATION'
                      AND parent.pending_confirmation_alerted_at IS NULL
                    ORDER BY parent.prepared_at, parent.fulfillment_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT $1
                )
                UPDATE komari_chat_reply_fulfillments AS parent
                SET pending_confirmation_alerted_at = NOW(),
                    updated_at = NOW()
                FROM candidates
                WHERE parent.fulfillment_id = candidates.fulfillment_id
                RETURNING
                    parent.fulfillment_id,
                    'pending_confirmation'::text AS status,
                    NULL::text AS commitment_type,
                    NULL::text AS error_code
                """,
                limit,
            )
        return [dict(row) for row in rows]

    async def claim_commitment_disposition_alerts(
        self,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        """原子领取首次进入待处置转换的失败承诺，落子表告警去重时间戳。

        领取身份是（``fulfillment_id``, ``commitment_type``）二元组：
        只有 ``FAILED`` 且 ``disposition_alerted_at`` 为空、父已送达且
        未完成的子项才会被领取，普通 ``RETRY_WAIT`` 自动重试永不成为
        候选。领取即在同一事务内落时间戳，并发与重启不重复命中；返回
        行只含白名单字段与稳定错误码，绝不携带冻结 payload。
        """
        if limit <= 0:
            return []
        async with self.pg_pool.acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                """
                WITH candidates AS (
                    SELECT child.fulfillment_id, child.commitment_type
                    FROM komari_chat_reply_fulfillment_commitments AS child
                    JOIN komari_chat_reply_fulfillments AS parent
                      ON parent.fulfillment_id = child.fulfillment_id
                    WHERE child.state = 'FAILED'
                      AND child.disposition_alerted_at IS NULL
                      AND parent.delivery_state = 'DELIVERED'
                      AND parent.completed_at IS NULL
                    ORDER BY child.fulfillment_id, child.commitment_type
                    FOR UPDATE OF child SKIP LOCKED
                    LIMIT $1
                )
                UPDATE komari_chat_reply_fulfillment_commitments AS child
                SET disposition_alerted_at = NOW(),
                    updated_at = NOW()
                FROM candidates
                WHERE child.fulfillment_id = candidates.fulfillment_id
                  AND child.commitment_type = candidates.commitment_type
                RETURNING
                    child.fulfillment_id,
                    'needs_disposition'::text AS status,
                    child.commitment_type,
                    child.last_error_code AS error_code
                """,
                limit,
            )
        return [dict(row) for row in rows]


__all__ = [
    "COMMITMENT_TYPES",
    "AssistantReplyHistoryPayload",
    "CommitmentPayload",
    "FavorabilityAdjustmentPayload",
    "InteractionHistoryPayload",
    "ProactiveReplyConfirmationPayload",
    "ReplyCommitmentInput",
    "ReplyFulfillmentConflictError",
    "ReplyFulfillmentDraft",
    "ReplyFulfillmentRepository",
]
