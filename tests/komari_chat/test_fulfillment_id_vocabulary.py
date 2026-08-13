"""TSK-115: 履约身份词汇统一（fulfillment_id 取代 operation_id）验收测试。

验收对象（红线基线，实现落地前预期全红）：
- message_handler.PendingReply 字段 operation_id -> fulfillment_id（位置不变）
- reply_fulfillment_workflow._PendingReply Protocol property
  operation_id -> fulfillment_id
- is_duplicate_event 形参 operation_id -> fulfillment_id
  （ReplyFulfillmentQueryProtocol 与 ReplyFulfillmentWorkflow）
- MessageHandler._reply_operation_id -> _reply_fulfillment_id

下游既有幂等契约（Redis 防重、user_data 好感度、清理证据 API、
好感度账本 SQL 列名）不在本票范围，此处不涉及。
"""

from __future__ import annotations

import dataclasses
import inspect

from komari_bot.plugins.komari_chat.handlers import message_handler
from komari_bot.plugins.komari_chat.services import reply_fulfillment_workflow
from komari_bot.plugins.komari_memory.services.redis_manager import MessageSchema


def _pending_reply_field_names() -> set[str]:
    return {field.name for field in dataclasses.fields(message_handler.PendingReply)}


def _assert_second_parameter_named_fulfillment_id(func: object) -> None:
    assert callable(func)
    parameters = list(inspect.signature(func).parameters.values())
    assert parameters[1].name == "fulfillment_id"
    assert "operation_id" not in [parameter.name for parameter in parameters]


def test_pending_reply_dataclass_has_fulfillment_id_field() -> None:
    assert "fulfillment_id" in _pending_reply_field_names()


def test_pending_reply_dataclass_has_no_operation_id_field() -> None:
    assert "operation_id" not in _pending_reply_field_names()


def test_pending_reply_protocol_exposes_fulfillment_id_property() -> None:
    member = getattr(reply_fulfillment_workflow._PendingReply, "fulfillment_id", None)
    assert isinstance(member, property)


def test_pending_reply_protocol_has_no_operation_id_member() -> None:
    assert not hasattr(reply_fulfillment_workflow._PendingReply, "operation_id")


def test_query_protocol_is_duplicate_event_uses_fulfillment_id_parameter() -> None:
    _assert_second_parameter_named_fulfillment_id(
        reply_fulfillment_workflow.ReplyFulfillmentQueryProtocol.is_duplicate_event
    )


def test_workflow_is_duplicate_event_uses_fulfillment_id_parameter() -> None:
    _assert_second_parameter_named_fulfillment_id(
        reply_fulfillment_workflow.ReplyFulfillmentWorkflow.is_duplicate_event
    )


def test_message_handler_has_reply_fulfillment_id_helper() -> None:
    assert hasattr(message_handler.MessageHandler, "_reply_fulfillment_id")


def test_message_handler_has_no_reply_operation_id_helper() -> None:
    assert not hasattr(message_handler.MessageHandler, "_reply_operation_id")


def test_pending_reply_round_trips_fulfillment_id_value() -> None:
    """行为锚点：以 fulfillment_id= 关键字构造 PendingReply 并读回同值。

    构造所需其余字段照搬 tests/komari_chat/test_reply_fulfillment_workflow.py
    的 _pending_reply 辅助构造；实现落地前 PendingReply 只接受
    operation_id=，fulfillment_id= 会抛 TypeError（预期红灯）。
    """
    fulfillment_id = "chat-message-1-fulfillment"
    pending_reply = message_handler.PendingReply(
        reply="回复正文",
        reply_to_message_id="message-1",
        message=MessageSchema(
            user_id="user-1",
            user_nickname="测试用户",
            group_id="group-1",
            content="用户正文",
            timestamp=1.0,
            message_id="message-1",
        ),
        reply_result=message_handler.ReplyResult(
            content="回复正文",
            interaction_history={"event": "发言", "result": "回复", "emotion": "平静"},
            favorability_delta=1,
            favorability_reason="正常互动",
        ),
        force_reply=False,
        bot_nickname="小鞠",
        bot_self_id="bot-1",
        adapter_name="onebot.v11",
        reason="score",
        reply_score=0.9,
        fulfillment_id=fulfillment_id,
        request_trace_id="chat-message-1",
        reply_timestamp=2.0,
        proactive_reservation_id="reservation-1",
    )
    assert pending_reply.fulfillment_id == fulfillment_id
