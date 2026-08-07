"""SR 插件命令测试。"""

from __future__ import annotations

import asyncio
from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from nonebot.adapters.onebot.v11 import Adapter, Bot, GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.event import Sender
from pydantic import ValidationError

from komari_bot.onebot.onebot_messages import plain_text_message

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


class _AtomicListConfigManager:
    def __init__(self, sr_list: list[str]) -> None:
        self.sr_list = list(sr_list)
        self._lock = asyncio.Lock()

    async def mutate_field_async(self, field_name: str, mutator: Any) -> object:
        assert field_name == "sr_list"
        async with self._lock:
            await asyncio.sleep(0)
            self.sr_list = list(mutator(list(self.sr_list)))
            return SimpleNamespace(sr_list=list(self.sr_list))


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
        ctx.should_call_send(
            event,
            plain_text_message("测试用户抽到的神人是——\n2. 乙"),
            bot=bot,
        )
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
        ctx.should_call_send(event, plain_text_message("SR 已启用"), bot=bot)
        ctx.should_finished()


@pytest.mark.asyncio
async def test_concurrent_add_commands_preserve_both_items(sr_module: Any) -> None:
    manager = _AtomicListConfigManager(["甲"])

    first, second = await asyncio.gather(
        sr_module.AddCommand("乙", manager).execute(),
        sr_module.AddCommand("丙", manager).execute(),
    )

    assert first.startswith("✅")
    assert second.startswith("✅")
    assert manager.sr_list == ["甲", "乙", "丙"]


@pytest.mark.asyncio
async def test_concurrent_add_and_delete_do_not_overwrite_each_other(
    sr_module: Any,
) -> None:
    manager = _AtomicListConfigManager(["甲"])
    delete_command = sr_module.DeleteCommand(item="甲", config_manager=manager)

    add_result, delete_result = await asyncio.gather(
        sr_module.AddCommand("乙", manager).execute(),
        delete_command.execute(),
    )

    assert add_result.startswith("✅")
    assert delete_result.startswith("✅")
    assert manager.sr_list == ["乙"]
    assert delete_command.item == "甲"


@pytest.mark.asyncio
async def test_delete_undo_restores_position_without_losing_new_items(
    sr_module: Any,
) -> None:
    manager = _AtomicListConfigManager(["甲", "乙"])
    delete_command = sr_module.DeleteCommand(index=1, config_manager=manager)

    assert (await delete_command.execute()).startswith("✅")
    await sr_module.AddCommand("丙", manager).execute()
    undo_result = await delete_command.undo()

    assert undo_result.startswith("↩️")
    assert manager.sr_list == ["甲", "乙", "丙"]


def test_sr_list_rejects_excessive_count_and_item_size(sr_module: Any) -> None:
    del sr_module
    config_module = import_module("komari_bot.plugins.sr.config_schema")

    with pytest.raises(ValidationError, match="最多允许 500 项"):
        config_module.DynamicConfigSchema(
            sr_list=[
                f"项目 {index}"
                for index in range(config_module.MAX_SR_LIST_ITEMS + 1)
            ]
        )

    with pytest.raises(ValidationError, match="字符上限"):
        config_module.DynamicConfigSchema(sr_list=["超长" * 65])


def test_sr_list_normalizes_items_and_rejects_duplicates(sr_module: Any) -> None:
    del sr_module
    config_module = import_module("komari_bot.plugins.sr.config_schema")

    config = config_module.DynamicConfigSchema(sr_list=[" 甲 ", "乙"])
    assert config.sr_list == ["甲", "乙"]

    with pytest.raises(ValidationError, match="重复项目"):
        config_module.DynamicConfigSchema(sr_list=["甲", " 甲 "])


@pytest.mark.asyncio
async def test_main_operation_reports_success_when_undo_storage_fails(
    sr_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fail_push(*_args: object) -> None:
        msg = "Redis unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr(sr_module, "push_undo", _fail_push)

    result = await sr_module._record_undo_or_warn(
        "user",
        object(),
        "✅ 已添加 '甲' 到神人榜",
    )

    assert result.startswith("✅")
    assert "操作已生效" in result
    assert "撤销记录保存失败" in result


@pytest.mark.asyncio
async def test_undo_keeps_record_when_config_mutation_fails(
    sr_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingManager:
        @staticmethod
        async def mutate_field_async(_field_name: str, _mutator: Any) -> object:
            msg = "PostgreSQL unavailable"
            raise RuntimeError(msg)

    async def _peek(*_args: object) -> dict[str, object]:
        return {
            "token": "undo-token",
            "type": "AddCommand",
            "item": "甲",
            "index": None,
        }

    pop_calls = 0

    async def _pop(*_args: object) -> bool:
        nonlocal pop_calls
        pop_calls += 1
        return True

    monkeypatch.setattr(sr_module, "config_manager", _FailingManager())
    monkeypatch.setattr(sr_module, "peek_undo", _peek)
    monkeypatch.setattr(sr_module, "pop_undo_if_token", _pop)

    with pytest.raises(RuntimeError, match="PostgreSQL unavailable"):
        await sr_module._undo_latest("user")

    assert pop_calls == 0


@pytest.mark.asyncio
async def test_undo_only_pops_matching_token_after_success(
    sr_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _AtomicListConfigManager(["甲"])

    async def _peek(*_args: object) -> dict[str, object]:
        return {
            "token": "undo-token",
            "type": "AddCommand",
            "item": "甲",
            "index": None,
        }

    popped_tokens: list[str] = []

    async def _pop(_user_id: str, token: str, _manager: object) -> bool:
        popped_tokens.append(token)
        return True

    monkeypatch.setattr(sr_module, "config_manager", manager)
    monkeypatch.setattr(sr_module, "peek_undo", _peek)
    monkeypatch.setattr(sr_module, "pop_undo_if_token", _pop)

    result = await sr_module._undo_latest("user")

    assert result.startswith("↩️")
    assert manager.sr_list == []
    assert popped_tokens == ["undo-token"]
