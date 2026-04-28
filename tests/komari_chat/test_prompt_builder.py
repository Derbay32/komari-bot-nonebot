"""Prompt Builder 引用上下文测试。"""

from __future__ import annotations

import asyncio
from importlib import import_module
from types import SimpleNamespace
from typing import Any

from komari_bot.plugins.komari_chat.services.reply_context import ReplyContext

prompt_builder_module = import_module(
    "komari_bot.plugins.komari_chat.services.prompt_builder"
)


async def _empty_search_knowledge(**_kwargs: object) -> list[object]:
    return []


async def _empty_search_by_keyword(_uid: str) -> list[object]:
    return []


def _patch_dependencies(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        prompt_builder_module,
        "get_template",
        lambda: {
            "system_prompt": "system",
            "memory_ack": "ack",
            "memory_ack_role": "user",
            "output_instruction": "output",
            "cot_prefix": "cot",
            "cot_prefix_role": "system",
        },
    )
    monkeypatch.setattr(prompt_builder_module, "get_festival_info", lambda: None)
    monkeypatch.setattr(
        prompt_builder_module,
        "komari_knowledge",
        SimpleNamespace(
            search_knowledge=_empty_search_knowledge,
            search_by_keyword=_empty_search_by_keyword,
        ),
    )
    monkeypatch.setattr(
        prompt_builder_module,
        "character_binding",
        SimpleNamespace(
            get_character_name=lambda user_id, fallback_nickname: (
                fallback_nickname or user_id or "未知用户"
            )
        ),
    )


def _build_config() -> SimpleNamespace:
    return SimpleNamespace(
        knowledge_enabled=False,
        llm_model_chat="test-model",
        assistant_prefill_enabled=False,
        dsv4_roleplay_instruct_mode="disabled",
    )


def test_build_prompt_inserts_assistant_turn_for_bot_reply_text(
    monkeypatch: Any,
) -> None:
    _patch_dependencies(monkeypatch)
    reply_context = ReplyContext(
        source_side="assistant",
        message_id="reply-1",
        user_id="bot",
        user_nickname="小鞠",
        text="上一条是机器人说的话",
        image_sources=(),
        image_count=0,
        has_visible_image=False,
    )

    messages = asyncio.run(
        prompt_builder_module.build_prompt(
            user_message="继续说",
            memories=[],
            config=_build_config(),
            current_user_id="user-1",
            current_user_nickname="阿虚",
            reply_context=reply_context,
        )
    )

    assert messages[0] == {"role": "system", "content": "system"}
    assert messages[1] == {"role": "system", "content": "output"}
    assert messages[2] == {"role": "assistant", "content": "上一条是机器人说的话"}
    assert messages[4] == {
        "role": "user",
        "content": "- 阿虚: <user_input>继续说</user_input>",
    }
    assert messages[-1] == messages[4]


def test_build_prompt_inserts_bot_reply_image_as_user_attachment(
    monkeypatch: Any,
) -> None:
    _patch_dependencies(monkeypatch)
    reply_context = ReplyContext(
        source_side="assistant",
        message_id="reply-2",
        user_id="bot",
        user_nickname="小鞠",
        text="",
        image_sources=("https://example.com/reply.png",),
        image_count=1,
        has_visible_image=True,
    )

    messages = asyncio.run(
        prompt_builder_module.build_prompt(
            user_message="看看这个",
            memories=[],
            config=_build_config(),
            current_user_id="user-1",
            current_user_nickname="阿虚",
            reply_context=reply_context,
            reply_image_urls=["data:image/png;base64,reply"],
        )
    )

    assert messages[2]["role"] == "assistant"
    assert "你上一条还发了 1 张图片" in messages[2]["content"]
    assert messages[4]["role"] == "user"
    assert messages[4]["content"] == [
        {"type": "text", "text": "（以下是你上一条被引用的 1 张图片）"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,reply"}},
        {"type": "text", "text": "- 阿虚: <user_input>看看这个</user_input>"},
    ]


def test_build_prompt_merges_user_reply_text_into_user_side(monkeypatch: Any) -> None:
    _patch_dependencies(monkeypatch)
    reply_context = ReplyContext(
        source_side="user",
        message_id="reply-3",
        user_id="user-2",
        user_nickname="长门",
        text="她刚才提到的角色是谁？",
        image_sources=(),
        image_count=0,
        has_visible_image=False,
    )

    messages = asyncio.run(
        prompt_builder_module.build_prompt(
            user_message="她是谁",
            memories=[],
            config=_build_config(),
            current_user_id="user-1",
            current_user_nickname="阿虚",
            reply_context=reply_context,
        )
    )

    assert messages[3] == {
        "role": "user",
        "content": (
            "- 长门（被回复）: 她刚才提到的角色是谁？\n"
            "- 阿虚: <user_input>她是谁</user_input>"
        ),
    }


def test_build_prompt_orders_user_reply_images_before_current_images(
    monkeypatch: Any,
) -> None:
    _patch_dependencies(monkeypatch)
    reply_context = ReplyContext(
        source_side="user",
        message_id="reply-4",
        user_id="user-2",
        user_nickname="长门",
        text="看看这张图",
        image_sources=("https://example.com/reply.png",),
        image_count=1,
        has_visible_image=True,
    )

    messages = asyncio.run(
        prompt_builder_module.build_prompt(
            user_message="这个呢",
            memories=[],
            config=_build_config(),
            current_user_id="user-1",
            current_user_nickname="阿虚",
            reply_context=reply_context,
            reply_image_urls=["data:image/png;base64,reply"],
            image_urls=["data:image/png;base64,current"],
        )
    )

    assert messages[3]["role"] == "user"
    assert messages[3]["content"] == [
        {
            "type": "text",
            "text": "- 长门（被回复）: 看看这张图\n- 长门（被回复）发送了 1 张图片。",
        },
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,reply"}},
        {"type": "text", "text": "- 阿虚: <user_input>这个呢</user_input>"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,current"}},
    ]


def test_build_prompt_keeps_legacy_prefill_when_enabled(monkeypatch: Any) -> None:
    _patch_dependencies(monkeypatch)
    config = _build_config()
    config.assistant_prefill_enabled = True

    messages = asyncio.run(
        prompt_builder_module.build_prompt(
            user_message="继续说",
            memories=[],
            config=config,
            current_user_id="user-1",
            current_user_nickname="阿虚",
        )
    )

    assert messages[-2] == {"role": "user", "content": "ack"}
    assert messages[-1] == {"role": "system", "content": "cot"}


def test_build_prompt_injects_dsv4_marker_to_first_user_message(
    monkeypatch: Any,
) -> None:
    _patch_dependencies(monkeypatch)
    config = _build_config()
    config.llm_model_chat = "deepseek-v4-flash"
    config.dsv4_roleplay_instruct_mode = "auto"

    messages = asyncio.run(
        prompt_builder_module.build_prompt(
            user_message="早上好",
            memories=[],
            config=config,
            current_user_id="user-1",
            current_user_nickname="阿虚",
        )
    )

    user_message = next(message for message in messages if message["role"] == "user")
    assert "【角色沉浸要求】" in user_message["content"]
    assert "【角色沉浸要求】" not in messages[0]["content"]
