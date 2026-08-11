"""回复送达事实与发送前恢复。

迁移 ID: 0007
父迁移: 0006

本 revision 为旧运行表 ``komari_chat_reply_commit_outbox`` 补齐正交送达
事实与恢复身份：``delivery_state`` 只表达送达生命周期，与旧
``status`` 的履约生命周期正交；``bot_self_id`` / ``adapter_name`` /
``reply_target_message_id`` 供发送前恢复按精确身份领取；
``prepared_at`` / ``send_started_at`` / ``not_delivered_at`` 记录送达
时间线。历史行保守 backfill：历史 PREPARED 按待确认建模（绝不变成可
自动重发的 NOT_STARTED），历史已送达 / 处理中 / 完成 / 失败映射
DELIVERED，历史 CANCELLED 映射 NOT_DELIVERED；只有新插入行才是
NOT_STARTED。

同时为 ``komari_chat_config`` 增加独立回复时效配置列，并调整 0006 父表
送达时间戳约束，使发送开始前过期的 NOT_DELIVERED 允许
``send_started_at IS NULL``。正常聊天路径仍由旧 outbox 承载，本 revision
不切换父子表。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence


revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(name: str = "") -> None:
    if name:
        return

    # 旧运行表补齐正交送达事实与恢复身份
    op.execute(
        """
        ALTER TABLE komari_chat_reply_commit_outbox
        ADD COLUMN delivery_state TEXT NOT NULL DEFAULT 'NOT_STARTED' CHECK (
            delivery_state IN (
                'NOT_STARTED',
                'PENDING_CONFIRMATION',
                'DELIVERED',
                'NOT_DELIVERED'
            )
        ),
        ADD COLUMN bot_self_id TEXT,
        ADD COLUMN adapter_name TEXT,
        ADD COLUMN reply_target_message_id TEXT,
        ADD COLUMN prepared_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        ADD COLUMN send_started_at TIMESTAMPTZ,
        ADD COLUMN not_delivered_at TIMESTAMPTZ
        """
    )

    # 历史行保守 backfill：PREPARED 按待确认建模，绝不进入可自动重发的
    # NOT_STARTED；已送达 / 处理中 / 完成 / 失败映射 DELIVERED；
    # CANCELLED 映射 NOT_DELIVERED。只有迁移后的新插入才是 NOT_STARTED。
    op.execute(
        """
        UPDATE komari_chat_reply_commit_outbox
        SET delivery_state = CASE status
                WHEN 'PREPARED' THEN 'PENDING_CONFIRMATION'
                WHEN 'CANCELLED' THEN 'NOT_DELIVERED'
                ELSE 'DELIVERED'
            END,
            prepared_at = COALESCE(prepared_at, created_at),
            send_started_at = CASE status
                WHEN 'CANCELLED' THEN send_started_at
                ELSE COALESCE(send_started_at, delivered_at, created_at)
            END
        """
    )

    # 独立回复时效配置列（默认 120 秒，范围 30..300）
    op.execute(
        """
        ALTER TABLE komari_chat_config
        ADD COLUMN reply_fulfillment_freshness_seconds INTEGER NOT NULL DEFAULT 120
            CONSTRAINT ck_komari_chat_config_reply_fulfillment_freshness
            CHECK (
                reply_fulfillment_freshness_seconds >= 30
                AND reply_fulfillment_freshness_seconds <= 300
            )
        """
    )

    # 调整 0006 父表送达时间戳约束：发送开始前过期的 NOT_DELIVERED
    # 允许 send_started_at IS NULL
    op.execute(
        """
        ALTER TABLE komari_chat_reply_fulfillments
        DROP CONSTRAINT ck_reply_fulfillment_delivery_timestamps
        """
    )
    op.execute(
        """
        ALTER TABLE komari_chat_reply_fulfillments
        ADD CONSTRAINT ck_reply_fulfillment_delivery_timestamps CHECK (
            (delivery_state = 'NOT_STARTED'
                AND send_started_at IS NULL
                AND delivered_at IS NULL
                AND not_delivered_at IS NULL)
            OR (delivery_state = 'PENDING_CONFIRMATION'
                AND send_started_at IS NOT NULL
                AND delivered_at IS NULL
                AND not_delivered_at IS NULL)
            OR (delivery_state = 'DELIVERED'
                AND send_started_at IS NOT NULL
                AND delivered_at IS NOT NULL
                AND not_delivered_at IS NULL)
            OR (delivery_state = 'NOT_DELIVERED'
                AND delivered_at IS NULL
                AND not_delivered_at IS NOT NULL)
        )
        """
    )


def downgrade(name: str = "") -> None:
    if name:
        return

    # 恢复 0006 原约束（NOT_DELIVERED 必须已有发送开始）
    op.execute(
        """
        ALTER TABLE komari_chat_reply_fulfillments
        DROP CONSTRAINT ck_reply_fulfillment_delivery_timestamps
        """
    )
    op.execute(
        """
        ALTER TABLE komari_chat_reply_fulfillments
        ADD CONSTRAINT ck_reply_fulfillment_delivery_timestamps CHECK (
            (delivery_state = 'NOT_STARTED'
                AND send_started_at IS NULL
                AND delivered_at IS NULL
                AND not_delivered_at IS NULL)
            OR (delivery_state = 'PENDING_CONFIRMATION'
                AND send_started_at IS NOT NULL
                AND delivered_at IS NULL
                AND not_delivered_at IS NULL)
            OR (delivery_state = 'DELIVERED'
                AND send_started_at IS NOT NULL
                AND delivered_at IS NOT NULL
                AND not_delivered_at IS NULL)
            OR (delivery_state = 'NOT_DELIVERED'
                AND send_started_at IS NOT NULL
                AND delivered_at IS NULL
                AND not_delivered_at IS NOT NULL)
        )
        """
    )

    op.execute(
        """
        ALTER TABLE komari_chat_config
        DROP CONSTRAINT ck_komari_chat_config_reply_fulfillment_freshness
        """
    )
    op.execute(
        "ALTER TABLE komari_chat_config "
        "DROP COLUMN reply_fulfillment_freshness_seconds"
    )

    op.execute(
        """
        ALTER TABLE komari_chat_reply_commit_outbox
        DROP COLUMN delivery_state,
        DROP COLUMN bot_self_id,
        DROP COLUMN adapter_name,
        DROP COLUMN reply_target_message_id,
        DROP COLUMN prepared_at,
        DROP COLUMN send_started_at,
        DROP COLUMN not_delivered_at
        """
    )
