"""TSK-271 消费者必须复用已验证的群成员上下文。"""

from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace
from typing import Any, cast

import pytest
from nonebot.adapters.onebot.v11 import Adapter, Bot, GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.event import Sender
from nonebot.exception import FinishedException


class _StrictScopedBinding:
    """只接受 group_id/user_id，防止消费者偷偷回退到全局 user_id。"""

    def __init__(self, names: dict[tuple[str, str], str]) -> None:
        self.names = names
        self.lookup_calls: list[tuple[str, str, str | None]] = []

    def get_character_name(
        self,
        *,
        group_id: str,
        user_id: str,
        fallback_nickname: str | None = None,
    ) -> str:
        key = (str(group_id), str(user_id))
        self.lookup_calls.append((key[0], key[1], fallback_nickname))
        return self.names.get(key, fallback_nickname or user_id)

    def get_qq_character_name(
        self,
        *,
        app_id: str,
        group_openid: str,
        member_openid: str,
        fallback_nickname: str | None = None,
    ) -> str:
        del app_id
        return self.names.get(
            (group_openid, member_openid),
            fallback_nickname or member_openid,
        )


def _build_group_event(
    text: str = ".sr",
    *,
    user_id: int = 1047195267,
    group_id: int = 114514,
    self_id: int = 669293859,
) -> GroupMessageEvent:
    message = Message(text)
    return GroupMessageEvent.model_construct(
        time=1,
        self_id=self_id,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        message_id=1,
        group_id=group_id,
        message=message,
        original_message=message,
        raw_message=text,
        font=14,
        sender=Sender.model_construct(
            user_id=user_id,
            nickname="平台昵称",
            card="",
        ),
        to_me=False,
        reply=None,
        anonymous=None,
    )


def _create_bot(ctx: Any, *, self_id: str = "669293859") -> Bot:
    adapter = ctx.create_adapter(base=Adapter)
    return cast("Bot", ctx.create_bot(base=Bot, adapter=adapter, self_id=self_id))


async def _sr_config() -> SimpleNamespace:
    return SimpleNamespace(plugin_enable=True, sr_list=["甲"])


