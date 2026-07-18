"""OneBot 消息构造边界测试。"""

from __future__ import annotations

import pytest

from komari_bot.common.onebot_messages import plain_text_message


@pytest.mark.parametrize(
    "text",
    [
        "普通文本",
        "[CQ:at,qq=all]",
        "前缀[CQ:image,file=https://example.com/a.jpg]后缀",
        "[CQ:reply,id=1][CQ:at,qq=10086]",
    ],
)
def test_plain_text_message_never_parses_cq_code(text: str) -> None:
    message = plain_text_message(text)

    assert len(message) == 1
    assert message[0].type == "text"
    assert message[0].data == {"text": text}


def test_plain_text_message_stringifies_non_string_value() -> None:
    message = plain_text_message(42)

    assert message[0].data == {"text": "42"}
