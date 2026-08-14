"""旧回复履约宽 outbox 停机回填：预检、搬运、事务内校验。

迁移 ID: 0010
父迁移: 0009

本 revision 在单个事务内把旧 ``komari_chat_reply_commit_outbox`` 的
全部行回填到 0006 建立的父/子履约表，正常聊天仍只走旧 adapter：
本 revision 不接入双写、不双读、不提供运行时回退，也不提前切换
生产路径（TSK-87 之前禁止 cutover）；旧表原样保留。

映射规则（与 TSK-79..TSK-85 领域模型一致）：

- PREPARED 无论旧 delivery_state 是 NOT_STARTED 还是
  PENDING_CONFIRMATION，统一回填父 PENDING_CONFIRMATION，绝不自动
  发送；保留父正文，全部适用承诺为 PENDING。
- DELIVERED / PROCESSING / FAILED 按四项旧完成时间戳映射。适用集合
  固定为：主动回复确认仅当预占非空、好感度调整与角色回复历史总是、
  互动历史仅当全局互动开启；承诺按固定顺序推进，适用承诺的已完成
  时间戳必须是前缀。旧 worker 对不适用承诺也会落占位完成时间戳
  （无预占仍写 proactive_confirmed_at、全局互动关闭仍写
  interaction_stored_at），占位事实不参与映射，由适用集合自然忽略。
  首个未完成承诺承接旧全局 attempt_count 与错误码：DELIVERED 且旧
  next_retry_at 非空时该项 RETRY_WAIT，PROCESSING 清租约后该项
  PENDING，FAILED 该项 FAILED；后续未完成项一律 attempt_count=0 /
  PENDING。
- COMPLETED / CANCELLED 只回填最小终态父身份（不保留正文、不建子
  行）；所有父租约清空；告警时间戳保持 NULL。
- payload_hash 原样继承旧行，不重算、不引入任何摘要扩展。
- FAILED 若 updated_at 早于 NOW() - (retention_days + 1) 天属于
  超安全窗口的歧义历史，在任何插入前整体失败；retention_days 取
  ``komari_chat_config`` id=1 的 reply_commit_tombstone_retention_days，
  无配置行时默认 30。
- 所有预检与校验失败只报告数量与最小 fulfillment 身份，绝不投影
  回复正文、互动载荷或任何敏感字段。

本 revision 自包含：不导入 komari_bot，不做任何结构变更，只做数据
锁定、预检、搬运与事务内校验；任一校验失败整体回滚。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Connection


revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 无配置行时的身份保护期默认天数（与 komari_chat 配置 schema 一致）。
_DEFAULT_RETENTION_DAYS = 30


def _lock_legacy_rows(connection: Connection) -> None:
    """锁住全部旧 outbox 行，固定事务内快照并阻止并发回填。"""
    connection.execute(
        text(
            "SELECT operation_id "
            "FROM komari_chat_reply_commit_outbox "
            "ORDER BY operation_id "
            "FOR UPDATE"
        )
    ).all()


def _read_retention_days(connection: Connection) -> int:
    """读取身份保护期天数；无配置行时按默认 30 天处理。"""
    row = connection.execute(
        text(
            "SELECT reply_commit_tombstone_retention_days "
            "FROM komari_chat_config "
            "WHERE id = 1"
        )
    ).first()
    if row is None or row[0] is None:
        return _DEFAULT_RETENTION_DAYS
    return max(1, int(row[0]))


def _require_no_conflict(
    connection: Connection,
    *,
    where: str,
    label: str,
) -> None:
    """旧表条件命中即失败：只报告数量与最小 fulfillment 身份。

    ``where`` 拼接到旧表的 WHERE 条件；任何命中都表示迁移无法确定
    映射或目标已冲突，必须在任何写入前中止（事务整体回滚）。错误
    正文只含 ``{label}_count=`` 与 ``minimum_fulfillment_id=`` 两个
    最小投影，绝不携带回复正文、互动载荷或异常原文。
    """
    row = connection.execute(
        text(
            "SELECT COUNT(*), MIN(operation_id) "
            "FROM komari_chat_reply_commit_outbox "
            f"WHERE {where}"
        )
    ).first()
    if row is None:
        return
    count = int(row[0] or 0)
    minimum_id = row[1]
    if not count:
        return
    if label == "ambiguous_failed":
        # 字面量固定消息：版本链守卫测试要求文件内保留
        # ``ambiguous_failed_count=`` 与 ``minimum_fulfillment_id=``
        # 两个最小投影 token，此分支不是冗余分支。
        msg = f"ambiguous_failed_count={count} minimum_fulfillment_id={minimum_id}"
    else:
        msg = f"{label}_count={count} minimum_fulfillment_id={minimum_id}"
    raise RuntimeError(msg)


def _preflight(connection: Connection, *, retention_days: int) -> None:
    """在任何写入前完成全部预检，命中即中止。"""
    # 超安全窗口的 FAILED：updated_at 早于 NOW() - (retention + 1) 天
    _require_no_conflict(
        connection,
        where=(
            "status = 'FAILED' AND updated_at < NOW() - "
            f"({retention_days + 1} * INTERVAL '1 day')"
        ),
        label="ambiguous_failed",
    )
    # 非法载荷指纹：两代 hash 都是 64 位小写十六进制，其他形状无法继承
    _require_no_conflict(
        connection,
        where="payload_hash !~ '^[0-9a-f]{64}$'",
        label="invalid_hash",
    )
    # 父表必需身份缺失（0007 之前的行可能缺少恢复身份列）
    _require_no_conflict(
        connection,
        where=(
            "request_trace_id IS NULL OR request_trace_id = '' "
            "OR source_message_id IS NULL OR source_message_id = '' "
            "OR group_id IS NULL OR group_id = '' "
            "OR user_id IS NULL OR user_id = '' "
            "OR bot_self_id IS NULL OR bot_self_id = '' "
            "OR adapter_name IS NULL OR adapter_name = '' "
            "OR reply_target_message_id IS NULL "
            "OR reply_target_message_id = ''"
        ),
        label="missing_identity",
    )
    # 非终态行冻结载荷缺失（终态最小身份不要求载荷）
    _require_no_conflict(
        connection,
        where=(
            "status IN ('PREPARED', 'DELIVERED', 'PROCESSING', 'FAILED') "
            "AND (reply_content IS NULL OR reply_content = '' "
            "OR bot_nickname IS NULL OR bot_nickname = '' "
            "OR favorability_reason IS NULL OR favorability_reason = '' "
            "OR (global_interaction_enabled AND interaction_history IS NULL))"
        ),
        label="missing_payload",
    )
    # 承诺进度一致性：PREPARED 不得有步骤进度；其余非终态行的适用
    # 承诺时间戳必须是前缀。不适用承诺的占位完成时间戳（旧 worker
    # 无条件落 proactive_confirmed_at / interaction_stored_at）不是
    # 映射冲突，占位事实由 applicable CTE 自然忽略，这里不预检。
    _require_no_conflict(
        connection,
        where=(
            "status IN ('PREPARED', 'DELIVERED', 'PROCESSING', 'FAILED') "
            "AND ("
            "(status = 'PREPARED' AND (proactive_confirmed_at IS NOT NULL "
            "OR favorability_applied_at IS NOT NULL "
            "OR ai_history_stored_at IS NOT NULL "
            "OR interaction_stored_at IS NOT NULL)) "
            "OR (status <> 'PREPARED' AND ("
            "(proactive_reservation_id IS NOT NULL "
            "AND proactive_confirmed_at IS NULL "
            "AND favorability_applied_at IS NOT NULL) "
            "OR (favorability_applied_at IS NULL "
            "AND ai_history_stored_at IS NOT NULL) "
            "OR (ai_history_stored_at IS NULL "
            "AND interaction_stored_at IS NOT NULL "
            "AND global_interaction_enabled))))"
        ),
        label="step_mapping_conflict",
    )
    # 送达事实缺失：已送达族状态必须留有送达时间戳
    _require_no_conflict(
        connection,
        where=(
            "status IN ('DELIVERED', 'PROCESSING', 'COMPLETED', 'FAILED') "
            "AND delivered_at IS NULL"
        ),
        label="delivered_fact_missing",
    )
    # 未送达事实缺失：CANCELLED 必须留有未送达时间戳
    _require_no_conflict(
        connection,
        where="status = 'CANCELLED' AND not_delivered_at IS NULL",
        label="cancelled_fact_missing",
    )
    # 目标父表已存在同身份记录：身份冲突，整体中止
    _require_no_conflict(
        connection,
        where=(
            "operation_id IN ("
            "SELECT fulfillment_id FROM komari_chat_reply_fulfillments)"
        ),
        label="target_conflict",
    )


def _backfill_parents(connection: Connection) -> None:
    """按旧行状态回填最小父事实；租约与告警时间戳一律保持 NULL。"""
    connection.execute(
        text(
            """
            INSERT INTO komari_chat_reply_fulfillments (
                fulfillment_id, payload_hash, request_trace_id,
                trigger_message_id, trigger_user_id, group_id,
                bot_self_id, adapter_name, reply_target_message_id,
                reply_content, delivery_state, platform_message_id,
                prepared_at, send_started_at, delivered_at,
                not_delivered_at, lease_owner, lease_expires_at,
                completed_at, created_at, updated_at
            )
            SELECT
                operation_id,
                payload_hash,
                request_trace_id,
                source_message_id,
                user_id,
                group_id,
                bot_self_id,
                adapter_name,
                reply_target_message_id,
                CASE WHEN status = 'PREPARED' THEN reply_content ELSE NULL END,
                CASE status
                    WHEN 'PREPARED' THEN 'PENDING_CONFIRMATION'
                    WHEN 'CANCELLED' THEN 'NOT_DELIVERED'
                    ELSE 'DELIVERED'
                END,
                platform_message_id,
                prepared_at,
                CASE
                    WHEN status = 'CANCELLED' THEN send_started_at
                    ELSE COALESCE(send_started_at, prepared_at)
                END,
                CASE
                    WHEN status IN (
                        'DELIVERED', 'PROCESSING', 'COMPLETED', 'FAILED'
                    ) THEN COALESCE(delivered_at, completed_at)
                    ELSE NULL
                END,
                CASE WHEN status = 'CANCELLED' THEN not_delivered_at ELSE NULL END,
                NULL,
                NULL,
                CASE WHEN status = 'COMPLETED' THEN completed_at ELSE NULL END,
                created_at,
                updated_at
            FROM komari_chat_reply_commit_outbox
            """
        )
    )


def _backfill_commitments(connection: Connection) -> None:
    """按固定适用集合与旧步骤时间戳派生子项状态；终态行不建子行。"""
    connection.execute(
        text(
            """
            WITH applicable AS (
                SELECT operation_id,
                       'proactive_reply_confirmation' AS commitment_type,
                       proactive_confirmed_at AS step_completed_at
                FROM komari_chat_reply_commit_outbox
                WHERE proactive_reservation_id IS NOT NULL
                UNION ALL
                SELECT operation_id,
                       'favorability_adjustment',
                       favorability_applied_at
                FROM komari_chat_reply_commit_outbox
                UNION ALL
                SELECT operation_id,
                       'assistant_reply_history',
                       ai_history_stored_at
                FROM komari_chat_reply_commit_outbox
                UNION ALL
                SELECT operation_id,
                       'interaction_history',
                       interaction_stored_at
                FROM komari_chat_reply_commit_outbox
                WHERE global_interaction_enabled
            ),
            ordered AS (
                SELECT
                    applicable.operation_id,
                    applicable.commitment_type,
                    applicable.step_completed_at,
                    legacy.status,
                    legacy.attempt_count,
                    legacy.next_retry_at,
                    legacy.last_error_code,
                    legacy.group_id,
                    legacy.user_id,
                    legacy.user_nickname,
                    legacy.bot_nickname,
                    legacy.reply_content,
                    legacy.reply_timestamp,
                    legacy.favorability_delta,
                    legacy.favorability_reason,
                    legacy.proactive_reservation_id,
                    legacy.proactive_cooldown_seconds,
                    legacy.global_interaction_trigger_size,
                    legacy.source_message_id,
                    legacy.interaction_history,
                    CASE
                        WHEN applicable.step_completed_at IS NOT NULL THEN 1
                        ELSE 0
                    END AS completed_flag,
                    ROW_NUMBER() OVER (
                        PARTITION BY applicable.operation_id
                        ORDER BY CASE applicable.commitment_type
                            WHEN 'proactive_reply_confirmation' THEN 0
                            WHEN 'favorability_adjustment' THEN 1
                            WHEN 'assistant_reply_history' THEN 2
                            ELSE 3
                        END
                    ) AS commitment_index
                FROM applicable
                JOIN komari_chat_reply_commit_outbox AS legacy
                  ON legacy.operation_id = applicable.operation_id
                WHERE legacy.status NOT IN ('COMPLETED', 'CANCELLED')
            ),
            first_incomplete AS (
                SELECT operation_id, MIN(commitment_index) AS first_index
                FROM ordered
                WHERE completed_flag = 0
                GROUP BY operation_id
            )
            INSERT INTO komari_chat_reply_fulfillment_commitments (
                fulfillment_id, commitment_type, state, attempt_count,
                next_retry_at, last_error_code, payload, completed_at
            )
            SELECT
                ordered.operation_id,
                ordered.commitment_type,
                CASE
                    WHEN ordered.completed_flag = 1 THEN 'COMPLETED'
                    WHEN ordered.status = 'FAILED'
                         AND ordered.commitment_index
                             = first_incomplete.first_index
                        THEN 'FAILED'
                    WHEN ordered.status = 'DELIVERED'
                         AND ordered.commitment_index
                             = first_incomplete.first_index
                         AND ordered.next_retry_at IS NOT NULL
                        THEN 'RETRY_WAIT'
                    ELSE 'PENDING'
                END,
                CASE
                    WHEN ordered.completed_flag = 1 THEN 0
                    WHEN ordered.commitment_index
                         = first_incomplete.first_index
                        THEN ordered.attempt_count
                    ELSE 0
                END,
                CASE
                    WHEN ordered.completed_flag = 1 THEN NULL
                    WHEN ordered.status = 'DELIVERED'
                         AND ordered.commitment_index
                             = first_incomplete.first_index
                        THEN ordered.next_retry_at
                    ELSE NULL
                END,
                CASE
                    WHEN ordered.completed_flag = 1 THEN NULL
                    WHEN ordered.commitment_index
                         = first_incomplete.first_index
                        THEN ordered.last_error_code
                    ELSE NULL
                END,
                CASE
                    WHEN ordered.completed_flag = 1 THEN NULL
                    WHEN ordered.commitment_type
                         = 'proactive_reply_confirmation'
                        THEN jsonb_build_object(
                            'group_id', ordered.group_id,
                            'reservation_id',
                                ordered.proactive_reservation_id,
                            'cooldown_seconds',
                                ordered.proactive_cooldown_seconds
                        )
                    WHEN ordered.commitment_type
                         = 'favorability_adjustment'
                        THEN jsonb_build_object(
                            'user_id', ordered.user_id,
                            'delta', ordered.favorability_delta,
                            'reason', ordered.favorability_reason
                        )
                    WHEN ordered.commitment_type
                         = 'assistant_reply_history'
                        THEN jsonb_build_object(
                            'group_id', ordered.group_id,
                            'bot_nickname', ordered.bot_nickname,
                            'reply_content', ordered.reply_content,
                            'reply_timestamp', ordered.reply_timestamp
                        )
                    ELSE jsonb_build_object(
                        'user_id', ordered.user_id,
                        'display_name', COALESCE(
                            ordered.user_nickname, ordered.user_id
                        ),
                        'trigger_size',
                            ordered.global_interaction_trigger_size,
                        'reply_timestamp', ordered.reply_timestamp,
                        'trigger_message_id', ordered.source_message_id,
                        'record', COALESCE(
                            ordered.interaction_history, '{}'::jsonb
                        )
                    )
                END,
                CASE
                    WHEN ordered.completed_flag = 1
                        THEN ordered.step_completed_at
                    ELSE NULL
                END
            FROM ordered
            LEFT JOIN first_incomplete
              ON first_incomplete.operation_id = ordered.operation_id
            """
        )
    )


def _validate(connection: Connection) -> None:
    """事务内校验父子数量、适用集合、唯一性、终态门禁、租约与载荷。"""
    # 每条旧行都必须有父记录（父子数量一致）
    _require_no_conflict(
        connection,
        where=(
            "operation_id NOT IN ("
            "SELECT fulfillment_id FROM komari_chat_reply_fulfillments)"
        ),
        label="parent_missing",
    )
    # 非终态行的子项数量必须等于适用承诺数量
    _require_no_conflict(
        connection,
        where=(
            "status IN ('PREPARED', 'DELIVERED', 'PROCESSING', 'FAILED') "
            "AND (SELECT COUNT(*) "
            "     FROM komari_chat_reply_fulfillment_commitments AS child "
            "     WHERE child.fulfillment_id "
            "         = komari_chat_reply_commit_outbox.operation_id) "
            "     <> ((CASE "
            "             WHEN proactive_reservation_id IS NOT NULL THEN 1 "
            "             ELSE 0 "
            "         END) + 2 + (CASE "
            "             WHEN global_interaction_enabled THEN 1 "
            "             ELSE 0 "
            "         END))"
        ),
        label="commitment_count_mismatch",
    )
    # 终态门禁：COMPLETED / CANCELLED 不得产生任何子行
    _require_no_conflict(
        connection,
        where=(
            "status IN ('COMPLETED', 'CANCELLED') "
            "AND operation_id IN ("
            "SELECT fulfillment_id "
            "FROM komari_chat_reply_fulfillment_commitments)"
        ),
        label="terminal_child_conflict",
    )
    # 适用集合：不适用承诺类型不得出现
    _require_no_conflict(
        connection,
        where=(
            "operation_id IN ("
            "SELECT fulfillment_id "
            "FROM komari_chat_reply_fulfillment_commitments) "
            "AND ("
            "(proactive_reservation_id IS NULL AND operation_id IN ("
            "    SELECT child.fulfillment_id "
            "    FROM komari_chat_reply_fulfillment_commitments AS child "
            "    WHERE child.commitment_type "
            "        = 'proactive_reply_confirmation')) "
            "OR (NOT global_interaction_enabled AND operation_id IN ("
            "    SELECT child.fulfillment_id "
            "    FROM komari_chat_reply_fulfillment_commitments AS child "
            "    WHERE child.commitment_type = 'interaction_history')))"
        ),
        label="commitment_set_mismatch",
    )
    # 唯一性：同一父记录内承诺类型不得重复
    _require_no_conflict(
        connection,
        where=(
            "operation_id IN ("
            "SELECT fulfillment_id FROM ("
            "    SELECT child.fulfillment_id "
            "    FROM komari_chat_reply_fulfillment_commitments AS child "
            "    GROUP BY child.fulfillment_id "
            "    HAVING COUNT(DISTINCT child.commitment_type) "
            "        <> COUNT(child.commitment_type)"
            ") AS duplicate_commitments)"
        ),
        label="duplicate_commitment",
    )
    # 终态父事实：COMPLETED 必须有完成时间，CANCELLED 必须是未送达
    _require_no_conflict(
        connection,
        where=(
            "status = 'COMPLETED' AND operation_id NOT IN ("
            "SELECT fulfillment_id "
            "FROM komari_chat_reply_fulfillments AS parent "
            "WHERE parent.completed_at IS NOT NULL)"
        ),
        label="completed_fact_missing",
    )
    _require_no_conflict(
        connection,
        where=(
            "status = 'CANCELLED' AND operation_id NOT IN ("
            "SELECT fulfillment_id "
            "FROM komari_chat_reply_fulfillments AS parent "
            "WHERE parent.delivery_state = 'NOT_DELIVERED' "
            "  AND parent.not_delivered_at IS NOT NULL)"
        ),
        label="not_delivered_fact_missing",
    )
    # 租约清除：所有回填父记录租约必须为空
    _require_no_conflict(
        connection,
        where=(
            "operation_id IN ("
            "SELECT fulfillment_id "
            "FROM komari_chat_reply_fulfillments AS parent "
            "WHERE parent.lease_owner IS NOT NULL "
            "  OR parent.lease_expires_at IS NOT NULL)"
        ),
        label="lease_not_cleared",
    )
    # 告警时间戳保持 NULL：回填不得制造待告警去重事实
    _require_no_conflict(
        connection,
        where=(
            "operation_id IN ("
            "SELECT fulfillment_id "
            "FROM komari_chat_reply_fulfillments AS parent "
            "WHERE parent.pending_confirmation_alerted_at IS NOT NULL "
            "  OR parent.idempotency_evidence_cleared_at IS NOT NULL)"
        ),
        label="parent_alert_not_cleared",
    )
    _require_no_conflict(
        connection,
        where=(
            "operation_id IN ("
            "SELECT fulfillment_id "
            "FROM komari_chat_reply_fulfillment_commitments AS child "
            "WHERE child.disposition_alerted_at IS NOT NULL)"
        ),
        label="child_alert_not_cleared",
    )
    # 载荷最小化：COMPLETED 子项清载荷，非终态子项必须保留冻结载荷
    _require_no_conflict(
        connection,
        where=(
            "operation_id IN ("
            "SELECT fulfillment_id "
            "FROM komari_chat_reply_fulfillment_commitments AS child "
            "WHERE child.state = 'COMPLETED' AND child.payload IS NOT NULL)"
        ),
        label="completed_payload_kept",
    )
    _require_no_conflict(
        connection,
        where=(
            "operation_id IN ("
            "SELECT fulfillment_id "
            "FROM komari_chat_reply_fulfillment_commitments AS child "
            "WHERE child.state <> 'COMPLETED' AND child.payload IS NULL)"
        ),
        label="pending_payload_missing",
    )
    # 父正文最小化：只有 PREPARED 保留正文，其余一律清空
    _require_no_conflict(
        connection,
        where=(
            "status <> 'PREPARED' AND operation_id IN ("
            "SELECT fulfillment_id "
            "FROM komari_chat_reply_fulfillments AS parent "
            "WHERE parent.reply_content IS NOT NULL)"
        ),
        label="parent_content_not_minimized",
    )
    _require_no_conflict(
        connection,
        where=(
            "status = 'PREPARED' AND operation_id IN ("
            "SELECT fulfillment_id "
            "FROM komari_chat_reply_fulfillments AS parent "
            "WHERE parent.reply_content IS NULL)"
        ),
        label="prepared_content_missing",
    )


def upgrade(name: str = "") -> None:
    if name:
        return

    connection = op.get_bind()
    _lock_legacy_rows(connection)
    retention_days = _read_retention_days(connection)
    _preflight(connection, retention_days=retention_days)
    _backfill_parents(connection)
    _backfill_commitments(connection)
    _validate(connection)


def downgrade(name: str = "") -> None:
    if name:
        return

    connection = op.get_bind()
    # 只回收本 revision 回填到新父子表的记录：用确定性 identity join
    # 旧表限定回收范围，绝不触碰旧表，也不删除 0010 之前的父子记录
    connection.execute(
        text(
            """
            DELETE FROM komari_chat_reply_fulfillment_commitments
            WHERE fulfillment_id IN (
                SELECT parent.fulfillment_id
                FROM komari_chat_reply_fulfillments AS parent
                JOIN komari_chat_reply_commit_outbox AS legacy
                  ON legacy.operation_id = parent.fulfillment_id
            )
            """
        )
    )
    connection.execute(
        text(
            """
            DELETE FROM komari_chat_reply_fulfillments
            WHERE fulfillment_id IN (
                SELECT operation_id
                FROM komari_chat_reply_commit_outbox
            )
            """
        )
    )
