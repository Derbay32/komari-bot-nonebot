"""父表行键常量单源化验收测试 (TSK-111)。

真实仓储 ``RETURNING parent.*`` 的父表键集由共享模块
``tests.komari_chat.fulfillment_row_keys`` 的 ``PARENT_ROW_KEYS``
唯一权威定义；``test_reply_fulfillment_workflow`` 与
``test_reply_delivery_workflow`` 都必须 from-import 该常量，不得各自
保留 ``_PARENT_ROW_KEYS`` 私有副本。本文件把完整键列表以字面量钉在
断言里（顺序敏感），并断言两个测试模块命名空间与共享常量是同一对象。
"""

from __future__ import annotations

from tests.komari_chat import (
    test_reply_delivery_workflow,
    test_reply_fulfillment_workflow,
)
from tests.komari_chat.fulfillment_row_keys import PARENT_ROW_KEYS


def test_shared_module_exports_parent_row_keys_literal() -> None:
    """验收点 1：from-import 成功，值逐字等于既有键集（顺序敏感）。"""
    assert isinstance(PARENT_ROW_KEYS, tuple)
    assert all(isinstance(key, str) for key in PARENT_ROW_KEYS)
    # 与两个既有测试文件 ``_PARENT_ROW_KEYS`` 逐字一致的完整键集
    # （migrations 0006 建表 + 0008/0009 增补列），元组相等天然顺序敏感。
    assert PARENT_ROW_KEYS == (
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


def test_fulfillment_workflow_reuses_shared_parent_row_keys() -> None:
    """验收点 2a：履约 workflow 测试模块 from-import 同一常量对象。"""
    assert test_reply_fulfillment_workflow.PARENT_ROW_KEYS is PARENT_ROW_KEYS
    assert not hasattr(test_reply_fulfillment_workflow, "_PARENT_ROW_KEYS")


def test_delivery_workflow_reuses_shared_parent_row_keys() -> None:
    """验收点 2b：送达 workflow 测试模块 from-import 同一常量对象。"""
    assert test_reply_delivery_workflow.PARENT_ROW_KEYS is PARENT_ROW_KEYS
    assert not hasattr(test_reply_delivery_workflow, "_PARENT_ROW_KEYS")
