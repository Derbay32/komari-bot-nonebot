"""履约测试替身共享的父表行键常量 (TSK-111)。

真实仓储 ``RETURNING parent.*`` 的父表键集
（migrations 0006 建表 + 0008/0009 增补列）；由
``test_reply_fulfillment_workflow`` 与 ``test_reply_delivery_workflow``
from-import，不得各自保留私有副本。
"""

from __future__ import annotations

PARENT_ROW_KEYS: tuple[str, ...] = (
    "fulfillment_id",
    "payload_hash",
    "request_trace_id",
    "trigger_message_id",
    "trigger_user_id",
    "group_id",
    "bot_self_id",
    "adapter_name",
    "reply_target_message_id",
    "reply_content",
    "delivery_state",
    "platform_message_id",
    "prepared_at",
    "send_started_at",
    "delivered_at",
    "not_delivered_at",
    "lease_owner",
    "lease_expires_at",
    "completed_at",
    "created_at",
    "updated_at",
    "idempotency_evidence_cleared_at",
    "pending_confirmation_alerted_at",
)
