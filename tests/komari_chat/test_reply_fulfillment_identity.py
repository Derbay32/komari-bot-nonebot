"""回复履约身份与冻结责任的领域验收测试。"""

from __future__ import annotations

import asyncio
from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from komari_bot.plugins.komari_memory.services.redis_manager import MessageSchema

if TYPE_CHECKING:
    from nonebug import App


class _FrozenFulfillmentStore:
    """通过 workflow seam 观察首次冻结后的持久领域投影。"""

    def __init__(self) -> None:
        self.records: dict[str, Any] = {}
        self.terminal_states: dict[str, str] = {}

    async def has_fulfillment(self, fulfillment_id: str) -> bool:
        return fulfillment_id in self.records or fulfillment_id in self.terminal_states

    async def prepare(self, draft: Any) -> bool:
        fulfillment_id = draft.fulfillment_id
        if fulfillment_id in self.terminal_states:
            return False
        current = self.records.get(fulfillment_id)
        if current is None:
            self.records[fulfillment_id] = draft
            return True
        if current.payload_hash != draft.payload_hash:
            msg = f"履约冲突: {fulfillment_id}"
            raise ValueError(msg)
        return False

    async def mark_send_started(self, fulfillment_id: str) -> bool:
        return fulfillment_id in self.records


class _ReservationHandoff:
    """移交凭据 fake：冻结身份快照（与默认 pending 的预占身份一致）。

    身份测试只关心承诺载荷冻结，凭据只需承载与默认预占一致的
    group/reservation/cooldown 快照。
    """

    group_id = "group-1"
    reservation_id = "reservation-1"
    cooldown_seconds = 300

    async def release(self) -> bool:
        return True


@pytest.fixture
def workflow_module(app: App) -> Any:
    del app
    return import_module(
        "komari_bot.plugins.komari_chat.services.reply_fulfillment_workflow"
    )


def _config(*, global_interaction_enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        proactive_cooldown=300,
        global_interaction_enabled=global_interaction_enabled,
        global_interaction_trigger_size=20,
        reply_fulfillment_batch_size=20,
        reply_fulfillment_lease_seconds=60,
        reply_fulfillment_max_attempts=5,
        reply_fulfillment_retry_base_seconds=1,
        reply_fulfillment_retry_max_seconds=3600,
        reply_fulfillment_tombstone_retention_days=30,
        reply_fulfillment_freshness_seconds=120,
    )


def _pending_reply(
    workflow_module: Any,
    *,
    reply_content: str = "回复正文",
    bot_self_id: str = "bot-1",
    adapter_name: str = "onebot.v11",
    reply_target_message_id: str = "message-1",
    favorability_delta: int = 1,
    favorability_reason: str = "正常互动",
    interaction_history: dict[str, str] | None = None,
    proactive_reservation_id: str | None = "reservation-1",
    reply_timestamp: float = 2.0,
    request_trace_id: str = "chat-message-1",
    user_nickname: str = "测试用户",
) -> Any:
    handler_module = import_module(
        "komari_bot.plugins.komari_chat.handlers.message_handler"
    )
    message = MessageSchema(
        user_id="user-1",
        user_nickname=user_nickname,
        group_id="group-1",
        content="用户正文",
        timestamp=1.0,
        message_id="message-1",
    )
    fulfillment_id = workflow_module.build_reply_fulfillment_id(
        group_id=message.group_id,
        trigger_message_id=message.message_id,
        trigger_user_id=message.user_id,
    )
    return handler_module.PendingReply(
        reply=reply_content,
        reply_to_message_id=reply_target_message_id,
        message=message,
        reply_result=handler_module.ReplyResult(
            content=reply_content,
            interaction_history=interaction_history
            or {"event": "发言", "result": "回复", "emotion": "平静"},
            favorability_delta=favorability_delta,
            favorability_reason=favorability_reason,
        ),
        force_reply=False,
        bot_nickname="小鞠",
        bot_self_id=bot_self_id,
        adapter_name=adapter_name,
        reason="score",
        reply_score=0.9,
        fulfillment_id=fulfillment_id,
        request_trace_id=request_trace_id,
        reply_timestamp=reply_timestamp,
        proactive_reservation_id=proactive_reservation_id,
        proactive_handoff=(
            _ReservationHandoff() if proactive_reservation_id is not None else None
        ),
    )


