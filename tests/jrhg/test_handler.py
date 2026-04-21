"""JRHG 主流程测试。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from nonebot.adapters.onebot.v11 import (
    Adapter,
    Bot,
    GroupMessageEvent,
    Message,
    PrivateMessageEvent,
)
from nonebot.adapters.onebot.v11.event import Sender

if TYPE_CHECKING:
    from collections.abc import Iterator

    from nonebug import App

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = PROJECT_ROOT / "komari_bot/plugins/jrhg/__init__.py"
PACKAGE_ROOT = PROJECT_ROOT / "komari_bot/plugins/jrhg"


@pytest.fixture
def jrhg_module(app: App) -> Iterator[Any]:
    del app
    original_package = sys.modules.get("komari_bot.plugins.jrhg")
    spec = importlib.util.spec_from_file_location(
        "komari_bot.plugins.jrhg",
        MODULE_PATH,
        submodule_search_locations=[str(PACKAGE_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise AssertionError

    module = importlib.util.module_from_spec(spec)
    sys.modules["komari_bot.plugins.jrhg"] = module
    spec.loader.exec_module(module)

    try:
        yield module
    finally:
        if original_package is not None:
            sys.modules["komari_bot.plugins.jrhg"] = original_package
        else:
            sys.modules.pop("komari_bot.plugins.jrhg", None)


def _build_sender(user_id: int, nickname: str = "阿虚") -> Sender:
    return Sender.model_construct(user_id=user_id, nickname=nickname, card="")


def _build_group_event(
    plain_text: str,
    *,
    message_id: int,
    group_id: int = 114514,
) -> GroupMessageEvent:
    message = Message(plain_text)
    return GroupMessageEvent.model_construct(
        time=1,
        self_id=669293859,
        post_type="message",
        sub_type="normal",
        user_id=10001,
        message_type="group",
        message_id=message_id,
        message=message,
        original_message=message,
        raw_message=plain_text,
        font=14,
        sender=_build_sender(10001),
        to_me=False,
        reply=None,
        group_id=group_id,
        anonymous=None,
    )


def _build_private_event(
    plain_text: str,
    *,
    message_id: int,
) -> PrivateMessageEvent:
    message = Message(plain_text)
    return PrivateMessageEvent.model_construct(
        time=1,
        self_id=669293859,
        post_type="message",
        sub_type="friend",
        user_id=10001,
        message_type="private",
        message_id=message_id,
        message=message,
        original_message=message,
        raw_message=plain_text,
        font=14,
        sender=_build_sender(10001),
        to_me=True,
        reply=None,
    )


def _create_onebot_bot(ctx: Any) -> Bot:
    adapter = ctx.create_adapter(base=Adapter)
    return cast("Bot", ctx.create_bot(base=Bot, adapter=adapter, self_id="669293859"))


@pytest.mark.asyncio
async def test_jrhg_function_queries_daily_favor_and_ignores_tail_text(
    app: App,
    jrhg_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_calls: list[dict[str, Any]] = []

    async def _generate_or_update_favorability(_user_id: str) -> object:
        return SimpleNamespace(
            daily_favor=73,
            cumulative_favor=114,
            is_new_day=False,
            favor_level="友好",
        )

    async def _format_favor_response(
        ai_response: str,
        user_nickname: str,
        daily_favor: int,
    ) -> str:
        captured_calls.append(
            {
                "ai_response": ai_response,
                "user_nickname": user_nickname,
                "daily_favor": daily_favor,
            }
        )
        return f"{user_nickname}:{daily_favor}"

    monkeypatch.setattr(
        jrhg_module,
        "generate_or_update_favorability",
        _generate_or_update_favorability,
    )
    monkeypatch.setattr(jrhg_module, "format_favor_response", _format_favor_response)

    async with app.test_matcher(jrhg_module.jrhg) as ctx:
        bot = _create_onebot_bot(ctx)

        event = _build_group_event(".jrhg", message_id=1)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=jrhg_module.jrhg)
        ctx.should_pass_rule(matcher=jrhg_module.jrhg)
        ctx.should_call_send(event, "阿虚:73", bot=bot)
        ctx.should_finished()

        event_with_tail = _build_group_event(".jrhg 任意尾随文本", message_id=2)
        ctx.receive_event(bot, event_with_tail)
        ctx.should_pass_permission(matcher=jrhg_module.jrhg)
        ctx.should_pass_rule(matcher=jrhg_module.jrhg)
        ctx.should_call_send(event_with_tail, "阿虚:73", bot=bot)
        ctx.should_finished()

    assert captured_calls == [
        {
            "ai_response": "",
            "user_nickname": "阿虚",
            "daily_favor": 73,
        },
        {
            "ai_response": "",
            "user_nickname": "阿虚",
            "daily_favor": 73,
        },
    ]


@pytest.mark.asyncio
async def test_jrhg_function_private_chat_uses_formatted_response(
    app: App,
    jrhg_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _generate_or_update_favorability(_user_id: str) -> object:
        return SimpleNamespace(
            daily_favor=73,
            cumulative_favor=114,
            is_new_day=False,
            favor_level="友好",
        )

    async def _format_favor_response(
        ai_response: str,
        user_nickname: str,
        daily_favor: int,
    ) -> str:
        del ai_response
        return f"{user_nickname} 今日好感 {daily_favor}"

    monkeypatch.setattr(
        jrhg_module,
        "generate_or_update_favorability",
        _generate_or_update_favorability,
    )
    monkeypatch.setattr(jrhg_module, "format_favor_response", _format_favor_response)

    async with app.test_matcher(jrhg_module.jrhg) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".jrhg 任意尾随文本", message_id=3)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=jrhg_module.jrhg)
        ctx.should_pass_rule(matcher=jrhg_module.jrhg)
        ctx.should_call_send(event, "阿虚 今日好感 73", bot=bot)
        ctx.should_finished()
