"""JRHG 主流程测试。"""

from __future__ import annotations

import asyncio
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
async def test_jrhg_function_reads_group_interaction_history_and_ignores_tail_text(
    app: App,
    jrhg_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction_history = {
        "summary": "最近常找小鞠聊天",
        "records": [{"event": "投喂", "result": "开心"}],
    }
    captured_prompts: list[dict[str, Any]] = []

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
        del user_nickname, daily_favor
        return ai_response

    async def _get_interaction_history(**_kwargs: object) -> dict[str, Any]:
        return interaction_history

    def _build_prompt(**kwargs: Any) -> list[dict[str, str]]:
        captured_prompts.append(kwargs)
        return [{"role": "user", "content": "prompt"}]

    async def _generate_reply(**_kwargs: object) -> str:
        return "生成回复"

    monkeypatch.setattr(
        jrhg_module,
        "generate_or_update_favorability",
        _generate_or_update_favorability,
    )
    monkeypatch.setattr(jrhg_module, "format_favor_response", _format_favor_response)
    monkeypatch.setattr(
        jrhg_module,
        "komari_memory_plugin",
        SimpleNamespace(
            get_plugin_manager=lambda: SimpleNamespace(
                memory=SimpleNamespace(
                    get_interaction_history=_get_interaction_history,
                )
            )
        ),
    )
    monkeypatch.setattr(jrhg_module, "build_prompt", _build_prompt)
    monkeypatch.setattr(jrhg_module, "generate_reply", _generate_reply)

    async with app.test_matcher(jrhg_module.jrhg) as ctx:
        bot = _create_onebot_bot(ctx)

        event = _build_group_event(".jrhg", message_id=1)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=jrhg_module.jrhg)
        ctx.should_pass_rule(matcher=jrhg_module.jrhg)
        ctx.should_call_send(event, "生成回复", bot=bot)
        ctx.should_finished()

        event_with_tail = _build_group_event(".jrhg 任意尾随文本", message_id=2)
        ctx.receive_event(bot, event_with_tail)
        ctx.should_pass_permission(matcher=jrhg_module.jrhg)
        ctx.should_pass_rule(matcher=jrhg_module.jrhg)
        ctx.should_call_send(event_with_tail, "生成回复", bot=bot)
        ctx.should_finished()

    assert captured_prompts == [
        {"daily_favor": 73, "interaction_history": interaction_history},
        {"daily_favor": 73, "interaction_history": interaction_history},
    ]


@pytest.mark.asyncio
async def test_jrhg_function_private_chat_uses_fallback_when_llm_fails(
    app: App,
    jrhg_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_prompts: list[dict[str, Any]] = []

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
        del user_nickname, daily_favor
        return ai_response

    def _build_prompt(**kwargs: Any) -> list[dict[str, str]]:
        captured_prompts.append(kwargs)
        return [{"role": "user", "content": "prompt"}]

    async def _generate_reply(**_kwargs: object) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(
        jrhg_module,
        "generate_or_update_favorability",
        _generate_or_update_favorability,
    )
    monkeypatch.setattr(jrhg_module, "format_favor_response", _format_favor_response)
    monkeypatch.setattr(
        jrhg_module,
        "komari_memory_plugin",
        SimpleNamespace(get_plugin_manager=lambda: None),
    )
    monkeypatch.setattr(jrhg_module, "build_prompt", _build_prompt)
    monkeypatch.setattr(jrhg_module, "generate_reply", _generate_reply)

    async with app.test_matcher(jrhg_module.jrhg) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".jrhg 任意尾随文本", message_id=3)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=jrhg_module.jrhg)
        ctx.should_pass_rule(matcher=jrhg_module.jrhg)
        ctx.should_call_send(
            event,
            jrhg_module._get_response(73, "阿虚"),
            bot=bot,
        )
        ctx.should_finished()

    assert captured_prompts == [{"daily_favor": 73, "interaction_history": None}]


def test_load_interaction_history_returns_none_when_memory_has_no_record(
    jrhg_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _get_interaction_history(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        jrhg_module,
        "komari_memory_plugin",
        SimpleNamespace(
            get_plugin_manager=lambda: SimpleNamespace(
                memory=SimpleNamespace(
                    get_interaction_history=_get_interaction_history,
                )
            )
        ),
    )

    result = asyncio.run(
        jrhg_module._load_interaction_history(user_id="10001", group_id="114514")
    )

    assert result is None