def _workflow(
    workflow_module: Any,
    store: _FrozenFulfillmentStore,
    *,
    global_interaction_enabled: bool = True,
) -> Any:
    return workflow_module.ReplyFulfillmentWorkflow(
        repository=store,
        proactive_reservation=SimpleNamespace(),
        config_getter=lambda: _config(
            global_interaction_enabled=global_interaction_enabled
        ),
        recovery_senders_getter=dict,
        commitment_workflow=SimpleNamespace(),
        alert_service=SimpleNamespace(),
    )


async def _freeze_before_delivery(workflow: Any, pending_reply: Any) -> None:
    async def _cancel_after_freeze(_pending_reply: object) -> object:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await workflow.fulfill(
            pending_reply,
            send_reply=_cancel_after_freeze,
        )


def test_fulfillment_id_is_stable_and_versioned_by_trigger_identity(
    workflow_module: Any,
) -> None:
    build_id = workflow_module.build_reply_fulfillment_id

    assert build_id(
        group_id="group-1",
        trigger_message_id="message-1",
        trigger_user_id="user-1",
    ) == "reply-f4a5b040c6483b19aba869b1f19106c86bcb1de97e763ea2a51e3c0f74d77469"
    assert build_id(
        group_id="group-1",
        trigger_message_id="message-1",
        trigger_user_id="user-1",
    ) == build_id(
        group_id="group-1",
        trigger_message_id="message-1",
        trigger_user_id="user-1",
    )
    assert len(
        {
            build_id(
                group_id="group-1",
                trigger_message_id="message-1",
                trigger_user_id="user-1",
            ),
            build_id(
                group_id="group-2",
                trigger_message_id="message-1",
                trigger_user_id="user-1",
            ),
            build_id(
                group_id="group-1",
                trigger_message_id="message-2",
                trigger_user_id="user-1",
            ),
            build_id(
                group_id="group-1",
                trigger_message_id="message-1",
                trigger_user_id="user-2",
            ),
        }
    ) == 4


async def test_first_prepare_freezes_identity_target_and_applicable_commitments(
    workflow_module: Any,
) -> None:
    store = _FrozenFulfillmentStore()
    workflow = _workflow(workflow_module, store)
    pending = _pending_reply(workflow_module)

    await _freeze_before_delivery(workflow, pending)

    draft = store.records[pending.fulfillment_id]
    assert draft.fulfillment_id == pending.fulfillment_id
    assert draft.trigger_message_id == "message-1"
    assert draft.trigger_user_id == "user-1"
    assert draft.group_id == "group-1"
    assert draft.bot_self_id == "bot-1"
    assert draft.adapter_name == "onebot.v11"
    assert draft.reply_target_message_id == "message-1"
    assert draft.reply_content == "回复正文"
    assert [item.commitment_type for item in draft.commitments] == [
        "proactive_reply_confirmation",
        "favorability_adjustment",
        "assistant_reply_history",
        "interaction_history",
    ]
    assert [item.to_json() for item in draft.commitments] == [
        {
            "group_id": "group-1",
            "reservation_id": "reservation-1",
            "cooldown_seconds": 300,
        },
        {"user_id": "user-1", "delta": 1, "reason": "正常互动"},
        {
            "group_id": "group-1",
            "bot_nickname": "小鞠",
            "reply_content": "回复正文",
            "reply_timestamp": 2.0,
        },
        {
            "user_id": "user-1",
            "display_name": "测试用户",
            "trigger_size": 20,
            "reply_timestamp": 2.0,
            "trigger_message_id": "message-1",
            "record": {"event": "发言", "result": "回复", "emotion": "平静"},
        },
    ]


