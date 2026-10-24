"""TSK-187 已确认送达观测验收：既有送达承诺 → 判定自有近期上下文。

断言穿过真实 ``ReplyCommitmentWorkflow``（既有送达承诺编排）与真实判定插件
顶层 ``observe_delivered_reply`` / ``JevDecisionEngine``，只用一次性隔离库与
隔离 Redis；外部 I/O（父子履约仓储、记忆 Redis、好感度、预占）使用内存替身。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import pytest

import komari_bot.plugins.komari_decision as decision_module
from komari_bot.plugins.komari_decision.services import jev_dispatch
from tests.komari_decision.jev_test_support import (
    DecisionHarness,
    HarnessOptions,
    create_scratch_database,
    drop_scratch_database,
    note_scope,
    skip_without_services,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.asyncio

_BOT_SELF_ID = 12345
_PLATFORM_MESSAGE_ID = 424242
_REPLY_TEXT = "已确认的回复正文"
_HANDOFF = "被引用的原文"

workflow_module = import_module(
    "komari_bot.plugins.komari_chat.services.reply_commitment_workflow"
)


@pytest.fixture(scope="module")
def decision_database() -> Iterator[str]:
    skip_without_services()
    url = asyncio.run(create_scratch_database(f"_tsk187obs{uuid4().hex[:8]}"))
    yield url
    asyncio.run(drop_scratch_database(url))


class _Repository:
    """父子履约仓储替身：只驱动既有承诺执行路径。"""

    def __init__(self, record: dict[str, Any], payload: dict[str, Any]) -> None:
        self.record = record
        self.payload = payload
        self.completed_commitments: list[str] = []
        self.failed_commitments: list[str] = []
        self.parent_completed = False
        self.renew_count = 0

    async def claim_pending(
        self, *, owner_token: str, limit: int, lease_seconds: int
    ) -> list[dict[str, Any]]:
        del owner_token, limit, lease_seconds
        return []

    async def claim_lease(
        self, fulfillment_id: str, *, owner_token: str, lease_seconds: int
    ) -> dict[str, Any] | None:
        del fulfillment_id, owner_token, lease_seconds
        return dict(self.record)

    async def load_claimed_commitments(
        self, fulfillment_id: str, *, owner_token: str
    ) -> list[dict[str, Any]] | None:
        del fulfillment_id, owner_token
        return [
            {
                "commitment_type": "assistant_reply_history",
                "payload": dict(self.payload),
            }
        ]

    async def load_parent_attribution(
        self, fulfillment_id: str
    ) -> dict[str, Any] | None:
        del fulfillment_id
        return {"fulfillment_id": self.record["fulfillment_id"], "group_id": self.record["group_id"]}

    async def mark_commitment_completed(
        self, fulfillment_id: str, *, commitment_type: str, owner_token: str
    ) -> bool:
        del fulfillment_id, owner_token
        self.completed_commitments.append(commitment_type)
        return True

    async def mark_commitment_failed(
        self,
        fulfillment_id: str,
        *,
        commitment_type: str,
        owner_token: str,
        error_code: str,
        max_attempts: int,
        retry_base_seconds: int,
        retry_max_seconds: int,
    ) -> str | None:
        del (
            fulfillment_id,
            owner_token,
            error_code,
            max_attempts,
            retry_base_seconds,
            retry_max_seconds,
        )
        self.failed_commitments.append(commitment_type)
        return "FAILED"

    async def complete_fulfillment(
        self, fulfillment_id: str, *, owner_token: str
    ) -> bool:
        del fulfillment_id, owner_token
        self.parent_completed = True
        return True

    async def renew_lease(
        self, fulfillment_id: str, *, owner_token: str, lease_seconds: int
    ) -> bool:
        del fulfillment_id, owner_token, lease_seconds
        self.renew_count += 1
        return True

    async def release_lease(self, fulfillment_id: str, *, owner_token: str) -> bool:
        del fulfillment_id, owner_token
        return True

    async def claim_terminal_cleanup_candidates(
        self,
        *,
        owner_token: str,
        limit: int,
        lease_seconds: int,
        protection_days: int,
    ) -> list[dict[str, Any]]:
        del owner_token, limit, lease_seconds, protection_days
        return []

    async def mark_idempotency_evidence_cleared(
        self, fulfillment_id: str, *, owner_token: str
    ) -> bool:
        del fulfillment_id, owner_token
        return True

    async def delete_terminal_tombstone(
        self, fulfillment_id: str, *, owner_token: str
    ) -> bool:
        del fulfillment_id, owner_token
        return True


class _Redis:
    """记忆历史写入的内存替身：按既有 operation_id 语义幂等。"""

    def __init__(self) -> None:
        self.pushed: list[tuple[str, Any]] = []
        self._operations: set[str] = set()

    async def push_message_once(
        self,
        group_id: str,
        message: Any,
        *,
        operation_id: str,
        dedupe_ttl_seconds: int | None,
    ) -> None:
        del dedupe_ttl_seconds
        if operation_id in self._operations:
            return
        self._operations.add(operation_id)
        self.pushed.append((group_id, message))

    async def push_global_interaction_once(self, **_kwargs: object) -> None:
        return None

    async def delete_chat_commit_evidence(self, _fulfillment_id: str) -> None:
        return None


class _ProactiveReservation:
    async def confirm(self, *_args: object, **_kwargs: object) -> None:
        return None


class _UserData:
    async def adjust_user_favorability(
        self, *_args: object, **_kwargs: object
    ) -> None:
        return None

    async def delete_favorability_operation(self, _operation_id: str) -> None:
        return None


class _CommitWorld:
    def __init__(
        self,
        *,
        group_id: int,
        platform_message_id: object = str(_PLATFORM_MESSAGE_ID),
        adapter_name: object = "OneBot V11",
        delivered_at: object | None = None,
        reply_content: object = _REPLY_TEXT,
    ) -> None:
        self.group_id = group_id
        self.fulfillment_id = f"tsk187-{uuid4().hex}"
        self.delivered_at = (
            delivered_at
            if delivered_at is not None
            else datetime.now(UTC) - timedelta(seconds=5)
        )
        self.record = {
            "fulfillment_id": self.fulfillment_id,
            "group_id": str(group_id),
            "bot_self_id": str(_BOT_SELF_ID),
            "adapter_name": adapter_name,
            "platform_message_id": platform_message_id,
            "delivered_at": self.delivered_at,
        }
        self.payload = {
            "group_id": str(group_id),
            "bot_nickname": "小鞠",
            "reply_content": reply_content,
            "reply_timestamp": 123.0,
        }
        self.repository = _Repository(self.record, self.payload)
        self.redis = _Redis()
        self.workflow = workflow_module.ReplyCommitmentWorkflow(
            repository=self.repository,
            redis=self.redis,
            proactive_reservation=_ProactiveReservation(),
            user_data=_UserData(),
            config_getter=self._config,
        )

    @staticmethod
    def _config() -> SimpleNamespace:
        return SimpleNamespace(
            reply_fulfillment_lease_seconds=30,
            reply_fulfillment_batch_size=10,
            reply_fulfillment_max_attempts=3,
            reply_fulfillment_retry_base_seconds=1,
            reply_fulfillment_retry_max_seconds=10,
            reply_fulfillment_tombstone_retention_days=30,
        )


async def _later_result(
    world: _CommitWorld,
    harness: DecisionHarness,
    *,
    reference_id: int = _PLATFORM_MESSAGE_ID,
) -> Any:
    """用真实入站投影评估一条引用该平台消息 id 的后续目标。"""
    from nonebot.adapters.onebot.v11 import Message, MessageSegment
    from nonebot.adapters.onebot.v11.event import GroupMessageEvent, Sender

    assert harness.decision is not None
    event = GroupMessageEvent.model_construct(
        time=int(datetime.now(UTC).timestamp()),
        self_id=_BOT_SELF_ID,
        post_type="message",
        sub_type="normal",
        user_id=67890,
        message_type="group",
        message_id=uuid4().int % 2**31,
        message=Message(MessageSegment.reply(reference_id) + _HANDOFF),
        original_message=Message(MessageSegment.reply(reference_id) + _HANDOFF),
        raw_message="raw",
        font=0,
        sender=Sender.model_construct(user_id=67890, nickname="群友"),
        to_me=False,
        reply=None,
        group_id=world.group_id,
        anonymous=None,
    )
    state: dict[Any, Any] = {}
    await jev_dispatch.prepare_decision_dispatch(
        bot=cast("Any", object()),
        event=cast("Any", event),
        state=cast("Any", state),
    )
    return await _evaluate(harness, event, state)


async def _evaluate(
    harness: DecisionHarness,
    event: Any,
    state: dict[Any, Any],
) -> Any:
    """直接使用真实投影结果评估（Bot 身份由事件自账号提供）。"""
    from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
    from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot

    adapter = cast("Any", OneBotV11Adapter.__new__(OneBotV11Adapter))
    bot = OneBotV11Bot(adapter, str(_BOT_SELF_ID))
    dispatch = jev_dispatch.get_decision_dispatch(
        bot, cast("Any", event), cast("Any", state)
    )
    assert harness.decision is not None
    return await dispatch.evaluate(harness.decision, summary_candidate=False)


async def _conversation_texts(harness: DecisionHarness) -> list[tuple[str, bool]]:
    conversation = harness.transport.state(-1)["untrusted_group_conversation"]
    return [
        (
            cast("str", conversation["messages"][key]["text"]),
            cast("bool", conversation["messages"][key]["is_bot"]),
        )
        for key in conversation["timeline"]
    ]


@pytest.mark.parametrize("recoveries", [1, 2])
async def test_confirmed_delivery_enters_later_decision_context_with_real_id(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
    recoveries: int,
) -> None:
    group_id = 800_000 + uuid4().int % 100_000
    note_scope("onebot_v11", str(_BOT_SELF_ID), str(group_id))
    world = _CommitWorld(group_id=group_id)
    async with DecisionHarness(decision_database, HarnessOptions()) as harness:
        runtime = decision_module.get_decision_runtime()
        monkeypatch.setattr(runtime, "_engine", harness.decision)
        monkeypatch.setattr(runtime, "_closed", False)

        for _ in range(recoveries):
            assert await world.workflow.recover_fulfillment(world.fulfillment_id)

        # 既有恢复可以重复执行同一条已完成的记忆历史承诺；观测因此幂等重复。
        assert world.repository.completed_commitments.count(
            "assistant_reply_history"
        ) == recoveries
        assert world.repository.parent_completed is True
        assert world.repository.failed_commitments == []
        assert [message.content for _, message in world.redis.pushed] == [_REPLY_TEXT]

        result = await _later_result(world, harness)

        # 真实平台消息 id 已在近期窗口内：显式引用边直接复用，无需补取。
        assert result.references.missing_count == 0
        assert result.references.complete is True
        assert harness.reference_calls == []
        rendered = await _conversation_texts(harness)
        bot_entries = [text for text, is_bot in rendered if is_bot]
        assert bot_entries == [_REPLY_TEXT]
        assert (_HANDOFF, False) in rendered
    note_scope("onebot_v11", str(_BOT_SELF_ID), str(group_id))


@pytest.mark.parametrize(
    ("overrides", "restrict_group"),
    [
        ({"platform_message_id": None}, False),
        ({"platform_message_id": "not-a-number"}, False),
        ({"platform_message_id": 2**31}, False),
        ({"adapter_name": "QQ"}, False),
        ({"delivered_at": datetime(2026, 1, 1)}, False),  # noqa: DTZ001 - 故意无时区
        ({}, True),
    ],
)
async def test_untrusted_delivery_evidence_does_not_enter_decision_context(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
    restrict_group: bool,  # noqa: FBT001 - parametrize 布尔断言
) -> None:
    group_id = 800_000 + uuid4().int % 100_000
    note_scope("onebot_v11", str(_BOT_SELF_ID), str(group_id))
    world = _CommitWorld(group_id=group_id, **overrides)  # type: ignore[arg-type]
    if restrict_group:
        monkeypatch.setattr(jev_dispatch, "group_business_admitted", lambda _g: False)
    async with DecisionHarness(decision_database, HarnessOptions()) as harness:
        runtime = decision_module.get_decision_runtime()
        monkeypatch.setattr(runtime, "_engine", harness.decision)
        monkeypatch.setattr(runtime, "_closed", False)

        assert await world.workflow.recover_fulfillment(world.fulfillment_id)
        assert world.repository.completed_commitments == ["assistant_reply_history"]

        result = await _later_result(world, harness)

        assert result.references.resolved_count == 0
        assert result.references.missing_count == 1
        assert harness.reference_calls != []
        rendered = await _conversation_texts(harness)
        assert all(text != _REPLY_TEXT for text, _is_bot in rendered)
    note_scope("onebot_v11", str(_BOT_SELF_ID), str(group_id))


async def test_observation_failure_does_not_fail_commitment(
    decision_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group_id = 800_000 + uuid4().int % 100_000
    note_scope("onebot_v11", str(_BOT_SELF_ID), str(group_id))
    world = _CommitWorld(group_id=group_id)

    async def _fail(**_kwargs: object) -> None:
        msg = "synthetic observation failure"
        raise RuntimeError(msg)

    monkeypatch.setattr(decision_module, "observe_delivered_reply", _fail)
    async with DecisionHarness(decision_database, HarnessOptions()) as harness:
        runtime = decision_module.get_decision_runtime()
        monkeypatch.setattr(runtime, "_engine", harness.decision)
        monkeypatch.setattr(runtime, "_closed", False)

        assert await world.workflow.recover_fulfillment(world.fulfillment_id)

        # 观测失败不改变已完成的记忆历史承诺，也不进入失败重试预算。
        assert world.repository.completed_commitments == ["assistant_reply_history"]
        assert world.repository.parent_completed is True
        assert world.repository.failed_commitments == []
        assert [message.content for _, message in world.redis.pushed] == [_REPLY_TEXT]
    note_scope("onebot_v11", str(_BOT_SELF_ID), str(group_id))
