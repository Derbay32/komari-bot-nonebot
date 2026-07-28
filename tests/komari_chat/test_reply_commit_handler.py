"""聊天送达后 outbox 编排测试。"""

from __future__ import annotations

from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from komari_bot.plugins.komari_memory.services.redis_manager import MessageSchema

if TYPE_CHECKING:
    from nonebug import App


class _FakeReplyCommitRepository:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}

    async def prepare(self, payload: Any) -> bool:
        if payload.operation_id in self.records:
            return False
        self.records[payload.operation_id] = {
            "operation_id": payload.operation_id,
            "request_trace_id": payload.request_trace_id,
            "source_message_id": payload.source_message_id,
            "group_id": payload.group_id,
            "user_id": payload.user_id,
            "user_nickname": payload.user_nickname,
            "bot_nickname": payload.bot_nickname,
            "reply_content": payload.reply_content,
            "reply_timestamp": payload.reply_timestamp,
            "favorability_delta": payload.favorability_delta,
            "favorability_reason": payload.favorability_reason,
            "interaction_history": payload.interaction_history,
            "proactive_reservation_id": payload.proactive_reservation_id,
            "proactive_cooldown_seconds": payload.proactive_cooldown_seconds,
            "global_interaction_enabled": payload.global_interaction_enabled,
            "global_interaction_trigger_size": payload.global_interaction_trigger_size,
            "status": "PREPARED",
            "proactive_confirmed_at": None,
            "favorability_applied_at": None,
            "ai_history_stored_at": None,
            "interaction_stored_at": None,
            "attempt_count": 0,
        }
        return True

    async def mark_delivered(
        self,
        operation_id: str,
        *,
        platform_message_id: str | None = None,
    ) -> bool:
        record = self.records[operation_id]
        record["status"] = "DELIVERED"
        record["platform_message_id"] = platform_message_id
        return True

    async def claim_operation(
        self,
        operation_id: str,
        *,
        owner_token: str,
        lease_seconds: int,
    ) -> dict[str, Any] | None:
        del owner_token, lease_seconds
        record = self.records[operation_id]
        if record["status"] != "DELIVERED":
            return None
        record["status"] = "PROCESSING"
        record["attempt_count"] = int(record["attempt_count"]) + 1
        return record

    async def claim_pending(
        self,
        *,
        owner_token: str,
        limit: int,
        lease_seconds: int,
    ) -> list[dict[str, Any]]:
        del owner_token, lease_seconds
        claimed: list[dict[str, Any]] = []
        for record in self.records.values():
            if record["status"] == "DELIVERED" and len(claimed) < limit:
                record["status"] = "PROCESSING"
                record["attempt_count"] = int(record["attempt_count"]) + 1
                claimed.append(record)
        return claimed

    async def renew_lease(self, *_args: object, **_kwargs: object) -> bool:
        return True

    async def mark_step(
        self,
        operation_id: str,
        *,
        owner_token: str,
        step: str,
    ) -> bool:
        del owner_token
        columns = {
            "proactive_confirmed": "proactive_confirmed_at",
            "favorability_applied": "favorability_applied_at",
            "ai_history_stored": "ai_history_stored_at",
            "interaction_stored": "interaction_stored_at",
        }
        self.records[operation_id][columns[step]] = object()
        return True

    async def complete(self, operation_id: str, *, owner_token: str) -> bool:
        del owner_token
        record = self.records[operation_id]
        assert all(
            record[column] is not None
            for column in (
                "proactive_confirmed_at",
                "favorability_applied_at",
                "ai_history_stored_at",
                "interaction_stored_at",
            )
        )
        record["status"] = "COMPLETED"
        return True

    async def mark_failure(
        self,
        operation_id: str,
        **_kwargs: object,
    ) -> str:
        self.records[operation_id]["status"] = "DELIVERED"
        return "DELIVERED"

    async def cleanup_tombstones(self, *, retention_days: int) -> int:
        del retention_days
        return 0


