"""回复履约告警去重事实列。

迁移 ID: 0009
父迁移: 0008

本 revision 只在父子表上各加一个告警已发送时间戳：父表
``pending_confirmation_alerted_at`` 记录待确认转换首次被告警领取的
跨进程去重事实，子表 ``disposition_alerted_at`` 记录单个送达后承诺
首次进入待处置转换的去重事实；告警代际复位由运行时
``resume_failed_commitment`` 只清本子项时间戳，本 revision 不新增
其他表、不触碰旧 outbox、不切换正常聊天或新 worker。新增两个部分
索引覆盖告警领取扫描路径（未告警的待确认行与失败承诺）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence


revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(name: str = "") -> None:
    if name:
        return

    # 待确认转换去重事实：PENDING_CONFIRMATION 首次被告警领取后落时间戳
    op.execute(
        """
        ALTER TABLE komari_chat_reply_fulfillments
        ADD COLUMN pending_confirmation_alerted_at TIMESTAMPTZ
        """
    )

    # 单个承诺待处置转换去重事实：FAILED 首次被告警领取后落时间戳
    op.execute(
        """
        ALTER TABLE komari_chat_reply_fulfillment_commitments
        ADD COLUMN disposition_alerted_at TIMESTAMPTZ
        """
    )

    # 告警领取扫描路径：只覆盖未告警候选，随时间戳落库后自动退出索引
    op.execute(
        """
        CREATE INDEX idx_reply_fulfillment_pending_alert
        ON komari_chat_reply_fulfillments (prepared_at, fulfillment_id)
        WHERE delivery_state = 'PENDING_CONFIRMATION'
          AND pending_confirmation_alerted_at IS NULL
        """
    )
    op.execute(
        """
        CREATE INDEX idx_reply_fulfillment_disposition_alert
        ON komari_chat_reply_fulfillment_commitments (
            fulfillment_id,
            commitment_type
        )
        WHERE state = 'FAILED'
          AND disposition_alerted_at IS NULL
        """
    )


def downgrade(name: str = "") -> None:
    if name:
        return

    op.execute("DROP INDEX IF EXISTS idx_reply_fulfillment_disposition_alert")
    op.execute("DROP INDEX IF EXISTS idx_reply_fulfillment_pending_alert")
    op.execute(
        """
        ALTER TABLE komari_chat_reply_fulfillment_commitments
        DROP COLUMN disposition_alerted_at
        """
    )
    op.execute(
        """
        ALTER TABLE komari_chat_reply_fulfillments
        DROP COLUMN pending_confirmation_alerted_at
        """
    )
