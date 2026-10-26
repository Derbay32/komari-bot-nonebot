"""TSK-203 closed chat task-data envelope and automatic recall query."""

from __future__ import annotations

import json
from importlib import import_module
from types import SimpleNamespace

import pytest

from komari_bot.context.redis_store import MessageSchema
from komari_bot.llm.content_budget import QUERY_TEXT_BUDGET, validate_text_budget
from komari_bot.plugins.komari_chat.services.reply_context import ReplyContext

data_module = import_module(
    "komari_bot.plugins.komari_chat.services.chat_task_data"
)
query_module = import_module(
    "komari_bot.plugins.komari_chat.services.chat_recall_query"
)


def _message(
    index: int,
    *,
    content: str | None = None,
    group_id: str = "g-1",
    is_bot: bool = False,
    user_id: str | None = None,
    nickname: str | None = None,
) -> MessageSchema:
    return MessageSchema(
        user_id=user_id or f"u-{index}",
        user_nickname=nickname or f"用户{index}",
        group_id=group_id,
        content=content if content is not None else f"消息{index}",
        timestamp=float(index),
        message_id=f"m-{index}",
        is_bot=is_bot,
    )


def _favorability() -> object:
    return SimpleNamespace(
        user_id="u-1",
        favorability=123,
        stage_index=2,
        stage_name="熟悉",
        stage_prompt="关系逐渐熟悉。",
    )


def _recall_result(identifier: object = 7) -> object:
    return SimpleNamespace(
        id=identifier,
        text="用户喜欢在周末散步。",
    )


def _knowledge_hit(identifier: int = 11) -> object:
    return SimpleNamespace(
        id=identifier,
        content="小鞠喜欢甜食。",
        source="vector",
    )


def _build(
    *,
    recent: list[MessageSchema] | None = None,
    quote: ReplyContext | None = None,
    memory: list[object] | None = None,
    knowledge: list[object] | None = None,
    uid_knowledge: list[tuple[str, object]] | None = None,
) -> object:
    current = _message(0, content="当前消息", user_id="u-0", nickname="当前用户")
    return data_module.build_chat_task_data(
        current=current,
        recent=recent if recent is not None else [],
        quote=quote,
        memory_results=memory or [],
        knowledge_results=knowledge or [],
        uid_knowledge_results=uid_knowledge or [],
        favorability=_favorability(),
        bot_nickname="小鞠",
        max_favorability_delta=3,
        group_bindings={"u-0": "管理员", "u-1": "旅人", "u-4": "同伴"},
    )


def test_exact_closed_shape_and_provenance() -> None:
    recent = [
        _message(1, is_bot=True, content="<b>历史</b>"),
        _message(2, group_id="g-2"),
        _message(3),
        _message(4, user_id="u-4", nickname="用户4"),
    ]
    quote = ReplyContext(
        source_side="user",
        message_id="quoted-1",
        user_id="u-3",
        user_nickname="被引用用户",
        text="引用正文",
        image_sources=("https://example.invalid/image.png",),
        image_count=2,
        has_visible_image=True,
    )
    data = _build(
        recent=recent,
        quote=quote,
        memory=[_recall_result()],
        knowledge=[_knowledge_hit()],
        uid_knowledge=[("u-3", _knowledge_hit(12))],
    )
    payload = data.to_dict()

    assert set(payload) == {
        "schema",
        "current",
        "recent",
        "quote",
        "memory",
        "knowledge",
        "uid_knowledge",
        "facts",
    }
    assert payload["schema"] == 1
    assert payload["current"] == {
        "source_type": "conversation_history",
        "source_id": "m-0",
        "trust_level": "untrusted",
        "content": "当前消息",
        "message_id": "m-0",
        "group_id": "g-1",
        "user_id": "u-0",
        "user_nickname": "当前用户",
        "timestamp": 0.0,
        "is_bot": False,
    }
    assert [item["message_id"] for item in payload["recent"]] == ["m-1", "m-3", "m-4"]
    assert payload["recent"][0]["is_bot"] is True
    assert payload["recent"][0]["source_type"] == "conversation_history"
    assert payload["recent"][0]["content"] == "<b>历史</b>"
    assert payload["quote"] == {
        "source_type": "quoted_message",
        "source_id": "quoted-1",
        "trust_level": "untrusted",
        "content": "引用正文",
        "message_id": "quoted-1",
        "user_id": "u-3",
        "user_nickname": "被引用用户",
        "source_side": "user",
        "image_count": 2,
        "has_visible_image": True,
    }
    assert "image_sources" not in json.dumps(payload, ensure_ascii=False)
    assert payload["memory"] == [
        {
            "source_type": "memory",
            "source_id": "7",
            "trust_level": "untrusted",
            "content": "用户喜欢在周末散步。",
        }
    ]
    assert payload["knowledge"] == [
        {
            "source_type": "knowledge",
            "source_id": "11",
            "trust_level": "low",
            "content": "小鞠喜欢甜食。",
        }
    ]
    assert payload["uid_knowledge"] == [
        {
            "source_type": "knowledge",
            "source_id": "uid:u-3:12",
            "trust_level": "low",
            "content": "小鞠喜欢甜食。",
        }
    ]
    facts = payload["facts"]
    assert facts["bot_nickname"] == "小鞠"
    assert facts["current_favorability"] == {
        "value": 123,
        "stage_index": 2,
        "stage_name": "熟悉",
        "stage_prompt": "关系逐渐熟悉。",
    }
    assert facts["visible_users"] == [
        {"user_id": "u-3", "nickname": "用户3", "character_name": None},
        {"user_id": "u-4", "nickname": "用户4", "character_name": "同伴"},
        {"user_id": "u-0", "nickname": "当前用户", "character_name": "管理员"},
    ]
    assert facts["max_favorability_delta"] == 3