class _FakeRedis:
    def __init__(self) -> None:
        self.confirmed: set[str] = set()
        self.ai_operations: set[str] = set()
        self.interaction_operations: set[str] = set()
        self.fail_ai_once = False

    async def confirm_proactive_reply(
        self,
        _group_id: str,
        reservation_id: str,
        *,
        cooldown_seconds: int,
    ) -> None:
        del cooldown_seconds
        self.confirmed.add(reservation_id)

    async def push_message_once(
        self,
        _group_id: str,
        _message: MessageSchema,
        *,
        operation_id: str,
        dedupe_ttl_seconds: int,
    ) -> bool:
        del dedupe_ttl_seconds
        if self.fail_ai_once:
            self.fail_ai_once = False
            msg = "模拟 Redis 短暂故障"
            raise RuntimeError(msg)
        inserted = operation_id not in self.ai_operations
        self.ai_operations.add(operation_id)
        return inserted

    async def push_global_interaction_once(
        self,
        *,
        user_id: str,
        record: dict[str, object],
        trigger_size: int,
        operation_id: str,
        dedupe_ttl_seconds: int,
    ) -> bool:
        del user_id, record, trigger_size, dedupe_ttl_seconds
        inserted = operation_id not in self.interaction_operations
        self.interaction_operations.add(operation_id)
        return inserted


class _FakeUserData:
    def __init__(self) -> None:
        self.operations: set[str] = set()
        self.application_count = 0

    async def adjust_user_favorability(
        self,
        _user_id: str,
        _delta: int,
        *,
        operation_id: str,
    ) -> SimpleNamespace:
        if operation_id not in self.operations:
            self.operations.add(operation_id)
            self.application_count += 1
        return SimpleNamespace(before=0, delta=_delta, after=_delta)

    async def cleanup_favorability_operations(self, *, retention_days: int) -> int:
        del retention_days
        return 0


@pytest.fixture
def handler_module(app: App) -> Any:
    del app
    return import_module("komari_bot.plugins.komari_chat.handlers.message_handler")


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        proactive_cooldown=300,
        global_interaction_enabled=True,
        global_interaction_trigger_size=20,
        reply_commit_lease_seconds=60,
        reply_commit_max_attempts=5,
        reply_commit_retry_base_seconds=1,
        reply_commit_batch_size=20,
        reply_commit_tombstone_retention_days=30,
    )


def _pending_reply(module: Any, operation_id: str) -> Any:
    reply_result = module.ReplyResult(
        content="回复正文",
        interaction_history={"event": "发言", "result": "回复", "emotion": "平静"},
        favorability_delta=1,
        favorability_reason="正常互动",
    )
    return module.PendingReply(
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
        reply_result=reply_result,
        force_reply=False,
        bot_nickname="小鞠",
        reason="score",
        reply_score=0.9,
        operation_id=operation_id,
        request_trace_id="chat-message-1",
        reply_timestamp=2.0,
        proactive_reservation_id="reservation-1",
    )


def _handler(module: Any) -> tuple[Any, _FakeReplyCommitRepository, _FakeRedis]:
    handler = module.MessageHandler.__new__(module.MessageHandler)
    repository = _FakeReplyCommitRepository()
    redis = _FakeRedis()
    handler.reply_commit_repository = repository
    handler.redis = redis
    handler._reply_commit_owner = "worker-1"
    handler._last_reply_commit_cleanup = 0.0
    return handler, repository, redis


@pytest.mark.asyncio
async def test_delivered_reply_commits_all_idempotent_steps(
    handler_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler, repository, redis = _handler(handler_module)
    user_data = _FakeUserData()
    monkeypatch.setattr(handler_module, "get_config", _config)
    monkeypatch.setattr(handler_module, "user_data_plugin", user_data)
    pending = _pending_reply(handler_module, "reply-operation-1")

    assert await handler.prepare_pending_reply(pending) is True
    await handler.commit_delivered_reply(pending)

    assert repository.records[pending.operation_id]["status"] == "COMPLETED"
    assert redis.confirmed == {"reservation-1"}
    assert redis.ai_operations == {pending.operation_id}
    assert redis.interaction_operations == {pending.operation_id}
    assert user_data.application_count == 1


@pytest.mark.asyncio
async def test_partial_commit_retries_without_reapplying_completed_step(
    handler_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler, repository, redis = _handler(handler_module)
    user_data = _FakeUserData()
    monkeypatch.setattr(handler_module, "get_config", _config)
    monkeypatch.setattr(handler_module, "user_data_plugin", user_data)
    pending = _pending_reply(handler_module, "reply-operation-2")
    redis.fail_ai_once = True

    assert await handler.prepare_pending_reply(pending) is True
    await handler.commit_delivered_reply(pending)

    record = repository.records[pending.operation_id]
    assert record["status"] == "DELIVERED"
    assert record["favorability_applied_at"] is not None
    assert record["ai_history_stored_at"] is None
    assert user_data.application_count == 1

    assert await handler.retry_pending_reply_commits() == 1
    assert record["status"] == "COMPLETED"
    assert user_data.application_count == 1
    assert redis.ai_operations == {pending.operation_id}
