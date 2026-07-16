"""SR 插件命令测试。"""

from __future__ import annotations

from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from nonebot.adapters.onebot.v11 import Adapter, Bot, GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.event import Sender

if TYPE_CHECKING:
    from nonebug import App


@pytest.fixture
def sr_module(app: App) -> Any:
    del app
    return import_module("komari_bot.plugins.sr")


class _StubConfigManager:
    def __init__(self, sr_list: list[str]) -> None:
        self._config = SimpleNamespace(plugin_enable=True, sr_list=sr_list)

    async def get_async(self) -> object:
        return self._config


class _StubPermissionManagerPlugin:
    @staticmethod
    async def check_runtime_permission(
        _bot: object,
        _event: object,
        _config: object,
    ) -> tuple[bool, str]:
        return True, ""

    @staticmethod
    def format_permission_info(_config: object) -> str:
        return "已启用"


class _StubCharacterBinding:
    @staticmethod
    def get_character_name(_user_id: str, fallback_nickname: str = "") -> str:
        return fallback_nickname


def _create_onebot_bot(ctx: Any) -> Bot:
    adapter = ctx.create_adapter(base=Adapter)
    return cast("Bot", ctx.create_bot(base=Bot, adapter=adapter, self_id="669293859"))


def _build_group_event(message_text: str = ".sr") -> GroupMessageEvent:
    message = Message(message_text)
    return GroupMessageEvent.model_construct(
        time=1,
        self_id=669293859,
        post_type="message",
        sub_type="normal",
        user_id=1047195267,
        message_type="group",
        message_id=123,
        message=message,
        original_message=message,
        raw_message=message_text,
        font=14,
        sender=Sender.model_construct(
            user_id=1047195267,
            nickname="测试用户",
            card="",
        ),
        to_me=False,
        reply=None,
        group_id=114514,
        anonymous=None,
    )


def _patch_sr_dependencies(
    sr_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    sr_list: list[str],
) -> None:
    monkeypatch.setattr(sr_module, "config_manager", _StubConfigManager(sr_list))
    monkeypatch.setattr(
        sr_module,
        "permission_manager_plugin",
        _StubPermissionManagerPlugin(),
    )
    monkeypatch.setattr(sr_module, "character_binding", _StubCharacterBinding())


@pytest.mark.asyncio
async def test_sr_empty_list_finishes_without_randint(
    app: App,
    sr_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sr_dependencies(sr_module, monkeypatch, sr_list=[])

    def fail_randint(_start: int, _end: int) -> int:
        msg = "空神人榜不应调用 randint"
        raise AssertionError(msg)

    monkeypatch.setattr(sr_module, "randint", fail_randint)

    async with app.test_matcher(sr_module.sr) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_group_event()
        ctx.receive_event(bot, event)
        ctx.should_ignore_permission(matcher=sr_module.sr)
        ctx.should_pass_rule(matcher=sr_module.sr)
        ctx.should_call_send(event, "神人榜为空，请先配置名单", bot=bot)
        ctx.should_finished()


@pytest.mark.asyncio
async def test_sr_non_empty_list_draws_item(
    app: App,
    sr_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sr_dependencies(sr_module, monkeypatch, sr_list=["甲", "乙"])
    monkeypatch.setattr(sr_module, "randint", lambda _start, _end: 1)

    async with app.test_matcher(sr_module.sr) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_group_event()
        ctx.receive_event(bot, event)
        ctx.should_ignore_permission(matcher=sr_module.sr)
        ctx.should_pass_rule(matcher=sr_module.sr)
        ctx.should_call_send(event, "测试用户抽到的神人是——\n2. 乙", bot=bot)
        ctx.should_finished()


@pytest.mark.asyncio
async def test_sr_manage_rejects_non_superuser_before_reading_config(
    app: App,
    sr_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailConfigManager:
        @staticmethod
        async def get_async() -> object:
            msg = "非超级用户不应读取配置"
            raise AssertionError(msg)

    async def _reject_superuser(_bot: object, _event: object) -> bool:
        return False

    monkeypatch.setattr(sr_module, "config_manager", _FailConfigManager())
    monkeypatch.setattr(sr_module, "SUPERUSER", _reject_superuser)

    async with app.test_matcher(sr_module.sr_manage) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_group_event(".sr status")
        ctx.receive_event(bot, event)
        ctx.should_ignore_permission(matcher=sr_module.sr_manage)
        ctx.should_pass_rule(matcher=sr_module.sr_manage)
        ctx.should_call_send(event, "❌ 仅限 SUPERUSER 使用", bot=bot)
        ctx.should_finished()


@pytest.mark.asyncio
async def test_sr_manage_allows_superuser_to_read_status(
    app: App,
    sr_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _allow_superuser(_bot: object, _event: object) -> bool:
        return True

    _patch_sr_dependencies(sr_module, monkeypatch, sr_list=[])
    monkeypatch.setattr(sr_module, "SUPERUSER", _allow_superuser)

    async with app.test_matcher(sr_module.sr_manage) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_group_event(".sr status")
        ctx.receive_event(bot, event)
        ctx.should_ignore_permission(matcher=sr_module.sr_manage)
        ctx.should_pass_rule(matcher=sr_module.sr_manage)
        ctx.should_call_send(event, "SR 已启用", bot=bot)
        ctx.should_finished()
