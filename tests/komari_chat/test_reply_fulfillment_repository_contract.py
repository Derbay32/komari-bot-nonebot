"""回复履约父子存储 adapter 的无数据库契约测试。"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from importlib import import_module
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import pytest


def _repository_module() -> Any:
    return import_module(
        "komari_bot.plugins.komari_chat.repositories.reply_fulfillment_repository"
    )


def test_commitment_inputs_use_fixed_typed_payloads() -> None:
    """固定承诺只接受各自的强类型 payload，不接受任意 JSON 对象。"""
    module = _repository_module()
    expected_types = {
        "proactive_reply_confirmation",
        "favorability_adjustment",
        "assistant_reply_history",
        "interaction_history",
    }

    assert set(module.COMMITMENT_TYPES) == expected_types

    valid = module.ReplyCommitmentInput(
        commitment_type="favorability_adjustment",
        payload=module.FavorabilityAdjustmentPayload(
            user_id="user-1",
            delta=1,
            reason="正常互动",
        ),
    )
    assert valid.commitment_type == "favorability_adjustment"

    with pytest.raises((TypeError, ValueError)):
        module.ReplyCommitmentInput(
            commitment_type="favorability_adjustment",
            payload={"user_id": "user-1", "delta": 1},
        )

    with pytest.raises((TypeError, ValueError)):
        module.ReplyCommitmentInput(
            commitment_type="dynamic_callback",
            payload=valid.payload,
        )


def test_interaction_payload_copies_record_into_read_only_mapping() -> None:
    """冻结载荷不与调用方共享可变字典，也不允许事后原地改写。"""
    module = _repository_module()
    source = {"event": "发言", "result": "回复", "emotion": "平静"}
    payload = module.InteractionHistoryPayload(
        user_id="user-1",
        display_name="测试用户",
        trigger_size=20,
        reply_timestamp=2,
        trigger_message_id="message-1",
        record=source,
    )

    source["event"] = "被调用方篡改"

    assert isinstance(payload.record, MappingProxyType)
    assert payload.record["event"] == "发言"
    assert payload.reply_timestamp == 2.0
    with pytest.raises(TypeError):
        cast("dict[str, str]", payload.record)["event"] = "原地篡改"


def test_draft_requires_fixed_core_commitments_in_domain_order() -> None:
    """适用集合可省略可选承诺，但不能丢失固定核心或打乱顺序。"""
    module = _repository_module()
    favorability = module.ReplyCommitmentInput(
        commitment_type="favorability_adjustment",
        payload=module.FavorabilityAdjustmentPayload(
            user_id="user-1",
            delta=1,
            reason="正常互动",
        ),
    )
    history = module.ReplyCommitmentInput(
        commitment_type="assistant_reply_history",
        payload=module.AssistantReplyHistoryPayload(
            group_id="group-1",
            bot_nickname="小鞠",
            reply_content="回复正文",
            reply_timestamp=2.0,
        ),
    )
    fields = {
        "fulfillment_id": "reply-1",
        "payload_hash": "a" * 64,
        "request_trace_id": "trace-1",
        "trigger_message_id": "message-1",
        "trigger_user_id": "user-1",
        "group_id": "group-1",
        "bot_self_id": "bot-1",
        "adapter_name": "OneBot V11",
        "reply_target_message_id": "message-1",
        "reply_content": "回复正文",
    }

    valid = module.ReplyFulfillmentDraft(
        **fields,
        commitments=(favorability, history),
    )
    assert [item.commitment_type for item in valid.commitments] == [
        "favorability_adjustment",
        "assistant_reply_history",
    ]

    with pytest.raises(ValueError, match="必须包含"):
        module.ReplyFulfillmentDraft(**fields, commitments=(favorability,))
    with pytest.raises(ValueError, match="固定顺序"):
        module.ReplyFulfillmentDraft(
            **fields,
            commitments=(history, favorability),
        )


@pytest.mark.asyncio
async def test_claimed_commitments_decode_asyncpg_jsonb_text() -> None:
    """asyncpg 默认返回的 JSONB 文本必须解码后再进入领域载荷校验。"""
    module = _repository_module()

    class _Connection:
        async def fetchval(self, *_args: object) -> int:
            return 1

        async def fetch(self, *_args: object) -> list[dict[str, object]]:
            return [
                {
                    "commitment_type": "favorability_adjustment",
                    "payload": json.dumps(
                        {
                            "user_id": "user-1",
                            "delta": 1,
                            "reason": "正常互动",
                        },
                        ensure_ascii=False,
                    ),
                }
            ]

    class _Pool:
        @asynccontextmanager
        async def acquire(self) -> Any:
            yield _Connection()

    repository = module.ReplyFulfillmentRepository(_Pool())

    commitments = await repository.load_claimed_commitments(
        "reply-jsonb",
        owner_token="worker-1",
    )

    assert commitments == [
        {
            "commitment_type": "favorability_adjustment",
            "payload": {
                "user_id": "user-1",
                "delta": 1,
                "reason": "正常互动",
            },
        }
    ]


@pytest.mark.asyncio
async def test_reconcile_delivered_persists_late_platform_evidence() -> None:
    """已送达但缺平台 ID 时，后补证据必须原子持久化后再幂等返回。"""
    module = _repository_module()

    class _Connection:
        def __init__(self) -> None:
            self.update_calls = 0
            self.released = False

        async def fetchval(self, query: str, *_args: object) -> str | None:
            assert not self.released, "连接已归还"
            if "UPDATE komari_chat_reply_fulfillments" not in query:
                return None
            self.update_calls += 1
            return "reply-late" if self.update_calls == 2 else None

        async def fetchrow(self, *_args: object) -> dict[str, object]:
            assert not self.released, "连接已归还"
            return {
                "delivery_state": "DELIVERED",
                "platform_message_id": None,
            }

    connection = _Connection()

    class _Acquire:
        async def __aenter__(self) -> _Connection:
            connection.released = False
            return connection

        async def __aexit__(self, *_args: object) -> None:
            connection.released = True

    class _Pool:
        def acquire(self) -> _Acquire:
            return _Acquire()

    repository = module.ReplyFulfillmentRepository(_Pool())

    outcome = await repository.reconcile_delivered(
        "reply-late",
        platform_message_id="platform-late-1",
    )

    assert outcome == "idempotent"
    assert connection.update_calls == 2


@pytest.mark.asyncio
async def test_management_list_preserves_sql_derived_statuses() -> None:
    """安全投影不得在缺少内部送达状态时覆盖 SQL 已推导的状态。"""
    module = _repository_module()

    class _Connection:
        async def fetch(
            self,
            query: str,
            *_args: object,
        ) -> list[dict[str, object]]:
            if "WITH derived AS" in query:
                return [
                    {
                        "fulfillment_id": "reply-pending",
                        "status": "pending_confirmation",
                        "total": 2,
                    },
                    {
                        "fulfillment_id": "reply-not-delivered",
                        "status": "not_delivered",
                        "total": 2,
                    },
                ]
            return []

    class _Acquire:
        async def __aenter__(self) -> _Connection:
            return _Connection()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _Pool:
        def acquire(self) -> _Acquire:
            return _Acquire()

    repository = module.ReplyFulfillmentRepository(_Pool())

    rows, total = await repository.list_for_management(
        status=None,
        limit=20,
        offset=0,
    )

    assert total == 2
    assert [row["status"] for row in rows] == [
        "pending_confirmation",
        "not_delivered",
    ]


def test_repository_contains_no_runtime_ddl() -> None:
    """运行时 adapter 只操作迁移管理的表，绝不创建或修改 schema。"""
    module = _repository_module()
    source_path = Path(module.__file__ or "")
    source = source_path.read_text(encoding="utf-8").upper()

    assert "CREATE TABLE" not in source
    assert "ALTER TABLE" not in source
    assert "DROP TABLE" not in source


def test_new_adapter_is_not_wired_into_active_chat_path_yet() -> None:
    """contract 前只扩展履约能力；正常聊天继续使用旧 adapter，禁止双读写。"""
    project_root = Path(__file__).resolve().parents[2]
    workflow_source = (
        project_root
        / "komari_bot/plugins/komari_chat/services/reply_fulfillment_workflow.py"
    ).read_text(encoding="utf-8")
    plugin_source = (
        project_root / "komari_bot/plugins/komari_chat/__init__.py"
    ).read_text(encoding="utf-8")
    handler_source = (
        project_root / "komari_bot/plugins/komari_chat/handlers/message_handler.py"
    ).read_text(encoding="utf-8")

    assert "reply_fulfillment_repository" not in workflow_source
    assert "reply_fulfillment_repository" not in plugin_source
    assert "reply_fulfillment_repository" not in handler_source
    assert "reply_commit_repository" in workflow_source
    assert "_LegacyReplyFulfillmentRepository(ReplyCommitRepository(pg_pool))" in (
        workflow_source
    )
