"""character_binding 命令测试。"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any, cast, get_type_hints

import pytest
from nonebot.adapters.onebot.v11 import Adapter, Bot, Message, PrivateMessageEvent
from nonebot.adapters.onebot.v11.event import Sender

from komari_bot.plugins.character_binding.manager import CharacterBindingManager

if TYPE_CHECKING:
    from nonebug import App


@pytest.fixture
def commands_module(app: App) -> Any:
    del app
    return import_module("komari_bot.plugins.character_binding.commands")


@pytest.fixture
def manager_module(app: App) -> Any:
    del app
    return import_module("komari_bot.plugins.character_binding.manager")


class _StubManager(CharacterBindingManager):
    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        self.bindings = dict(bindings or {})

    def has_binding(self, user_id: str) -> bool:
        return user_id in self.bindings

    def get_character_name(
        self,
        user_id: str,
        fallback_nickname: str | None = None,
    ) -> str:
        if user_id in self.bindings:
            return self.bindings[user_id]
        if fallback_nickname:
            return fallback_nickname
        return user_id

    async def set_character_name(self, user_id: str, character_name: str) -> None:
        self.bindings[user_id] = character_name

    async def remove_character_name(self, user_id: str) -> bool:
        if user_id not in self.bindings:
            return False
        del self.bindings[user_id]
        return True

    def list_bindings(self) -> dict[str, str]:
        return self.bindings.copy()


def _build_private_event(
    plain_text: str,
    *,
    user_id: int = 42,
    message_id: int = 1,
) -> PrivateMessageEvent:
    message = Message(plain_text)
    return PrivateMessageEvent.model_construct(
        time=1,
        self_id=669293859,
        post_type="message",
        sub_type="friend",
        user_id=user_id,
        message_type="private",
        message_id=message_id,
        message=message,
        original_message=message,
        raw_message=plain_text,
        font=14,
        sender=Sender.model_construct(user_id=user_id, nickname="tester", card=""),
        to_me=True,
        reply=None,
    )


def _create_onebot_bot(ctx: Any) -> Bot:
    adapter = ctx.create_adapter(base=Adapter)
    return cast("Bot", ctx.create_bot(base=Bot, adapter=adapter, self_id="669293859"))


def test_runtime_type_hints_can_resolve_onebot_message_types(
    commands_module: Any,
) -> None:
    event_hints = get_type_hints(commands_module.get_event_user_id)
    message_hints = get_type_hints(commands_module.get_command_text)

    assert event_hints["event"].__name__ == "MessageEvent"
    assert message_hints["args"].__name__ == "Message"


def test_parse_superuser_bind_set_request_supports_explicit_target(
    commands_module: Any,
) -> None:
    request = commands_module.parse_superuser_bind_set_request(
        user_id="42",
        arg_text="10086 柊镜",
    )

    assert request.operator_user_id == "42"
    assert request.target_user_id == "10086"
    assert request.character_name == "柊镜"
    assert request.specified_target is True


def test_parse_self_bind_set_request_always_targets_self(
    commands_module: Any,
) -> None:
    request = commands_module.parse_self_bind_set_request(
        user_id="42",
        arg_text="泉此方",
    )

    assert request.operator_user_id == "42"
    assert request.target_user_id == "42"
    assert request.character_name == "泉此方"
    assert request.specified_target is False


@pytest.mark.asyncio
async def test_handle_set_superuser_sets_other_user_binding_with_nonebug(
    app: App,
    commands_module: Any,
    manager_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _StubManager()
    monkeypatch.setattr(manager_module, "_manager_instance", manager)

    async with app.test_matcher(commands_module.bind_set_superuser) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".bind set 10086 柊镜")
        ctx.receive_event(bot, event)
        ctx.should_ignore_permission(matcher=commands_module.bind_set_superuser)
        ctx.should_pass_rule(matcher=commands_module.bind_set_superuser)
        ctx.should_call_send(event, "✅ 已为用户 10086 设置角色名为 柊镜", bot=bot)
        ctx.should_finished()

    assert manager.bindings == {"10086": "柊镜"}


@pytest.mark.asyncio
async def test_handle_del_superuser_removes_other_user_binding_with_nonebug(
    app: App,
    commands_module: Any,
    manager_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _StubManager({"10086": "柊镜"})
    monkeypatch.setattr(manager_module, "_manager_instance", manager)

    async with app.test_matcher(commands_module.bind_del_superuser) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".bind del 10086")
        ctx.receive_event(bot, event)
        ctx.should_ignore_permission(matcher=commands_module.bind_del_superuser)
        ctx.should_pass_rule(matcher=commands_module.bind_del_superuser)
        ctx.should_call_send(event, "✅ 已删除用户 10086 的角色绑定", bot=bot)
        ctx.should_finished()

    assert manager.bindings == {}


@pytest.mark.asyncio
async def test_handle_list_superuser_returns_all_bindings_with_nonebug(
    app: App,
    commands_module: Any,
    manager_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _StubManager({"42": "泉此方", "10086": "柊镜"})
    monkeypatch.setattr(manager_module, "_manager_instance", manager)

    async with app.test_matcher(commands_module.bind_list_superuser) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".bind list")
        ctx.receive_event(bot, event)
        ctx.should_ignore_permission(matcher=commands_module.bind_list_superuser)
        ctx.should_pass_rule(matcher=commands_module.bind_list_superuser)
        ctx.should_call_send(
            event,
            "📋 所有角色绑定列表：\n  42: 泉此方\n  10086: 柊镜",
            bot=bot,
        )
        ctx.should_finished()


@pytest.mark.asyncio
async def test_handle_list_only_returns_current_user_binding_with_nonebug(
    app: App,
    commands_module: Any,
    manager_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _StubManager({"42": "泉此方", "10086": "柊镜"})
    monkeypatch.setattr(manager_module, "_manager_instance", manager)

    async with app.test_matcher(commands_module.bind_list) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".bind list")
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=commands_module.bind_list)
        ctx.should_pass_rule(matcher=commands_module.bind_list)
        ctx.should_call_send(event, "📋 您的角色绑定: 泉此方", bot=bot)
        ctx.should_finished()
