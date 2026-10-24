"""TSK-187 入站投影验收：真实 OneBot V11 事件 → 单次分发冻结请求。

断言穿过真实事件前置处理器与 ``get_decision_dispatch`` 公开面；只使用真实
``Bot`` / ``Message`` / ``Reply`` 与真实 NoneBot 状态对象，不制造判定状态、
不调用模型、不访问存储或平台。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.adapters.onebot.v11.event import GroupMessageEvent, Reply, Sender

from komari_bot.decision.dispatch import DecisionDispatchUnavailableError
from komari_bot.plugins.komari_decision.services.jev_dispatch import (
    DISPATCH_STATE_KEY,
    get_decision_dispatch,
    prepare_decision_dispatch,
)

if TYPE_CHECKING:
    from nonebot.typing import T_State

pytestmark = pytest.mark.asyncio

BOT_SELF_ID = 12345
OTHER_USER_ID = 67890
GROUP_ID = 100
MESSAGE_ID = 777
PLATFORM_TIME = 1_000_000


class _ProbeBot(OneBotV11Bot):
    """真实 OneBot V11 Bot 身份（只记录平台调用，不访问网络）。"""

    def __init__(self, self_id: str = str(BOT_SELF_ID)) -> None:
        adapter = cast("OneBotV11Adapter", OneBotV11Adapter.__new__(OneBotV11Adapter))
        super().__init__(adapter, self_id)
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call_api(self, api: str, **data: object) -> object:
        self.calls.append((api, data))
        return None


def _message(*segments: MessageSegment | str) -> Message:
    message = Message()
    for segment in segments:
        message += segment
    return message


def _event(
    *,
    original: Message | None = None,
    message: Message | None = None,
    reply: Reply | None = None,
    to_me: bool = False,
    message_id: object = MESSAGE_ID,
    event_self_id: object = BOT_SELF_ID,
    time: object = PLATFORM_TIME,
) -> GroupMessageEvent:
    raw = original if original is not None else _message("你好")
    return cast(
        "GroupMessageEvent",
        GroupMessageEvent.model_construct(
            time=time,
            self_id=event_self_id,
            post_type="message",
            sub_type="normal",
            user_id=OTHER_USER_ID,
            message_type="group",
            message_id=message_id,
            message=message if message is not None else raw,
            original_message=raw,
            raw_message="raw",
            font=0,
            sender=Sender.model_construct(
                user_id=OTHER_USER_ID,
                nickname="群友",
                card="群名片",
            ),
            to_me=to_me,
            reply=reply,
            group_id=GROUP_ID,
            anonymous=None,
        ),
    )


def _reply(
    *,
    message_id: int = 555,
    user_id: int = BOT_SELF_ID,
    text: str = "机器人上一条回复",
    time: int = PLATFORM_TIME - 10,
    message_type: str = "group",
    group_id: object = None,
) -> Reply:
    extra: dict[str, Any] = {}
    if group_id is not None:
        extra["group_id"] = group_id
    return Reply(
        time=time,
        message_type=message_type,
        message_id=message_id,
        real_id=message_id,
        sender=Sender.model_construct(user_id=user_id, nickname="小鞠", card=""),
        message=_message(text),
        **extra,
    )


async def _state(
    event: GroupMessageEvent,
    bot: OneBotV11Bot | None = None,
) -> T_State:
    state: T_State = {}
    await prepare_decision_dispatch(
        bot=cast("Any", bot or _ProbeBot()),
        event=cast("Any", event),
        state=state,
    )
    return state


async def test_same_dispatch_returns_same_carrier_with_frozen_dispatch_id() -> None:
    bot = _ProbeBot()
    event = _event()
    state = await _state(event, bot)

    first = get_decision_dispatch(cast("Any", bot), event, state)
    second = get_decision_dispatch(cast("Any", bot), event, state)

    assert first is second
    assert first.request.dispatch_id
    assert first.request.dispatch_id == state[DISPATCH_STATE_KEY].dispatch_id
    assert first.request.event.account_id == str(BOT_SELF_ID)
    assert first.request.event.group_id == str(GROUP_ID)
    assert first.request.event.message_id == str(MESSAGE_ID)
    assert first.request.event.protocol == "onebot_v11"


async def test_second_framework_dispatch_gets_new_holder_and_dispatch_id() -> None:
    bot = _ProbeBot()
    event = _event()

    first_state = await _state(event, bot)
    first = get_decision_dispatch(cast("Any", bot), event, first_state)
    second_state = await _state(event, bot)
    second = get_decision_dispatch(cast("Any", bot), event, second_state)

    assert first is not second
    assert first.request.dispatch_id != second.request.dispatch_id
    # 同一正文不等于同一事件：平台身份仍相同，分发标识不同。
    assert first.request.event == second.request.event


async def test_raw_original_message_text_and_platform_time_are_preserved() -> None:
    raw = _message(
        MessageSegment.at(BOT_SELF_ID),
        " 我不吃药！",
    )
    # 适配器已剥离前导 at 的 ``message``：投影必须读原始消息。
    event = _event(original=raw, message=_message("我不吃药！"))
    bot = _ProbeBot()
    state = await _state(event, bot)

    request = get_decision_dispatch(cast("Any", bot), event, state).request

    assert request.target.text == " 我不吃药！"
    assert request.target.timestamp == datetime.fromtimestamp(PLATFORM_TIME, UTC)
    assert request.received_at.tzinfo is not None
    # 本地接收截止点与平台时间彼此独立，不互相替代。
    assert request.received_at != request.target.timestamp
    assert request.target.sender_id == str(OTHER_USER_ID)
    assert request.target.display_name == "群名片"


async def test_native_at_segment_is_real_platform_summons() -> None:
    raw = _message(MessageSegment.at(BOT_SELF_ID), " 在吗")
    bot = _ProbeBot()
    request = get_decision_dispatch(
        cast("Any", bot),
        _event(original=raw),
        await _state(_event(original=raw), bot),
    ).request

    assert request.real_platform_at is True
    assert request.platform_summoned is True
    assert request.target.mention_ids == (str(BOT_SELF_ID),)


async def test_adapter_to_me_and_text_alias_do_not_prove_platform_summons() -> None:
    bot = _ProbeBot()
    to_me_event = _event(original=_message("我不吃药！"), to_me=True)
    to_me_request = get_decision_dispatch(
        cast("Any", bot), to_me_event, await _state(to_me_event, bot)
    ).request

    alias_text = "@小鞠知花 我不吃药！"
    alias_event = _event(original=_message(alias_text), to_me=True)
    alias_request = get_decision_dispatch(
        cast("Any", bot), alias_event, await _state(alias_event, bot)
    ).request

    for request in (to_me_request, alias_request):
        assert request.real_platform_at is False
        assert request.reply_to_bot is False
        assert request.platform_summoned is False
    # 判定使用未经框架剥离称呼的原始正文。
    assert alias_request.target.text == alias_text
    assert to_me_request.target.text == "我不吃药！"


async def test_cq_looking_text_never_becomes_native_facts() -> None:
    raw = _message("[CQ:at,qq=12345][CQ:reply,id=555] 我不吃药！")
    event = _event(original=raw)
    bot = _ProbeBot()
    request = get_decision_dispatch(
        cast("Any", bot), event, await _state(event, bot)
    ).request

    assert request.target.text == "[CQ:at,qq=12345][CQ:reply,id=555] 我不吃药！"
    assert request.target.mention_ids == ()
    assert request.target.reference_ids == ()
    assert request.real_platform_at is False
    assert request.reply_to_bot is False
    assert request.known_references == ()


async def test_matching_native_reply_to_bot_is_trusted_and_known() -> None:
    raw = _message(MessageSegment.reply(555), " 接着说")
    event = _event(original=raw, reply=_reply(message_id=555))
    bot = _ProbeBot()
    request = get_decision_dispatch(
        cast("Any", bot), event, await _state(event, bot)
    ).request

    assert request.reply_to_bot is True
    assert request.platform_summoned is True
    assert request.target.reference_ids == ("555",)
    assert len(request.known_references) == 1
    known = request.known_references[0]
    assert known.message_id == "555"
    assert known.text == "机器人上一条回复"
    assert known.is_bot is True
    assert known.group_id == str(GROUP_ID)


async def test_reply_to_other_member_is_known_reference_without_summons() -> None:
    raw = _message(MessageSegment.reply(555), " 接着说")
    event = _event(original=raw, reply=_reply(message_id=555, user_id=OTHER_USER_ID))
    bot = _ProbeBot()
    request = get_decision_dispatch(
        cast("Any", bot), event, await _state(event, bot)
    ).request

    assert request.reply_to_bot is False
    assert len(request.known_references) == 1
    assert request.known_references[0].is_bot is False


@pytest.mark.parametrize(
    ("reply", "raw"),
    [
        # 原生 reply 标识与引用消息标识不一致。
        (_reply(message_id=556), _message(MessageSegment.reply(555), " 接着说")),
        # 私聊引用不能成为可信引用或召唤。
        (
            _reply(message_id=555, message_type="private"),
            _message(MessageSegment.reply(555), " 接着说"),
        ),
        # 引用晚于本地接收截止点（未来引用）。
        (
            _reply(message_id=555, time=4_102_444_800),
            _message(MessageSegment.reply(555), " 接着说"),
        ),
        # 显式跨群矛盾。
        (
            _reply(message_id=555, group_id=GROUP_ID + 1),
            _message(MessageSegment.reply(555), " 接着说"),
        ),
        # 引用发送者缺失。
        (
            Reply(
                time=PLATFORM_TIME - 10,
                message_type="group",
                message_id=555,
                real_id=555,
                sender=Sender.model_construct(user_id=None),
                message=_message("原文"),
            ),
            _message(MessageSegment.reply(555), " 接着说"),
        ),
    ],
)
async def test_untrusted_reply_evidence_never_establishes_summons(
    reply: Reply,
    raw: Message,
) -> None:
    event = _event(original=raw, reply=reply)
    bot = _ProbeBot()
    request = get_decision_dispatch(
        cast("Any", bot), event, await _state(event, bot)
    ).request

    assert request.reply_to_bot is False
    assert request.known_references == ()
    # 原生引用边仍保留：缺失原文由既有引用补取沿该边处理。
    assert request.target.reference_ids == ("555",)


async def test_functional_command_comes_from_parsed_nonebot_command_state() -> None:
    bot = _ProbeBot()
    event = _event(original=_message("。帮助"))
    state = await _state(event, bot)
    state["_prefix"] = {"command": ("帮助",), "raw_command": "。帮助"}
    command_request = get_decision_dispatch(cast("Any", bot), event, state).request
    assert command_request.functional_command is True

    discussing_event = _event(original=_message("怎么用 。帮助 这个功能"))
    discussing_state = await _state(discussing_event, bot)
    discussing_state["_prefix"] = {"command": None}
    discussing_request = get_decision_dispatch(
        cast("Any", bot), discussing_event, discussing_state
    ).request
    assert discussing_request.functional_command is False

    punctuation_event = _event(original=_message("？"))
    punctuation_state = await _state(punctuation_event, bot)
    punctuation_request = get_decision_dispatch(
        cast("Any", bot), punctuation_event, punctuation_state
    ).request
    assert punctuation_request.functional_command is False


async def test_images_project_presence_only_without_payload_or_description() -> None:
    raw = _message(
        MessageSegment.image(file="base64://AAAABBBBCCCC"),
        " 看这个",
    )
    event = _event(original=raw)
    bot = _ProbeBot()
    request = get_decision_dispatch(
        cast("Any", bot), event, await _state(event, bot)
    ).request

    assert request.target.image_present is True
    assert request.target.image_description is None
    assert request.target.text == " 看这个"
    assert "base64" not in request.target.text


async def test_missing_holder_fails_closed() -> None:
    bot = _ProbeBot()
    event = _event()

    for state in (None, {}, {DISPATCH_STATE_KEY: object()}):
        with pytest.raises(DecisionDispatchUnavailableError):
            get_decision_dispatch(cast("Any", bot), event, cast("Any", state))


@pytest.mark.parametrize(
    ("overrides", "bot_self_id"),
    [
        ({"message_id": 1.5}, str(BOT_SELF_ID)),
        ({"message_id": True}, str(BOT_SELF_ID)),
        ({"time": 0}, str(BOT_SELF_ID)),
        ({"time": -3}, str(BOT_SELF_ID)),
        ({"time": "now"}, str(BOT_SELF_ID)),
        ({"event_self_id": 999}, str(BOT_SELF_ID)),
        ({}, "not-a-number"),
    ],
)
async def test_missing_or_invalid_required_facts_fail_closed(
    overrides: dict[str, object],
    bot_self_id: str,
) -> None:
    event = _event(**overrides)  # type: ignore[arg-type]
    bot = _ProbeBot(self_id=bot_self_id)
    state = await _state(event, bot)

    with pytest.raises(DecisionDispatchUnavailableError):
        get_decision_dispatch(cast("Any", bot), event, state)


async def test_non_onebot_event_or_group_message_requirement_fails_closed() -> None:
    from nonebot.adapters.onebot.v11.event import PrivateMessageEvent

    bot = _ProbeBot()
    private_event = PrivateMessageEvent.model_construct(
        time=PLATFORM_TIME,
        self_id=BOT_SELF_ID,
        post_type="message",
        sub_type="friend",
        user_id=OTHER_USER_ID,
        message_type="private",
        message_id=MESSAGE_ID,
        message=_message("悄悄话"),
        original_message=_message("悄悄话"),
        raw_message="悄悄话",
        font=0,
        sender=Sender.model_construct(user_id=OTHER_USER_ID, nickname="群友"),
        to_me=True,
        reply=None,
    )
    state: T_State = {}
    await prepare_decision_dispatch(
        bot=cast("Any", bot), event=cast("Any", private_event), state=state
    )

    with pytest.raises(DecisionDispatchUnavailableError):
        get_decision_dispatch(cast("Any", bot), cast("Any", private_event), state)
