"""回复履约父子存储 adapter 的无数据库契约测试。"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any

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


def test_repository_contains_no_runtime_ddl() -> None:
    """运行时 adapter 只操作迁移管理的表，绝不创建或修改 schema。"""
    module = _repository_module()
    source_path = Path(module.__file__ or "")
    source = source_path.read_text(encoding="utf-8").upper()

    assert "CREATE TABLE" not in source
    assert "ALTER TABLE" not in source
    assert "DROP TABLE" not in source


def test_new_adapter_is_not_wired_into_active_chat_path_yet() -> None:
    """TSK-79 只 expand 存储；正常聊天继续使用旧 adapter，禁止双读写。"""
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
