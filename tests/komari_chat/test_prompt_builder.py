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
    assert messages[2]["role"] == "system"
    assert "<profile_tool_hint>" in messages[2]["content"]
    assert messages[3] == {"role": "assistant", "content": "上一条是机器人说的话"}
    assert messages[5] == {
        "role": "user",
        "content": "- 阿虚: <user_input>继续说</user_input>",
    }
    assert messages[-1] == messages[5]


def test_build_prompt_injects_search_tool_system_message(monkeypatch: Any) -> None:
    _patch_dependencies(monkeypatch)

    messages = asyncio.run(
        prompt_builder_module.build_prompt(
            user_message="搜索一下今天的大新闻",
            memories=[],
            config=_build_config(),
            current_user_id="user-1",
            current_user_nickname="阿虚",
            search_tool_mode=True,
        )
    )

    assert messages[3]["role"] == "system"
    assert "search_web" in messages[3]["content"]
    assert "不要编造" in messages[3]["content"]


def test_build_prompt_injects_only_current_user_profile(monkeypatch: Any) -> None:
    _patch_dependencies(monkeypatch)

    messages = asyncio.run(
        prompt_builder_module.build_prompt(
            user_message="她喜欢什么",
            memories=[],
            config=_build_config(),
            recent_messages=[
                SimpleNamespace(
                    is_bot=False,
                    user_id="user-2",
                    user_nickname="长门",
                    content="我喜欢咖喱",
                )
            ],
            current_user_id="user-1",
            current_user_nickname="阿虚",
            current_user_profile={
                "display_name": "阿虚",
                "traits": {
                    "喜欢的食物": {
                        "value": "拉面",
                        "category": "preference",
                        "importance": 5,
                        "updated_at": "2026-06-01T00:00:00+00:00",
                    }
                },
                "group_id": "group-1",
            },
        )
    )

    joined = "\n".join(str(message["content"]) for message in messages)
    assert "<current_user_profile>" in joined
    assert "喜欢的食物: 拉面" in joined
    assert "咖喱" in joined  # 只来自近期消息，不是画像块
    profile_block = joined.rsplit("<current_user_profile>", 1)[1].split(
        "</current_user_profile>",
        1,
    )[0]
    assert "拉面" in profile_block
    assert "咖喱" not in profile_block
    assert "<user_entities>" not in joined
    assert "importance" not in profile_block
    assert "updated_at" not in profile_block
    assert "group_id" not in profile_block


def test_build_prompt_does_not_fetch_profiles_for_visible_users(monkeypatch: Any) -> None:
    _patch_dependencies(monkeypatch)

    class _FailingMemory:
        async def get_user_profile(self, **_kwargs: object) -> object:
            raise AssertionError

        async def get_interaction_history(self, **_kwargs: object) -> object:
            raise AssertionError

    messages = asyncio.run(
        prompt_builder_module.build_prompt(
            user_message="你好",
            memories=[],
            config=_build_config(),
            current_user_id="user-1",
            current_user_nickname="阿虚",
            memory_service=_FailingMemory(),
            group_id="group-1",
            current_user_profile={
                "display_name": "阿虚",
                "traits": {"性格": {"value": "经常开玩笑", "category": "general"}},
            },
        )
    )

    assert any("<current_user_profile>" in str(message["content"]) for message in messages)


def test_build_prompt_injects_interactions_as_yaml_content_time_only(
    monkeypatch: Any,
) -> None:
    _patch_dependencies(monkeypatch)

    messages = asyncio.run(
        prompt_builder_module.build_prompt(
            user_message="继续聊",
            memories=[],
            config=_build_config(),
            current_user_id="user-1",
            current_user_nickname="阿虚",
            interaction_records=[
                {
                    "event": "用户问候小鞠",
                    "result": "小鞠回应",
                    "emotion": "开心",
                    "timestamp": 1780495200.0,
                    "message_id": "msg-1",
                    "importance": 5,
                }
            ],
            interaction_memories=[
                {
                    "event_summary": "用户之前经常用食物话题逗小鞠",
                    "last_seen_at": "2026-06-01T12:30:00+00:00",
                    "similarity": 0.9,
                    "importance_current": 5,
                }
            ],
        )
    )

    joined = "\n".join(str(message["content"]) for message in messages)
    assert "<recent_interaction_history>" in joined
    assert "content: 用户问候小鞠；小鞠回应；开心" in joined
    assert "<interaction_memory>" in joined
    assert "content: 用户之前经常用食物话题逗小鞠" in joined
    assert "timestamp" not in joined
    assert "message_id" not in joined
    assert "similarity" not in joined
    assert "importance_current" not in joined


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

    assert messages[3]["role"] == "assistant"
    assert "你上一条还发了 1 张图片" in messages[3]["content"]
    assert messages[5]["role"] == "user"
    assert messages[5]["content"] == [
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

    assert messages[4] == {
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

    assert messages[4]["role"] == "user"
    assert messages[4]["content"] == [
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
