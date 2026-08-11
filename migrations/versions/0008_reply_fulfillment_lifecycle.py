"""回复履约终态最小化与两阶段清理事实。

迁移 ID: 0008
父迁移: 0007

本 revision 只扩展新父子履约模型（TSK-83）：父 ``reply_content`` 由
NOT NULL 改为可空，供送达/未送达/完成生命周期节点整项清除完整正文；
新增 ``idempotency_evidence_cleared_at`` 记录下游幂等证据（Redis 防重
键与好感度账本）已清除的事实，作为删除父 tombstone 的前置门禁；
新增终态清理候选索引覆盖已解决终态与保护期起点。

本 revision 不回填旧 outbox、不删除旧表或旧配置、不接线生产路径；
downgrade 先把 NULL 正文保守回填为空串，再恢复 NOT NULL，并回收
索引与证据标记列。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence


revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(name: str = "") -> None:
    if name:
        return

    # 完整正文只在准备/发送阶段需要；送达、未送达与完成终态整项清除
    op.execute(
        """
        ALTER TABLE komari_chat_reply_fulfillments
        ALTER COLUMN reply_content DROP NOT NULL
        """
    )

    # 两阶段清理的证据标记：下游幂等证据清除后落此列，删除父身份前必须非空
    op.execute(
        """
        ALTER TABLE komari_chat_reply_fulfillments
        ADD COLUMN idempotency_evidence_cleared_at TIMESTAMPTZ
        """
    )

    # 终态清理候选索引：已解决终态按保护期起点与租约可领取性扫描
    op.execute(
        """
        CREATE INDEX idx_reply_fulfillment_terminal_cleanup
        ON komari_chat_reply_fulfillments (
            delivery_state,
            completed_at,
            not_delivered_at,
            lease_expires_at
        )
        """
    )


def downgrade(name: str = "") -> None:
    if name:
        return

    # 先保守回填 0008 期间产生的 NULL 正文为空串，再恢复 NOT NULL，
    # 避免现存数据违反旧约束导致回退失败
    op.execute(
        """
        UPDATE komari_chat_reply_fulfillments
        SET reply_content = '',
            updated_at = NOW()
        WHERE reply_content IS NULL
        """
    )
    op.execute(
        """
        ALTER TABLE komari_chat_reply_fulfillments
        ALTER COLUMN reply_content SET NOT NULL
        """
    )

    op.execute(
        "DROP INDEX IF EXISTS idx_reply_fulfillment_terminal_cleanup"
    )
    op.execute(
        """
        ALTER TABLE komari_chat_reply_fulfillments
        DROP COLUMN idempotency_evidence_cleared_at
        """
    )
