"""TSK-276 command receipts and one-to-one reply fulfillment metadata."""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(name: str = "") -> None:
    if name:
        return

    op.execute(
        """
        CREATE TABLE komari_roulette_command_receipts (
            receipt_id TEXT PRIMARY KEY,
            app_id TEXT NOT NULL,
            group_openid TEXT NOT NULL,
            inbound_msg_id TEXT NOT NULL,
            fingerprint JSONB NOT NULL,
            result_code TEXT NOT NULL,
            game_id TEXT,
            state_revision INTEGER,
            turn_seq INTEGER,
            reply_projection JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_komari_roulette_command_receipts_message
                UNIQUE (app_id, group_openid, inbound_msg_id),
            CONSTRAINT ck_komari_roulette_command_receipts_revision
                CHECK (state_revision IS NULL OR state_revision >= 0),
            CONSTRAINT ck_komari_roulette_command_receipts_turn
                CHECK (turn_seq IS NULL OR turn_seq >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE komari_roulette_fulfillments (
            receipt_id TEXT PRIMARY KEY,
            state TEXT NOT NULL DEFAULT 'NOT_STARTED',
            platform_message_id TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT fk_komari_roulette_fulfillments_receipt
                FOREIGN KEY (receipt_id)
                REFERENCES komari_roulette_command_receipts (receipt_id)
                ON DELETE CASCADE,
            CONSTRAINT ck_komari_roulette_fulfillments_state
                CHECK (state IN ('NOT_STARTED', 'PENDING_CONFIRMATION',
                                 'DELIVERED', 'NOT_DELIVERED')),
            CONSTRAINT ck_komari_roulette_fulfillments_platform_id
                CHECK (state <> 'DELIVERED' OR platform_message_id IS NOT NULL)
        )
        """
    )


def downgrade(name: str = "") -> None:
    if name:
        return

    op.execute("DROP TABLE komari_roulette_fulfillments")
    op.execute("DROP TABLE komari_roulette_command_receipts")
