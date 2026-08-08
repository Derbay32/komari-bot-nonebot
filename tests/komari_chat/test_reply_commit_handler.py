"""聊天送达后 outbox 编排测试。"""

from __future__ import annotations

import asyncio
import time
from functools import partial
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
        # 可翻转的失败行为：续租/子步骤确认失败、指定子步骤执行时让出事件循环
        self.renew_lease_result = True
        self.mark_step_result = True
        self.yield_once_on_step: str | None = None
        self.mark_step_calls = 0
        self.cleanup_calls = 0
        self.failure_log: list[dict[str, str]] = []

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
        return self.renew_lease_result

    async def mark_step(
        self,
        operation_id: str,
        *,
        owner_token: str,
        step: str,
    ) -> bool:
        del owner_token
        self.mark_step_calls += 1
        if not self.mark_step_result:
            return False
        if self.yield_once_on_step is not None and step == self.yield_once_on_step:
            # 让出事件循环，使心跳任务有机会置位 lost 事件
            await asyncio.sleep(0)
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
        *,
        owner_token: str,
        error_code: str,
        max_attempts: int,
        retry_base_seconds: int,
    ) -> str:
        del owner_token, max_attempts, retry_base_seconds
        self.failure_log.append(
            {"operation_id": operation_id, "error_code": error_code}
        )
        self.records[operation_id]["status"] = "DELIVERED"
        return "DELIVERED"

    async def cleanup_tombstones(self, *, retention_days: int) -> int:
        del retention_days
        self.cleanup_calls += 1
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
        self.cleanup_favorability_calls = 0

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
        self.cleanup_favorability_calls += 1
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


async def _heartbeat_lost_on_renew_failure(
    repository: _FakeReplyCommitRepository,
    operation_id: str,
    *,
    owner_token: str,
    lease_seconds: int,
    lost: asyncio.Event,
) -> None:
    """替身心跳：续租一次，失败（renew_lease 返回 False）即置位 lost。

    生产心跳以固定间隔轮询续租；测试中用单次续租替代，避免真实等待。
    """
    renewed = await repository.renew_lease(
        operation_id,
        owner_token=owner_token,
        lease_seconds=lease_seconds,
    )
    if not renewed:
        lost.set()


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


@pytest.mark.asyncio
async def test_mark_reply_commit_step_raises_when_mark_step_returns_false(
    handler_module: Any,
) -> None:
    """mark_step 返回 False 时 _mark_reply_commit_step 抛错，且不落记录。"""
    handler, repository, _redis = _handler(handler_module)
    repository.mark_step_result = False
    lost = asyncio.Event()

    with pytest.raises(RuntimeError, match="子步骤确认失败"):
        await handler._mark_reply_commit_step(
            "op-1",
            owner_token="worker-1",
            step="favorability_applied",
            lease_lost=lost,
        )

    assert repository.mark_step_calls == 1
    assert "op-1" not in repository.records


@pytest.mark.asyncio
async def test_mark_reply_commit_step_raises_when_lease_lost_already_set(
    handler_module: Any,
) -> None:
    """租约事件已置位时 _mark_reply_commit_step 直接抛错，不触碰 repository。"""
    handler, repository, _redis = _handler(handler_module)
    lost = asyncio.Event()
    lost.set()

    with pytest.raises(RuntimeError, match="租约已丢失"):
        await handler._mark_reply_commit_step(
            "op-1",
            owner_token="worker-1",
            step="proactive_confirmed",
            lease_lost=lost,
        )

    assert repository.mark_step_calls == 0


@pytest.mark.asyncio
async def test_process_claimed_reply_commit_raises_when_lease_lost_before_complete(
    handler_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """续租失败置位 lost 后，全部子步骤完成时在 complete 前抛错。"""
    handler, repository, _redis = _handler(handler_module)
    user_data = _FakeUserData()
    monkeypatch.setattr(handler_module, "get_config", _config)
    monkeypatch.setattr(handler_module, "user_data_plugin", user_data)
    repository.renew_lease_result = False
    repository.yield_once_on_step = "interaction_stored"
    monkeypatch.setattr(
        handler,
        "_reply_commit_heartbeat",
        partial(_heartbeat_lost_on_renew_failure, repository),
    )
    pending = _pending_reply(handler_module, "reply-operation-3")

    assert await handler.prepare_pending_reply(pending) is True
    await repository.mark_delivered(
        pending.operation_id,
        platform_message_id="platform-1",
    )
    record = await repository.claim_operation(
        pending.operation_id,
        owner_token="worker-1",
        lease_seconds=60,
    )
    assert record is not None

    with pytest.raises(RuntimeError, match="完成前租约已丢失"):
        await handler._process_claimed_reply_commit(record, owner_token="worker-1")

    # complete 未被调用：record 保持 PROCESSING，四个子步骤均已落列
    record_state = repository.records[pending.operation_id]
    assert record_state["status"] == "PROCESSING"
    assert record_state["interaction_stored_at"] is not None


@pytest.mark.asyncio
async def test_retry_pending_reply_commits_triggers_hourly_cleanup(
    handler_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """距上次清理超过一小时时，retry 触发 tombstone 与好感度台账清理。"""
    handler, repository, _redis = _handler(handler_module)
    user_data = _FakeUserData()
    monkeypatch.setattr(handler_module, "get_config", _config)
    monkeypatch.setattr(handler_module, "user_data_plugin", user_data)
    handler._last_reply_commit_cleanup = time.monotonic() - 7200
    pending = _pending_reply(handler_module, "reply-operation-7")

    assert await handler.prepare_pending_reply(pending) is True
    await repository.mark_delivered(
        pending.operation_id,
        platform_message_id="platform-1",
    )

    assert await handler.retry_pending_reply_commits() == 1

    assert repository.cleanup_calls == 1
    assert user_data.cleanup_favorability_calls == 1
    assert repository.records[pending.operation_id]["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_retry_pending_reply_commits_skips_cleanup_within_hour(
    handler_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一小时内已清理过时，retry 不再触发 cleanup。"""
    handler, repository, _redis = _handler(handler_module)
    user_data = _FakeUserData()
    monkeypatch.setattr(handler_module, "get_config", _config)
    monkeypatch.setattr(handler_module, "user_data_plugin", user_data)
    handler._last_reply_commit_cleanup = time.monotonic()

    assert await handler.retry_pending_reply_commits() == 0

    assert repository.cleanup_calls == 0
    assert user_data.cleanup_favorability_calls == 0


@pytest.mark.asyncio
async def test_finish_claimed_reply_commit_marks_failure_on_step_error(
    handler_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """子步骤确认失败时 _finish_claimed_reply_commit 记失败并退回 DELIVERED。"""
    handler, repository, _redis = _handler(handler_module)
    user_data = _FakeUserData()
    monkeypatch.setattr(handler_module, "get_config", _config)
    monkeypatch.setattr(handler_module, "user_data_plugin", user_data)
    pending = _pending_reply(handler_module, "reply-operation-4")
    assert await handler.prepare_pending_reply(pending) is True
    await repository.mark_delivered(
        pending.operation_id,
        platform_message_id="platform-1",
    )
    record = await repository.claim_operation(
        pending.operation_id,
        owner_token="worker-1",
        lease_seconds=60,
    )
    assert record is not None
    repository.mark_step_result = False

    assert (
        await handler._finish_claimed_reply_commit(record, owner_token="worker-1")
        is False
    )

    assert repository.failure_log[-1] == {
        "operation_id": pending.operation_id,
        "error_code": "RuntimeError",
    }
    assert repository.records[pending.operation_id]["status"] == "DELIVERED"