@pytest.mark.asyncio
async def test_sr_uses_verified_group_context_for_name(
    app: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sr_module = importlib.import_module("komari_bot.plugins.sr")
    binding = _StrictScopedBinding({("114514", "1047195267"): "群内角色"})
    monkeypatch.setattr(
        sr_module,
        "config_manager",
        SimpleNamespace(get_async=_sr_config),
    )
    monkeypatch.setattr(
        sr_module,
        "adjudicate",
        lambda *_args: SimpleNamespace(
            qualification=sr_module.AdmissionQualification.BUSINESS
        ),
    )
    monkeypatch.setattr(sr_module, "character_binding", binding)
    monkeypatch.setattr(sr_module, "randint", lambda _start, _end: 0)

    del app
    sent: list[str] = []

    async def _finish(message: object) -> None:
        sent.append(str(message))

    monkeypatch.setattr(sr_module.sr, "finish", _finish)
    await sr_module.sr_function(_build_group_event(), Message(""))

    assert ("114514", "1047195267", "平台昵称") in binding.lookup_calls
    assert sent == ["群内角色抽到的神人是——\n1. 甲"]


def test_prompt_builder_passes_canonical_context_to_binding_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.komari_chat.test_prompt_builder import (
        _build_config,
        _patch_dependencies,
    )

    prompt_builder = importlib.import_module(
        "komari_bot.plugins.komari_chat.services.prompt_builder"
    )
    _patch_dependencies(monkeypatch)
    binding = _StrictScopedBinding(
        {
            ("114514", "1047195267"): "群内角色",
            ("200000", "1047195267"): "另一群角色",
        }
    )
    monkeypatch.setattr(prompt_builder, "character_binding", binding)

    import asyncio

    messages = asyncio.run(
        prompt_builder.build_prompt(
            user_message="你好",
            memories=[],
            config=_build_config(),
            current_user_id="1047195267",
            current_user_nickname="平台昵称",
            group_id="114514",
        )
    )
    assert any("群内角色" in str(message["content"]) for message in messages)
    assert binding.lookup_calls

    other_messages = asyncio.run(
        prompt_builder.build_prompt(
            user_message="你好",
            memories=[],
            config=_build_config(),
            current_user_id="1047195267",
            current_user_nickname="平台昵称",
            group_id="200000",
        )
    )
    assert any("另一群角色" in str(message["content"]) for message in other_messages)
    assert ("114514", "1047195267", "平台昵称") in binding.lookup_calls
    assert ("200000", "1047195267", "平台昵称") in binding.lookup_calls


@pytest.mark.asyncio
async def test_summary_history_resolves_each_name_with_group_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planner = importlib.import_module(
        "komari_bot.plugins.group_history_summary.planner_service"
    )
    binding = _StrictScopedBinding(
        {
            ("114514", "1047195267"): "群一角色",
            ("200000", "1047195267"): "群二角色",
        }
    )
    monkeypatch.setattr(planner, "character_binding", binding)
    monkeypatch.setattr(planner, "ensure_effect_admitted", lambda **_kwargs: None)

    class _HistoryBot:
        async def call_api(self, _api: str, **_kwargs: object) -> object:
            return {
                "messages": [
                    {
                        "user_id": 1047195267,
                        "time": 1,
                        "message_seq": 2,
                        "message_id": 2,
                        "sender": {"nickname": "平台昵称"},
                        "message": [{"type": "text", "data": {"text": "消息二"}}],
                    },
                    {
                        "user_id": 1047195267,
                        "time": 2,
                        "message_seq": 1,
                        "message_id": 1,
                        "sender": {"nickname": "平台昵称"},
                        "message": [{"type": "text", "data": {"text": "消息一"}}],
                    },
                ]
            }

    bot = _HistoryBot()
    first = await planner._fetch_history_window(
        bot=cast("Any", bot),
        group_id="114514",
        count=2,
        batch_size=2,
    )
    second = await planner._fetch_history_window(
        bot=cast("Any", bot),
        group_id="200000",
        count=2,
        batch_size=2,
    )

    assert {message.nickname for message in first.messages} == {"群一角色"}
    assert {message.nickname for message in second.messages} == {"群二角色"}
    assert ("114514", "1047195267", "平台昵称") in binding.lookup_calls
    assert ("200000", "1047195267", "平台昵称") in binding.lookup_calls


@pytest.fixture
def custom_module(app: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    del app
    import nonebot.plugin

    original_require = nonebot.plugin.require

    def _require(plugin_name: str) -> object:
        if plugin_name in {"nonebot_plugin_apscheduler", "komari_knowledge"}:
            return SimpleNamespace()
        return original_require(plugin_name)

    monkeypatch.setattr(nonebot.plugin, "require", _require)
    package_name = "komari_bot.plugins.komari_custom"
    shim = sys.modules.pop(package_name, None)
    module = importlib.import_module(package_name)
    if shim is not None:
        monkeypatch.setitem(sys.modules, package_name, shim)
    return module


def test_custom_proposer_name_uses_event_context(
    custom_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 这里调用现有提交路径使用的解析函数，避免伪造不存在的 matcher 或
    # 让测试专用 helper 代替 .custom 的真实消费者。
    binding = _StrictScopedBinding({("114514", "1047195267"): "群内角色"})
    monkeypatch.setattr(custom_module, "character_binding", binding)
    event = _build_group_event(".custom")

    assert custom_module._resolve_proposer_name(event) == "群内角色"


@pytest.mark.asyncio
async def test_debug_bind_set_uses_current_group_context(
    app: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    debug_commands = importlib.import_module("komari_bot.plugins.komari_debug.commands")
    calls: list[dict[str, object]] = []

    class _Manager:
        async def set_group_character_name(
            self,
            group_id: str,
            user_id: str,
            character_name: str,
        ) -> None:
            assert group_id == "12345"
            assert user_id == "42"
            assert character_name == "群内角色"
            calls.append(
                {
                    "group_id": group_id,
                    "user_id": user_id,
                    "character_name": character_name,
                }
            )

    monkeypatch.setattr(debug_commands, "get_binding_manager", lambda: _Manager())

    async def _fake_basic_result(**_kwargs: object) -> str:
        return "binding-result"

    monkeypatch.setattr(
        debug_commands,
        "_build_basic_command_result",
        _fake_basic_result,
    )

    event = _build_group_event(
        ".debug bind set 42 群内角色",
        user_id=42,
        group_id=12345,
        self_id=1001,
    )

    sent: list[str] = []

    async def _authorized_superuser(*_args: object, **_kwargs: object) -> bool:
        return True

    async def _finish(message: object) -> None:
        sent.append(str(message))
        raise FinishedException

    # Use a real NoneBug Bot and make authorization an explicit test seam.  The
    # consumer assertion is about forwarding the current group context; the
    # independent komari_debug tests cover the production SUPERUSER rule.
    monkeypatch.setattr(debug_commands, "SUPERUSER", _authorized_superuser)
    monkeypatch.setattr(debug_commands.debug_bind_set, "finish", _finish)
    async with app.test_matcher() as ctx:
        bot = _create_bot(ctx, self_id="1001")
        with pytest.raises(FinishedException):
            await debug_commands.handle_debug_bind_set(
                bot=bot,
                event=event,
                arg_text="42 群内角色",
            )

    assert calls == [
        {
            "group_id": "12345",
            "user_id": "42",
            "character_name": "群内角色",
        }
    ]
    assert sent == ["binding-result"]