async def test_inapplicable_commitments_are_absent_and_stay_frozen(
    workflow_module: Any,
) -> None:
    store = _FrozenFulfillmentStore()
    workflow = _workflow(
        workflow_module,
        store,
        global_interaction_enabled=False,
    )
    pending = _pending_reply(
        workflow_module,
        proactive_reservation_id=None,
    )

    await _freeze_before_delivery(workflow, pending)

    draft = store.records[pending.fulfillment_id]
    assert [item.commitment_type for item in draft.commitments] == [
        "favorability_adjustment",
        "assistant_reply_history",
    ]

    replacement_workflow = _workflow(
        workflow_module,
        store,
        global_interaction_enabled=True,
    )
    with pytest.raises(ValueError, match="履约冲突"):
        await replacement_workflow.fulfill(
            pending,
            send_reply=lambda _pending: pytest.fail("重复履约不得重新发送"),
        )
    assert store.records[pending.fulfillment_id] is draft
    assert [item.commitment_type for item in draft.commitments] == [
        "favorability_adjustment",
        "assistant_reply_history",
    ]


async def test_payload_hash_is_canonical_and_covers_every_frozen_responsibility(
    workflow_module: Any,
) -> None:
    async def _payload_hash(
        *,
        global_interaction_enabled: bool = True,
        **pending_overrides: Any,
    ) -> str:
        store = _FrozenFulfillmentStore()
        workflow = _workflow(
            workflow_module,
            store,
            global_interaction_enabled=global_interaction_enabled,
        )
        pending = _pending_reply(workflow_module, **pending_overrides)
        await _freeze_before_delivery(workflow, pending)
        return str(store.records[pending.fulfillment_id].payload_hash)

    baseline = await _payload_hash()
    reordered = await _payload_hash(
        interaction_history={"emotion": "平静", "result": "回复", "event": "发言"}
    )

    assert len(baseline) == 64
    assert baseline == reordered
    assert baseline == await _payload_hash(request_trace_id="chat-redelivery-2")
    assert len(
        {
            baseline,
            await _payload_hash(reply_content="另一份回复"),
            await _payload_hash(bot_self_id="bot-2"),
            await _payload_hash(adapter_name="other-adapter"),
            await _payload_hash(reply_target_message_id="message-2"),
            await _payload_hash(favorability_delta=2),
            await _payload_hash(favorability_reason="特殊互动"),
            await _payload_hash(reply_timestamp=3.0),
            await _payload_hash(
                interaction_history={
                    "event": "追问",
                    "result": "解释",
                    "emotion": "认真",
                }
            ),
            await _payload_hash(user_nickname="另一个显示名"),
            await _payload_hash(proactive_reservation_id=None),
            await _payload_hash(global_interaction_enabled=False),
        }
    ) == 12


async def test_same_hash_is_idempotent_but_changed_payload_is_a_conflict(
    workflow_module: Any,
) -> None:
    store = _FrozenFulfillmentStore()
    workflow = _workflow(workflow_module, store)
    original = _pending_reply(workflow_module)
    await _freeze_before_delivery(workflow, original)
    frozen = store.records[original.fulfillment_id]
    send_count = 0

    async def _send(_pending: object) -> object:
        nonlocal send_count
        send_count += 1
        return object()

    assert await workflow.fulfill(
        _pending_reply(workflow_module),
        send_reply=_send,
    ) is False
    assert send_count == 0
    assert len(store.records) == 1

    with pytest.raises(ValueError, match="履约冲突"):
        await workflow.fulfill(
            _pending_reply(workflow_module, reply_content="冲突回复"),
            send_reply=_send,
        )

    assert send_count == 0
    assert store.records[original.fulfillment_id] is frozen
    assert frozen.reply_content == "回复正文"


@pytest.mark.parametrize("terminal_state", ["COMPLETED", "NOT_DELIVERED"])
async def test_terminal_identity_still_prevents_resend(
    workflow_module: Any,
    terminal_state: str,
) -> None:
    store = _FrozenFulfillmentStore()
    workflow = _workflow(workflow_module, store)
    pending = _pending_reply(workflow_module)
    await _freeze_before_delivery(workflow, pending)
    store.records.pop(pending.fulfillment_id)
    store.terminal_states[pending.fulfillment_id] = terminal_state

    assert await workflow.fulfill(
        _pending_reply(workflow_module),
        send_reply=lambda _pending: pytest.fail("终态履约不得重新发送"),
    ) is False
    assert pending.fulfillment_id not in store.records
    assert store.terminal_states[pending.fulfillment_id] == terminal_state
