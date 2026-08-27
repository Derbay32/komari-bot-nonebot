"""回复履约父子记录与原子状态约束。

迁移 ID: 0006
父迁移: 0005

本 revision 只建立回复履约的持久化事实模型。父记录保存回复准备与送达
事实，子记录保存四种固定的送达后承诺及其独立重试事实。运行时 adapter
只消费本 revision 建立的表，旧版 ``komari_chat_reply_commit_outbox`` 保留
给现有聊天路径继续使用。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence


revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(name: str = "") -> None:
    if name:
        return

    op.execute(
        """
        CREATE TABLE komari_chat_reply_fulfillments (
            fulfillment_id TEXT PRIMARY KEY,
            payload_hash TEXT NOT NULL,
            request_trace_id TEXT NOT NULL,
            trigger_message_id TEXT NOT NULL,
            trigger_user_id TEXT NOT NULL,
            group_id TEXT NOT NULL,
            bot_self_id TEXT NOT NULL,
            adapter_name TEXT NOT NULL,
            reply_target_message_id TEXT NOT NULL,
            reply_content TEXT NOT NULL,
            delivery_state TEXT NOT NULL CHECK (
                delivery_state IN (
                    'NOT_STARTED',
                    'PENDING_CONFIRMATION',
                    'DELIVERED',
                    'NOT_DELIVERED'
                )
            ),
            platform_message_id TEXT,
            prepared_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            send_started_at TIMESTAMPTZ,
            delivered_at TIMESTAMPTZ,
            not_delivered_at TIMESTAMPTZ,
            lease_owner TEXT,
            lease_expires_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT ck_reply_fulfillment_lease_pair CHECK (
                (lease_owner IS NULL) = (lease_expires_at IS NULL)
            ),
            CONSTRAINT ck_reply_fulfillment_delivery_timestamps CHECK (
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
        )
        """
    )
    op.execute(
        """
        CREATE TABLE komari_chat_reply_fulfillment_commitments (
            fulfillment_id TEXT NOT NULL,
            commitment_type TEXT NOT NULL CHECK (
                commitment_type IN (
                    'proactive_reply_confirmation',
                    'favorability_adjustment',
                    'assistant_reply_history',
                    'interaction_history'
                )
            ),
            state TEXT NOT NULL DEFAULT 'PENDING' CHECK (
                state IN ('PENDING', 'RETRY_WAIT', 'COMPLETED', 'FAILED')
            ),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            next_retry_at TIMESTAMPTZ,
            last_error_code TEXT,
            payload JSONB,
            completed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (fulfillment_id, commitment_type),
            CONSTRAINT fk_reply_fulfillment_commitment_parent
                FOREIGN KEY (fulfillment_id)
                REFERENCES komari_chat_reply_fulfillments(fulfillment_id)
                ON DELETE CASCADE,
            CONSTRAINT ck_reply_fulfillment_commitment_retry_fields CHECK (
                (state = 'RETRY_WAIT' AND next_retry_at IS NOT NULL)
                OR (state <> 'RETRY_WAIT' AND next_retry_at IS NULL)
            ),
            CONSTRAINT ck_reply_fulfillment_commitment_completion_fields CHECK (
                (state = 'COMPLETED' AND completed_at IS NOT NULL)
                OR (state <> 'COMPLETED' AND completed_at IS NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_reply_fulfillment_claim
        ON komari_chat_reply_fulfillments (
            delivery_state,
            lease_expires_at,
            delivered_at,
            prepared_at
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_reply_fulfillment_commitment_retry
        ON komari_chat_reply_fulfillment_commitments (
            state,
            next_retry_at,
            fulfillment_id
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_reply_fulfillment_commitment_parent_state
        ON komari_chat_reply_fulfillment_commitments (fulfillment_id, state)
        """
    )


def downgrade(name: str = "") -> None:
    if name:
        return

    op.execute("DROP TABLE komari_chat_reply_fulfillment_commitments")
    op.execute("DROP TABLE komari_chat_reply_fulfillments")
