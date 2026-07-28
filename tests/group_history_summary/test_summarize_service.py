"""群历史总结 LLM 调用测试。"""

from __future__ import annotations

import asyncio
from importlib import import_module
from typing import Any

from komari_bot.plugins.group_history_summary.history_service import HistoryMessage

summarize_service_module = import_module(
    "komari_bot.plugins.group_history_summary.summarize_service"
)


class _FakeLLMProvider:
    def __init__(self) -> None:
        self.message_calls: list[dict[str, Any]] = []

    async def generate_text_with_messages(self, **kwargs: Any) -> str:
        self.message_calls.append(kwargs)
        return "<content>今天主要聊了修复限流。</content>"


def test_summarize_history_messages_marks_group_history_summary_phase(
    monkeypatch: Any,
) -> None:
    fake_provider = _FakeLLMProvider()
    monkeypatch.setattr(summarize_service_module, "llm_provider", fake_provider)

    result = asyncio.run(
        summarize_service_module.summarize_history_messages(
            [
                HistoryMessage(
                    user_id="10001",
                    nickname="阿明",
                    content="今天修一下限流吧",
                    timestamp=1,
                    message_seq=1,
                    message_id="msg-1",
                    reply_to_message_id=None,
                )
            ],
            model="summary-model",
            temperature=0.3,
            max_tokens=512,
        )
    )

    assert result == "今天主要聊了修复限流。"
    assert fake_provider.message_calls[0]["request_phase"] == "group_history_summary"
