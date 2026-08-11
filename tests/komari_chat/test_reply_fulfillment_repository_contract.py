"""回复履约父子存储 adapter 的无数据库契约测试。"""

from __future__ import annotations

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

    assert "reply_fulfillment_repository" not in workflow_source
    assert "reply_fulfillment_repository" not in plugin_source
    assert "reply_commit_repository" in workflow_source
