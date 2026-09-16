"""TSK-294 回归：官 Bot 核验卡片的 Markdown 正文必须能提取会话码。

真实 QQ 官 Bot 以原生 reply + markdown + inline_keyboard 上报身份核验卡片，
会话码位于 ``markdown.content``。现有 ``ReplyEvidenceCollector`` 只拼接 text
段，卡片因此解析为 ``None``，取证链在 get_msg 交叉验证前静默中断。

本文件只驱动真实的 ``ReplyEvidenceCollector.handle_event``，复用
``tests/character_binding/test_reply_evidence.py``（TSK-273）的原始消息、会话、
原生 reply 与 get_msg 测试 helper；不访问数据库、网络或 ``.env``。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from tests.character_binding.reply_evidence_support import (
    BASE_TIME,
    COMMAND,
    FrozenClock,
    MessageFetcher,
    _assert_evidence,
    _challenge_event,
    _collector,
    _get_msg_payload,
    _open,
    _original_event,
    _prime_and_capture,
)

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import GroupMessageEvent

    from komari_bot.plugins.character_binding.reply_evidence import ReplyEvidence

CHALLENGE_TEMPLATE = "正在确认你的本群身份。会话码：{code}"
_UNSET: object = object()


def _challenge_text(code: str) -> str:
    return CHALLENGE_TEMPLATE.format(code=code)


def _markdown_body(content: object, *, reply_to: int | None = None) -> Message:
    segments: list[MessageSegment] = []
    if reply_to is not None:
        segments.append(MessageSegment.reply(reply_to))
    segments.append(MessageSegment("markdown", {"content": content}))
    return Message(segments)


def _challenge_with_body(
    *,
    message_id: int,
    session_code: str,
    quoted_message_id: int,
    message_body: Message,
    original_body: Message,
) -> GroupMessageEvent:
    """同一可信官 Bot 挑战结构，仅替换 message / original_message 正文。"""
    base = _challenge_event(
        message_id=message_id,
        session_code=session_code,
        quoted_message_id=quoted_message_id,
        quoted_text=COMMAND,
    )
    return base.model_copy(
        update={"message": message_body, "original_message": original_body}
    )


def _markdown_challenge(
    *,
    message_id: int,
    code: str,
    quoted_message_id: int,
    content: object = _UNSET,
) -> GroupMessageEvent:
    """message 与 original_message 都以 Markdown 上报同一挑战正文。"""
    body_content = _challenge_text(code) if content is _UNSET else content
    return _challenge_with_body(
        message_id=message_id,
        session_code=code,
        quoted_message_id=quoted_message_id,
        message_body=_markdown_body(body_content),
        original_body=_markdown_body(body_content, reply_to=quoted_message_id),
    )


async def _prime_and_capture_body(
    *,
    code: str,
    original_id: int,
    challenge: GroupMessageEvent,
) -> tuple[ReplyEvidence | None, MessageFetcher]:
    """缓存原生 /bind 原消息后处理挑战事件，返回证据与 fetcher。"""
    fetcher = MessageFetcher(
        {
            original_id: _get_msg_payload(
                message_id=original_id,
                original_text=COMMAND,
            )
        }
    )
    collector = _collector(fetcher=fetcher, clock=FrozenClock(BASE_TIME))
    _open(collector, code=code)
    evidence = await _prime_and_capture(
        collector,
        original=_original_event(message_id=original_id),
        challenge=challenge,
    )
    return evidence, fetcher


@pytest.mark.asyncio
async def test_plain_text_challenge_body_is_accepted_as_control() -> None:
    """对照：正文为普通 text 的同一挑战照常取证。"""
    code = "TSK294-TEXT-CONTROL"
    original_id = 31401
    challenge_id = 41401
    evidence, fetcher = await _prime_and_capture_body(
        code=code,
        original_id=original_id,
        challenge=_challenge_event(
            message_id=challenge_id,
            session_code=code,
            quoted_message_id=original_id,
            quoted_text=COMMAND,
            body_format="text",
        ),
    )

    assert evidence is not None
    _assert_evidence(
        evidence,
        code=code,
        onebot_original_message_id=original_id,
        challenge_message_id=challenge_id,
    )
    assert fetcher.calls == [original_id]


@pytest.mark.asyncio
async def test_markdown_challenge_body_yields_evidence() -> None:
    """message 与 original_message 的正文改为实测 Markdown 段仍须取证。"""
    code = "TSK294-MARKDOWN-ONLY"
    original_id = 31411
    challenge_id = 41411
    evidence, fetcher = await _prime_and_capture_body(
        code=code,
        original_id=original_id,
        challenge=_markdown_challenge(
            message_id=challenge_id,
            code=code,
            quoted_message_id=original_id,
        ),
    )

    assert evidence is not None
    _assert_evidence(
        evidence,
        code=code,
        onebot_original_message_id=original_id,
        challenge_message_id=challenge_id,
    )
    assert fetcher.calls == [original_id]


@pytest.mark.asyncio
async def test_same_code_repeated_across_text_and_markdown_is_allowed() -> None:
    """同一合法码在 text 与 Markdown 中出现时不得误判为冲突。"""
    code = "TSK294-SAME-CODE"
    original_id = 31421
    challenge_id = 41421
    evidence, fetcher = await _prime_and_capture_body(
        code=code,
        original_id=original_id,
        challenge=_challenge_with_body(
            message_id=challenge_id,
            session_code=code,
            quoted_message_id=original_id,
            message_body=Message([MessageSegment.text(_challenge_text(code))]),
            original_body=_markdown_body(
                _challenge_text(code),
                reply_to=original_id,
            ),
        ),
    )

    assert evidence is not None
    _assert_evidence(
        evidence,
        code=code,
        onebot_original_message_id=original_id,
        challenge_message_id=challenge_id,
    )
    assert fetcher.calls == [original_id]


@pytest.mark.asyncio
async def test_conflicting_text_and_markdown_codes_are_rejected() -> None:
    """正文出现两个不同会话码时必须拒绝，不得只看 text 一侧。"""
    code = "TSK294-CONFLICT-A"
    other_code = "TSK294-CONFLICT-B"
    original_id = 31431
    challenge_id = 41431
    evidence, fetcher = await _prime_and_capture_body(
        code=code,
        original_id=original_id,
        challenge=_challenge_with_body(
            message_id=challenge_id,
            session_code=code,
            quoted_message_id=original_id,
            message_body=Message([MessageSegment.text(_challenge_text(code))]),
            original_body=_markdown_body(
                _challenge_text(other_code),
                reply_to=original_id,
            ),
        ),
    )

    assert evidence is None
    assert fetcher.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "content"),
    [
        ("none", None),
        ("number", 20240914),
        ("dict_with_code", {"content": _challenge_text("TSK294-CONTENT")}),
        ("list_with_code", [_challenge_text("TSK294-CONTENT")]),
    ],
)
async def test_unsupported_markdown_content_does_not_produce_evidence(
    case: str,
    content: object,
) -> None:
    """markdown content 非约定字符串时失败关闭，即使结构内含合法码也不猜测。"""
    del case
    code = "TSK294-CONTENT"
    original_id = 31441
    challenge_id = 41441
    evidence, fetcher = await _prime_and_capture_body(
        code=code,
        original_id=original_id,
        challenge=_markdown_challenge(
            message_id=challenge_id,
            code=code,
            quoted_message_id=original_id,
            content=content,
        ),
    )

    assert evidence is None
    assert fetcher.calls == []


@pytest.mark.asyncio
async def test_keyboard_or_json_only_challenge_body_does_not_produce_evidence() -> None:
    """只有 keyboard / json 段（内含合法码）时不得从这些结构取证。"""
    code = "TSK294-KEYBOARD-JSON"
    original_id = 31451
    challenge_id = 41451
    keyboard = MessageSegment(
        "keyboard",
        {"content": {"rows": [[{"command": _challenge_text(code)}]]}},
    )
    json_card = MessageSegment(
        "json",
        {"data": json.dumps({"prompt": _challenge_text(code)})},
    )
    inline_keyboard = MessageSegment(
        "inline_keyboard",
        {
            "data": {
                "rows": [
                    {
                        "buttons": [
                            {
                                "action": {
                                    "type": 2,
                                    "data": f"/bind confirm {code}",
                                }
                            }
                        ]
                    }
                ]
            }
        },
    )

    for extra in (keyboard, json_card, inline_keyboard):
        evidence, fetcher = await _prime_and_capture_body(
            code=code,
            original_id=original_id,
            challenge=_challenge_with_body(
                message_id=challenge_id,
                session_code=code,
                quoted_message_id=original_id,
                message_body=Message([extra]),
                original_body=Message([MessageSegment.reply(original_id), extra]),
            ),
        )
        assert evidence is None
        assert fetcher.calls == []


@pytest.mark.asyncio
async def test_markdown_content_with_version_metadata_prefix_is_accepted() -> None:
    """实测卡片 content 带 [](%7B%22version%22%3A2%7D) 前缀，换行后正文仍须提取会话码。"""
    code = "TSK294-MD-PREFIX"
    original_id = 31461
    challenge_id = 41461
    content = "[](%7B%22version%22%3A2%7D)\n" + _challenge_text(code)
    evidence, fetcher = await _prime_and_capture_body(
        code=code,
        original_id=original_id,
        challenge=_markdown_challenge(
            message_id=challenge_id,
            code=code,
            quoted_message_id=original_id,
            content=content,
        ),
    )

    assert evidence is not None
    _assert_evidence(
        evidence,
        code=code,
        onebot_original_message_id=original_id,
        challenge_message_id=challenge_id,
    )
    assert fetcher.calls == [original_id]


_MISSING: object = object()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["no_code", "two_codes_same_markdown", "message_original_codes_differ"],
)
async def test_markdown_without_single_legal_code_yields_no_evidence(
    case: str,
) -> None:
    """无会话码、同一 Markdown 两个不同码、两侧 Markdown 码不同均失败关闭。"""
    code = "TSK294-MD-AMBIGUOUS"
    other_code = "TSK294-MD-OTHER"
    original_id = 31471
    challenge_id = 41471
    if case == "no_code":
        message_body = _markdown_body("正在确认你的本群身份，请稍后再试。")
        original_body = _markdown_body(
            "正在确认你的本群身份，请稍后再试。",
            reply_to=original_id,
        )
    elif case == "two_codes_same_markdown":
        content = f"{_challenge_text(code)} {_challenge_text(other_code)}"
        message_body = _markdown_body(content)
        original_body = _markdown_body(content, reply_to=original_id)
    else:
        message_body = _markdown_body(_challenge_text(code))
        original_body = _markdown_body(
            _challenge_text(other_code),
            reply_to=original_id,
        )
    evidence, fetcher = await _prime_and_capture_body(
        code=code,
        original_id=original_id,
        challenge=_challenge_with_body(
            message_id=challenge_id,
            session_code=code,
            quoted_message_id=original_id,
            message_body=message_body,
            original_body=original_body,
        ),
    )

    assert evidence is None
    assert fetcher.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "content"),
    [
        ("missing_content_key", _MISSING),
        ("none", None),
        ("number", 20240914),
        ("dict_with_code", {"content": _challenge_text("TSK294-MD-ILLEGAL")}),
        ("list_with_code", [_challenge_text("TSK294-MD-ILLEGAL")]),
    ],
)
async def test_legal_text_code_with_illegal_markdown_content_fails_closed(
    case: str,
    content: object,
) -> None:
    """message 带合法 text 码但 original_message 的 Markdown content 非法时，
    整个挑战必须失败关闭，不得只读合法 text 一侧放行。"""
    del case
    code = "TSK294-MD-ILLEGAL"
    original_id = 31481
    challenge_id = 41481
    markdown_segment = MessageSegment(
        "markdown",
        {} if content is _MISSING else {"content": content},
    )
    evidence, fetcher = await _prime_and_capture_body(
        code=code,
        original_id=original_id,
        challenge=_challenge_with_body(
            message_id=challenge_id,
            session_code=code,
            quoted_message_id=original_id,
            message_body=Message([MessageSegment.text(_challenge_text(code))]),
            original_body=Message(
                [MessageSegment.reply(original_id), markdown_segment]
            ),
        ),
    )

    assert evidence is None
    assert fetcher.calls == []


@pytest.mark.asyncio
async def test_text_session_code_label_split_across_two_text_segments_is_accepted() -> None:
    """普通 text 的“会话码：”标签拆为两个 text 段仍须按拼接语义取证。"""
    code = "TSK294-TEXT-SPLIT"
    original_id = 31491
    challenge_id = 41491
    label = "正在确认你的本群身份。会话码："
    message_body = Message(
        [MessageSegment.text(label), MessageSegment.text(code)]
    )
    original_body = Message(
        [
            MessageSegment.reply(original_id),
            MessageSegment.text(label),
            MessageSegment.text(code),
        ]
    )
    evidence, fetcher = await _prime_and_capture_body(
        code=code,
        original_id=original_id,
        challenge=_challenge_with_body(
            message_id=challenge_id,
            session_code=code,
            quoted_message_id=original_id,
            message_body=message_body,
            original_body=original_body,
        ),
    )

    assert evidence is not None
    _assert_evidence(
        evidence,
        code=code,
        onebot_original_message_id=original_id,
        challenge_message_id=challenge_id,
    )
    assert fetcher.calls == [original_id]
