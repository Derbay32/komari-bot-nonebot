"""JRHG 提示词构建测试。"""

from __future__ import annotations

import json
from importlib import import_module
from typing import Any

prompt_builder_module = import_module("komari_bot.plugins.jrhg.prompt_builder")


def _patch_template(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        prompt_builder_module,
        "get_template",
        lambda: {
            "system_prompt": "system",
            "memory_ack": "ack",
            "request_text": "固定请求",
            "output_instruction": "output",
            "cot_prefix": "cot",
        },
    )


def test_build_prompt_injects_interaction_history_and_request_text(
    monkeypatch: Any,
) -> None:
    _patch_template(monkeypatch)
    interaction_history = {
        "summary": "最近常找小鞠聊天",
        "records": [{"event": "投喂", "result": "开心"}],
    }

    messages = prompt_builder_module.build_prompt(
        daily_favor=88,
        interaction_history=interaction_history,
    )

    assert messages[0]["role"] == "system"
    assert "system" in messages[0]["content"]
    assert "<current_time>" in messages[0]["content"]
    assert "<favorability>88</favorability>" in messages[0]["content"]
    assert messages[1] == {
        "role": "user",
        "content": (
            f"<interaction_history>{json.dumps(interaction_history, ensure_ascii=False)}</interaction_history>\n"
            "<request_text>固定请求</request_text>"
        ),
    }
    assert messages[2] == {"role": "assistant", "content": "ack"}
    assert messages[3] == {"role": "system", "content": "output"}
    assert messages[4] == {"role": "assistant", "content": "cot"}


def test_build_prompt_uses_empty_interaction_history_when_missing(
    monkeypatch: Any,
) -> None:
    _patch_template(monkeypatch)

    messages = prompt_builder_module.build_prompt(
        daily_favor=40,
        interaction_history=None,
    )

    assert messages[1]["content"] == (
        "<interaction_history>"
        f"{json.dumps(prompt_builder_module.EMPTY_INTERACTION_HISTORY, ensure_ascii=False)}"
        "</interaction_history>\n"
        "<request_text>固定请求</request_text>"
    )