def test_recent_is_same_group_trigger_free_and_ten_message_bounded() -> None:
    current = _message(0, group_id="g-1")
    recent = [
        _message(index, group_id="g-1" if index != 2 else "g-2")
        for index in range(1, 13)
    ]
    recent.insert(4, current)
    data = data_module.build_chat_task_data(
        current=current,
        recent=recent,
        quote=None,
        memory_results=[],
        knowledge_results=[],
        uid_knowledge_results=[],
        favorability=_favorability(),
        bot_nickname="小鞠",
        max_favorability_delta=5,
        group_bindings={},
    )
    payload = data.to_dict()
    assert [item["message_id"] for item in payload["recent"]] == [
        "m-3",
        "m-4",
        "m-5",
        "m-6",
        "m-7",
        "m-8",
        "m-9",
        "m-10",
        "m-11",
        "m-12",
    ]


def test_synthetic_source_ids_are_explicit_and_platform_ids_are_not_fabricated() -> None:
    data = data_module.build_chat_task_data(
        current=_message(0),
        recent=[],
        quote=None,
        memory_results=[SimpleNamespace(id=None, text="无标识记忆")],
        knowledge_results=[SimpleNamespace(id=None, content="无标识知识", source="keyword")],
        uid_knowledge_results=[("u-9", SimpleNamespace(id=None, content="无标识 UID 知识", source="keyword"))],
        favorability=_favorability(),
        bot_nickname="小鞠",
        max_favorability_delta=5,
        group_bindings={},
    )
    payload = data.to_dict()
    assert payload["memory"][0]["source_id"] == "memory:index:0"
    assert payload["knowledge"][0]["source_id"] == "knowledge:index:0"
    assert payload["uid_knowledge"][0]["source_id"] == "knowledge:uid:u-9:index:0"


def test_untrusted_content_is_bounded_to_existing_extraction_limit() -> None:
    data = _build(
        memory=[SimpleNamespace(id=1, text="记" * 5_000)],
        knowledge=[SimpleNamespace(id=2, content="知" * 5_000, source="vector")],
    )
    payload = data.to_dict()
    assert len(payload["memory"][0]["content"]) <= 4_000
    assert len(payload["knowledge"][0]["content"]) <= 4_000


def test_recall_query_uses_current_ten_history_quote_and_budget() -> None:
    current = _message(0, content="当前问题", user_id="u-0")
    recent = [_message(index) for index in range(1, 13)]
    quote = ReplyContext(
        source_side="assistant",
        message_id="quoted-9",
        user_id="bot",
        user_nickname="小鞠",
        text="引用中的历史回答",
        image_sources=(),
        image_count=0,
        has_visible_image=False,
    )
    query = query_module.build_automatic_recall_query(
        current=current,
        recent=recent,
        quote=quote,
    )
    validate_text_budget(query, label="召回查询", budget=QUERY_TEXT_BUDGET)
    assert "当前问题" in query
    assert "消息1\n" not in query
    assert "消息12" in query
    assert 'message_id="quoted-9"' in query
    assert 'source_side="assistant"' in query
    assert "引用中的历史回答" in query


def test_recall_query_trims_oldest_history_first_but_keeps_trigger_and_quote_identity() -> None:
    current = _message(0, content="触发")
    recent = [_message(index, content=f"历史{index}-" + "长" * 70) for index in range(1, 11)]
    quote = ReplyContext(
        source_side="user",
        message_id="quoted-1",
        user_id="u-1",
        user_nickname="用户1",
        text="引用",
        image_sources=(),
        image_count=0,
        has_visible_image=False,
    )
    query = query_module.build_automatic_recall_query(
        current=current,
        recent=recent,
        quote=quote,
    )
    validate_text_budget(query, label="召回查询", budget=QUERY_TEXT_BUDGET)
    assert "触发" in query
    assert 'message_id="quoted-1"' in query
    assert "历史1-" not in query
    assert "历史10-" in query


def test_empty_trigger_uses_existing_media_marker_and_does_not_skip_history() -> None:
    current = _message(0, content="   ")
    query = query_module.build_automatic_recall_query(
        current=current,
        recent=[_message(1, content="唯一历史")],
        quote=None,
    )
    assert "当前交流" in query
    assert "唯一历史" in query


def test_current_trigger_alone_that_cannot_fit_is_a_bounded_validation_failure() -> None:
    current = _message(0, content="触" * (QUERY_TEXT_BUDGET.max_characters + 1))
    with pytest.raises(query_module.RecallQueryBudgetError) as exc_info:
        query_module.build_automatic_recall_query(
            current=current,
            recent=[_message(1)],
            quote=None,
        )
    assert exc_info.value.code == "recall_query_unfit"
