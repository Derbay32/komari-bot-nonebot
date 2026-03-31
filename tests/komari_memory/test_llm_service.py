"""KomariMemory 总结分段测试。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from komari_bot.plugins.komari_memory.config_schema import KomariMemoryConfigSchema
from komari_bot.plugins.komari_memory.services import llm_service as llm_service_module
from komari_bot.plugins.komari_memory.services.llm_service import summarize_conversation
from komari_bot.plugins.komari_memory.services.redis_manager import MessageSchema


class _FakeLLMProvider:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    async def generate_text(self, **kwargs: Any) -> str:
        self.prompts.append(str(kwargs["prompt"]))
        if not self._responses:
            raise AssertionError
        return json.dumps(self._responses.pop(0), ensure_ascii=False)


class _DynamicChunkFakeLLMProvider:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self._chunk_index = 0

    async def generate_text(self, **kwargs: Any) -> str:
        prompt = str(kwargs["prompt"])
        self.prompts.append(prompt)
        if "按时间顺序排列的分段总结" in prompt:
            return json.dumps(
                {
                    "summary": "最终总结",
                    "user_profiles": [],
                    "user_interactions": [],
                    "importance": 5,
                },
                ensure_ascii=False,
            )

        self._chunk_index += 1
        return json.dumps(
            {
                "summary": f"第{self._chunk_index}段总结",
                "user_profiles": [],
                "user_interactions": [],
                "importance": 3,
            },
            ensure_ascii=False,
        )


def _make_config(**overrides: Any) -> KomariMemoryConfigSchema:
    base = {
        "bot_nickname": "小鞠知花",
        "llm_model_summary": "summary-model",
        "llm_temperature_summary": 0.3,
        "llm_max_tokens_summary": 2048,
        "summary_chunk_token_limit": 3000,
    }
    base.update(overrides)
    return KomariMemoryConfigSchema(**base)


def _make_message(
    *,
    content: str,
    user_id: str = "10001",
    user_nickname: str = "阿明",
    is_bot: bool = False,
) -> MessageSchema:
    return MessageSchema(
        user_id=user_id,
        user_nickname=user_nickname,
        group_id="114514",
        content=content,
        timestamp=1.0,
        message_id=f"msg-{user_id}-{len(content)}",
        is_bot=is_bot,
    )


async def _run_summarize_conversation(
    *,
    messages: list[MessageSchema],
    config: KomariMemoryConfigSchema,
    existing_profiles: list[dict[str, Any]] | None = None,
    existing_interactions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return await summarize_conversation(
        messages=messages,
        config=config,
        existing_profiles=existing_profiles,
        existing_interactions=existing_interactions,
    )


def test_summarize_conversation_single_chunk_uses_one_request(monkeypatch: Any) -> None:
    fake_provider = _FakeLLMProvider(
        [
            {
                "summary": "一次总结",
                "user_profiles": [],
                "user_interactions": [],
                "importance": 4,
            }
        ]
    )
    monkeypatch.setattr(llm_service_module, "llm_provider", fake_provider)

    result = asyncio.run(
        _run_summarize_conversation(
            messages=[
                _make_message(content="今天一起吃拉面吧"),
                _make_message(content="好呀，我还想顺便聊聊新番", user_id="10002", user_nickname="小绿"),
            ],
            config=_make_config(summary_chunk_token_limit=5000),
        )
    )

    assert result["summary"] == "一次总结"
    assert len(fake_provider.prompts) == 1
    assert "按时间顺序排列的分段总结" not in fake_provider.prompts[0]


def test_summarize_conversation_chunks_then_merges(monkeypatch: Any) -> None:
    fake_provider = _DynamicChunkFakeLLMProvider()
    monkeypatch.setattr(llm_service_module, "llm_provider", fake_provider)

    result = asyncio.run(
        _run_summarize_conversation(
            messages=[
                _make_message(content="甲" * 1400),
                _make_message(content="乙" * 1400, user_id="10002", user_nickname="小绿"),
            ],
            config=_make_config(summary_chunk_token_limit=2200),
            existing_profiles=[
                {
                    "user_id": "10001",
                    "display_name": "阿明",
                    "traits": {"喜欢的饮料": {"value": "可乐", "category": "preference"}},
                }
            ],
            existing_interactions=[
                {
                    "user_id": "10001",
                    "file_type": "用户的近期对鞠行为备忘录",
                    "description": "旧记录",
                    "records": [{"event": "投喂", "result": "开心", "emotion": "期待"}],
                    "summary": "旧总结",
                }
            ],
        )
    )

    raw_prompts = [
        prompt
        for prompt in fake_provider.prompts
        if "按时间顺序排列的分段总结" not in prompt
    ]
    merge_prompts = [
        prompt
        for prompt in fake_provider.prompts
        if "按时间顺序排列的分段总结" in prompt
    ]

    assert result["summary"] == "最终总结"
    assert len(raw_prompts) >= 2
    assert len(merge_prompts) == 1
    assert all("【已知用户画像" not in prompt for prompt in raw_prompts)
    assert "【已知用户画像" in merge_prompts[0]
    assert "【分段1/" in merge_prompts[0]


def test_chunk_formatted_messages_splits_oversized_single_message() -> None:
    chunks, oversized_message_split = llm_service_module._chunk_formatted_messages(
        messages=[_make_message(content="超长内容" * 900)],
        config=_make_config(summary_chunk_token_limit=2200),
    )

    all_lines = [line for chunk in chunks for line in chunk.lines]

    assert oversized_message_split is True
    assert len(chunks) >= 2
    assert all(chunk.lines for chunk in chunks)
    assert any("[分片1]" in line for line in all_lines)


def test_existing_context_builder_trims_to_budget() -> None:
    result = llm_service_module._build_existing_context_with_budget(
        existing_profiles=[
            {
                "user_id": "10001",
                "display_name": "阿明",
                "traits": {
                    f"特征{i}": {
                        "value": "很长的描述" * 40,
                        "category": "general",
                        "importance": 5,
                        "updated_at": f"2026-03-21T00:00:{i:02d}+08:00",
                    }
                    for i in range(10)
                },
            }
        ],
        existing_interactions=[
            {
                "user_id": "10001",
                "summary": "很长的互动总结" * 40,
                "records": [
                    {
                        "event": "事件" * 20,
                        "result": "结果" * 20,
                        "emotion": "情绪" * 20,
                    }
                    for _ in range(6)
                ],
            }
        ],
        token_budget=1200,
    )

    assert result.estimated_tokens <= 1200
    assert result.included_profiles + result.included_interactions >= 1
    assert result.truncated is True


def test_build_summary_prompt_uses_template_content(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        llm_service_module,
        "get_summary_template",
        lambda: {
            "summary_prompt": "前缀\n{{conversation_text}}\n{{existing_context}}\n{{json_response_example}}",
            "merge_prompt": "不会用到",
            "existing_context_instruction_block": "不会用到",
            "existing_profiles_header": "不会用到",
            "existing_interactions_header": "不会用到",
            "truncated_context_marker": "不会用到",
            "json_response_example": '{"ok": true}',
        },
    )

    prompt = llm_service_module._build_summary_prompt(
        "对话正文",
        existing_context="已有上下文\n",
    )

    assert prompt == '前缀\n对话正文\n已有上下文\n\n{"ok": true}'


def test_existing_context_builder_uses_template_sections(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        llm_service_module,
        "get_summary_template",
        lambda: {
            "summary_prompt": "不会用到",
            "merge_prompt": "不会用到",
            "existing_context_instruction_block": "自定义指示",
            "existing_profiles_header": "自定义画像头",
            "existing_interactions_header": "自定义互动头",
            "truncated_context_marker": "自定义截断提示",
            "json_response_example": "不会用到",
        },
    )

    result = llm_service_module._build_existing_context_with_budget(
        existing_profiles=[
            {
                "user_id": "10001",
                "display_name": "阿明",
                "traits": [{"key": "喜好", "value": "拉面", "category": "preference"}],
            }
        ],
        existing_interactions=[
            {
                "user_id": "10001",
                "summary": "最近常聊吃饭",
                "records": [{"event": "约饭", "result": "答应", "emotion": "开心"}],
            }
        ],
        token_budget=None,
    )

    assert "自定义画像头" in result.text
    assert "自定义互动头" in result.text
    assert "自定义指示" in result.text


def test_summarize_conversation_single_chunk_trims_existing_context_to_fit_limit(
    monkeypatch: Any,
) -> None:
    fake_provider = _FakeLLMProvider(
        [
            {
                "summary": "一次总结",
                "user_profiles": [],
                "user_interactions": [],
                "importance": 4,
            }
        ]
    )
    monkeypatch.setattr(llm_service_module, "llm_provider", fake_provider)
    config = _make_config(summary_chunk_token_limit=4000)

    asyncio.run(
        _run_summarize_conversation(
            messages=[_make_message(content="今天一起吃拉面吧")],
            config=config,
            existing_profiles=[
                {
                    "user_id": "10001",
                    "display_name": "阿明",
                    "traits": {
                        f"特征{i}": {
                            "value": "很长的描述" * 50,
                            "category": "general",
                            "importance": 5,
                            "updated_at": f"2026-03-21T00:00:{i:02d}+08:00",
                        }
                        for i in range(12)
                    },
                }
            ],
            existing_interactions=[
                {
                    "user_id": "10001",
                    "summary": "很长的互动总结" * 60,
                    "records": [
                        {
                            "event": "事件" * 20,
                            "result": "结果" * 20,
                            "emotion": "情绪" * 20,
                        }
                        for _ in range(8)
                    ],
                }
            ],
        )
    )

    assert len(fake_provider.prompts) == 1
    assert len(fake_provider.prompts[0]) <= config.summary_chunk_token_limit
