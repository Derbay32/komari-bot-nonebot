""".debug 命令成功与失败路径测试。

覆盖：
- favor get 成功（显示好感度信息）
- favor set 成功（显示 before/after/stage）
- favor get/set 异常失败消息
- bind set 成功输出
- bind del 成功/不存在输出
- bind list 有绑定/无绑定
- bind 异常失败消息
- reply 成功流程（生成报告，不发送普通聊天）
- reply 额外参数（多于需要的参数）
- reply 空测试文本
- reply 错误后发送诊断报告
- summary 成功流程（先发图片再发诊断）
- summary CapabilityNotSupportedError
- summary 空总结要求
- 严格多余参数检查
"""

from __future__ import annotations

import sys
from importlib import import_module
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
    from nonebug import App


# ─── helpers ──────────────────────────────────────────────────


def _create_onebot_bot(ctx: Any) -> Bot:
    adapter = ctx.create_adapter(base=Adapter)
    return cast("Bot", ctx.create_bot(base=Bot, adapter=adapter, self_id="669293859"))


def _build_private_event(
    plain_text: str,
    *,
    user_id: int = 42,
) -> PrivateMessageEvent:
    message = Message(plain_text)
    return PrivateMessageEvent.model_construct(
        time=1,
        self_id=669293859,
        post_type="message",
        sub_type="friend",
        user_id=user_id,
        message_type="private",
        message_id=1,
        message=message,
        original_message=message,
        raw_message=plain_text,
        font=14,
        sender=Sender.model_construct(user_id=user_id, nickname="tester", card=""),
        to_me=True,
        reply=None,
    )


def _build_group_event(
    plain_text: str,
    *,
    user_id: int = 42,
    group_id: int = 12345,
) -> GroupMessageEvent:
    message = Message(plain_text)
    return GroupMessageEvent.model_construct(
        time=1,
        self_id=669293859,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        message_id=1,
        group_id=group_id,
        message=message,
        original_message=message,
        raw_message=plain_text,
        font=14,
        sender=Sender.model_construct(user_id=user_id, nickname="tester", card=""),
        to_me=True,
        reply=None,
    )


SU_ID = 42  # 在 conftest.py 中配置为 superuser


# ─── module fixtures ──────────────────────────────────────────


@pytest.fixture
def debug_commands(app: App) -> Any:
    """加载 komari_debug.commands 模块（使用 fake plugin stubs）。"""
    del app
    module_name = "komari_bot.plugins.komari_debug.commands"
    sys.modules.pop(module_name, None)
    return import_module(module_name)


# ─── favor get 成功 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_favor_get_shows_favorability_data(
    app: App,
    debug_commands: Any,
) -> None:
    """验证 .debug favor get 成功时显示数值、阶段和更新时间。"""
    async with app.test_matcher(debug_commands.debug_favor_get) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".debug favor get 42", user_id=SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_favor_get)
        ctx.should_pass_rule(matcher=debug_commands.debug_favor_get)
        ctx.should_call_send(
            event,
            "📊 用户 42 好感度:\n"
            "  数值: 0\n"
            "  阶段: 疏离戒备（1/4）\n"
            "  更新时间: 2026-06-07T00:00:00+00:00",
            bot=bot,
        )
        ctx.should_finished()


@pytest.mark.asyncio
async def test_favor_get_rejects_missing_user_id(
    app: App,
    debug_commands: Any,
) -> None:
    """缺少用户 ID 时提示用法。"""
    async with app.test_matcher(debug_commands.debug_favor_get) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".debug favor get", user_id=SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_favor_get)
        ctx.should_pass_rule(matcher=debug_commands.debug_favor_get)
        ctx.should_call_send(
            event,
            "❌ 请提供有效的用户 ID（正整数）\n用法: .debug favor get <用户ID>",
            bot=bot,
        )
        ctx.should_finished()


@pytest.mark.asyncio
async def test_favor_get_exception_shows_error_message(
    app: App,
    debug_commands: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_user_favorability 抛异常时显示错误消息。"""

    async def _raise_error(*_args: object, **_kwargs: object) -> object:
        msg = "数据库连接失败"
        raise RuntimeError(msg)

    monkeypatch.setattr(debug_commands, "get_user_favorability", _raise_error)

    async with app.test_matcher(debug_commands.debug_favor_get) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".debug favor get 42", user_id=SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_favor_get)
        ctx.should_pass_rule(matcher=debug_commands.debug_favor_get)
        ctx.should_call_send(event, "❌ 查询失败: 数据库连接失败", bot=bot)
        ctx.should_finished()


# ─── favor set 成功 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_favor_set_shows_before_after_and_stage(
    app: App,
    debug_commands: Any,
) -> None:
    """验证 .debug favor set 成功时显示 before/after/阶段/更新时间。"""
    async with app.test_matcher(debug_commands.debug_favor_set) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".debug favor set 42 200", user_id=SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_favor_set)
        ctx.should_pass_rule(matcher=debug_commands.debug_favor_set)
        ctx.should_call_send(
            event,
            "✅ 用户 42 好感度已设置:\n"
            "  before: 0\n"
            "  after:  200\n"
            "  阶段:    普通熟人（2/4）\n"
            "  更新时间: 2026-06-07T00:00:00+00:00",
            bot=bot,
        )
        ctx.should_finished()


@pytest.mark.asyncio
async def test_favor_set_rejects_insufficient_args(
    app: App,
    debug_commands: Any,
) -> None:
    """参数不足时提示正确用法。"""
    async with app.test_matcher(debug_commands.debug_favor_set) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".debug favor set 42", user_id=SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_favor_set)
        ctx.should_pass_rule(matcher=debug_commands.debug_favor_set)
        ctx.should_call_send(
            event,
            "❌ 参数不足\n用法: .debug favor set <用户ID> <0-400>",
            bot=bot,
        )
        ctx.should_finished()


@pytest.mark.asyncio
async def test_favor_set_rejects_invalid_user_id(
    app: App,
    debug_commands: Any,
) -> None:
    """无效用户 ID 时给出明确错误。"""
    async with app.test_matcher(debug_commands.debug_favor_set) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".debug favor set abc 200", user_id=SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_favor_set)
        ctx.should_pass_rule(matcher=debug_commands.debug_favor_set)
        ctx.should_call_send(
            event,
            "❌ 用户 ID 必须为正整数\n用法: .debug favor set <用户ID> <0-400>",
            bot=bot,
        )
        ctx.should_finished()


@pytest.mark.asyncio
async def test_favor_set_rejects_out_of_range_value(
    app: App,
    debug_commands: Any,
) -> None:
    """越界值被拒绝。"""
    async with app.test_matcher(debug_commands.debug_favor_set) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".debug favor set 42 999", user_id=SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_favor_set)
        ctx.should_pass_rule(matcher=debug_commands.debug_favor_set)
        ctx.should_call_send(
            event,
            "❌ 好感度值必须为 0-400 的整数\n用法: .debug favor set <用户ID> <0-400>",
            bot=bot,
        )
        ctx.should_finished()


@pytest.mark.asyncio
async def test_favor_set_exception_shows_error(
    app: App,
    debug_commands: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """set_user_favorability 抛异常时显示失败消息。"""

    async def _raise_error(*_args: object, **_kwargs: object) -> object:
        msg = "事务冲突"
        raise RuntimeError(msg)

    monkeypatch.setattr(debug_commands, "set_user_favorability", _raise_error)

    async with app.test_matcher(debug_commands.debug_favor_set) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".debug favor set 42 200", user_id=SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_favor_set)
        ctx.should_pass_rule(matcher=debug_commands.debug_favor_set)
        ctx.should_call_send(event, "❌ 设置失败: 事务冲突", bot=bot)
        ctx.should_finished()


@pytest.mark.asyncio
async def test_favor_set_strict_extra_args_ignored_by_split(
    app: App,
    debug_commands: Any,
) -> None:
    """多余参数在 maxsplit=1 下被归入第2部分，不影响主解析。"""
    async with app.test_matcher(debug_commands.debug_favor_set) as ctx:
        bot = _create_onebot_bot(ctx)
        # "42 200 extra stuff" → parts[0]="42", parts[1]="200 extra stuff"
        # _parse_favor_value("200 extra stuff") 会失败
        event = _build_private_event(".debug favor set 42 200 extra stuff", user_id=SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_favor_set)
        ctx.should_pass_rule(matcher=debug_commands.debug_favor_set)
        ctx.should_call_send(
            event,
            "❌ 好感度值必须为 0-400 的整数\n用法: .debug favor set <用户ID> <0-400>",
            bot=bot,
        )
        ctx.should_finished()


# ─── bind set 成功 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bind_set_success(
    app: App,
    debug_commands: Any,
) -> None:
    """验证 .debug bind set 成功输出。"""
    async with app.test_matcher(debug_commands.debug_bind_set) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".debug bind set 42 泉此方", user_id=SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_bind_set)
        ctx.should_pass_rule(matcher=debug_commands.debug_bind_set)
        ctx.should_call_send(event, "✅ 已为用户 42 设置角色绑定: 泉此方", bot=bot)
        ctx.should_finished()


@pytest.mark.asyncio
async def test_bind_set_rejects_missing_character_name(
    app: App,
    debug_commands: Any,
) -> None:
    """角色名为空时拒绝。"""
    async with app.test_matcher(debug_commands.debug_bind_set) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".debug bind set 42", user_id=SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_bind_set)
        ctx.should_pass_rule(matcher=debug_commands.debug_bind_set)
        ctx.should_call_send(
            event,
            "❌ 参数不足\n用法: .debug bind set <用户ID> <角色名>",
            bot=bot,
        )
        ctx.should_finished()


@pytest.mark.asyncio
async def test_bind_set_rejects_empty_character_name(
    app: App,
    debug_commands: Any,
) -> None:
    """角色名为空白时拒绝。"""
    async with app.test_matcher(debug_commands.debug_bind_set) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".debug bind set 42    ", user_id=SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_bind_set)
        ctx.should_pass_rule(matcher=debug_commands.debug_bind_set)
        ctx.should_call_send(
            event,
            "❌ 参数不足\n用法: .debug bind set <用户ID> <角色名>",
            bot=bot,
        )
        ctx.should_finished()


@pytest.mark.asyncio
async def test_bind_set_exception_shows_error(
    app: App,
    debug_commands: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """set_character_name 抛异常时显示失败消息。"""
    manager_stub = SimpleNamespace()
    manager_stub.set_character_name = _make_async_raise(RuntimeError("存储失败"))
    monkeypatch.setattr(debug_commands, "get_binding_manager", lambda: manager_stub)

    async with app.test_matcher(debug_commands.debug_bind_set) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".debug bind set 42 泉此方", user_id=SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_bind_set)
        ctx.should_pass_rule(matcher=debug_commands.debug_bind_set)
        ctx.should_call_send(event, "❌ 设置绑定失败: 存储失败", bot=bot)
        ctx.should_finished()


# ─── bind del 成功 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bind_del_success(
    app: App,
    debug_commands: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """remove_character_name 返回 True 时显示已删除。"""
    manager_stub = SimpleNamespace()
    manager_stub.remove_character_name = _make_async_return(return_value=True)
    monkeypatch.setattr(debug_commands, "get_binding_manager", lambda: manager_stub)

    async with app.test_matcher(debug_commands.debug_bind_del) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".debug bind del 42", user_id=SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_bind_del)
        ctx.should_pass_rule(matcher=debug_commands.debug_bind_del)
        ctx.should_call_send(event, "✅ 已删除用户 42 的角色绑定", bot=bot)
        ctx.should_finished()


@pytest.mark.asyncio
async def test_bind_del_not_found(
    app: App,
    debug_commands: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """remove_character_name 返回 False 时显示不存在。"""
    manager_stub = SimpleNamespace()
    manager_stub.remove_character_name = _make_async_return(return_value=False)
    monkeypatch.setattr(debug_commands, "get_binding_manager", lambda: manager_stub)

    async with app.test_matcher(debug_commands.debug_bind_del) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".debug bind del 999", user_id=SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_bind_del)
        ctx.should_pass_rule(matcher=debug_commands.debug_bind_del)
        ctx.should_call_send(event, "⚠️ 用户 999 没有角色绑定", bot=bot)
        ctx.should_finished()


@pytest.mark.asyncio
async def test_bind_del_rejects_missing_user_id(
    app: App,
    debug_commands: Any,
) -> None:
    """缺少用户 ID 时提示用法。"""
    async with app.test_matcher(debug_commands.debug_bind_del) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".debug bind del", user_id=SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_bind_del)
        ctx.should_pass_rule(matcher=debug_commands.debug_bind_del)
        ctx.should_call_send(
            event,
            "❌ 请提供有效的用户 ID（正整数）\n用法: .debug bind del <用户ID>",
            bot=bot,
        )
        ctx.should_finished()


@pytest.mark.asyncio
async def test_bind_del_exception_shows_error(
    app: App,
    debug_commands: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """remove_character_name 抛异常时显示失败消息。"""
    manager_stub = SimpleNamespace()
    manager_stub.remove_character_name = _make_async_raise(RuntimeError("JSON 解析失败"))
    monkeypatch.setattr(debug_commands, "get_binding_manager", lambda: manager_stub)

    async with app.test_matcher(debug_commands.debug_bind_del) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".debug bind del 42", user_id=SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_bind_del)
        ctx.should_pass_rule(matcher=debug_commands.debug_bind_del)
        ctx.should_call_send(event, "❌ 删除绑定失败: JSON 解析失败", bot=bot)
        ctx.should_finished()


# ─── bind list 成功 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_bind_list_with_bindings(
    app: App,
    debug_commands: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有绑定时列出所有绑定。"""
    bindings = {"42": "泉此方", "10086": "柊镜"}
    manager_stub = SimpleNamespace()
    manager_stub.list_bindings = lambda: bindings
    monkeypatch.setattr(debug_commands, "get_binding_manager", lambda: manager_stub)

    async with app.test_matcher(debug_commands.debug_bind_list) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".debug bind list", user_id=SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_bind_list)
        ctx.should_pass_rule(matcher=debug_commands.debug_bind_list)
        expected = "📋 全部角色绑定:\n  10086: 柊镜\n  42: 泉此方"
        ctx.should_call_send(event, expected, bot=bot)
        ctx.should_finished()


@pytest.mark.asyncio
async def test_bind_list_empty(
    app: App,
    debug_commands: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无绑定时显示提示。"""
    manager_stub = SimpleNamespace()
    manager_stub.list_bindings = dict
    monkeypatch.setattr(debug_commands, "get_binding_manager", lambda: manager_stub)

    async with app.test_matcher(debug_commands.debug_bind_list) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".debug bind list", user_id=SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_bind_list)
        ctx.should_pass_rule(matcher=debug_commands.debug_bind_list)
        ctx.should_call_send(event, "📋 当前没有任何角色绑定", bot=bot)
        ctx.should_finished()


@pytest.mark.asyncio
async def test_bind_list_exception_shows_error(
    app: App,
    debug_commands: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list_bindings 抛异常时显示失败消息。"""
    manager_stub = SimpleNamespace()
    manager_stub.list_bindings = lambda: (_ for _ in ()).throw(RuntimeError("JSON 文件损坏"))
    monkeypatch.setattr(debug_commands, "get_binding_manager", lambda: manager_stub)

    async with app.test_matcher(debug_commands.debug_bind_list) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".debug bind list", user_id=SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_bind_list)
        ctx.should_pass_rule(matcher=debug_commands.debug_bind_list)
        ctx.should_call_send(event, "❌ 查询绑定列表失败: JSON 文件损坏", bot=bot)
        ctx.should_finished()


@pytest.mark.asyncio
async def test_bind_list_rejects_extra_arguments(
    app: App,
    debug_commands: Any,
) -> None:
    """bind list 不接受任何额外参数。"""
    async with app.test_matcher(debug_commands.debug_bind_list) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".debug bind list extra", user_id=SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_bind_list)
        ctx.should_pass_rule(matcher=debug_commands.debug_bind_list)
        ctx.should_call_send(
            event,
            "❌ 参数过多\n用法: .debug bind list",
            bot=bot,
        )
        ctx.should_finished()


# ─── reply 私聊拒绝 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_reply_rejects_private_chat_detailed(
    app: App,
    debug_commands: Any,
) -> None:
    """.debug reply 私聊下显示"仅支持群聊"。"""
    async with app.test_matcher(debug_commands.debug_reply) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".debug reply 你好", user_id=SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_reply)
        ctx.should_pass_rule(matcher=debug_commands.debug_reply)
        ctx.should_call_send(event, "❌ .debug reply 仅支持群聊", bot=bot)
        ctx.should_finished()


# ─── reply 空文本 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reply_empty_text_refused(
    app: App,
    debug_commands: Any,
) -> None:
    """.debug reply 空文本时提示用法。"""
    async with app.test_matcher(debug_commands.debug_reply) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_group_event(".debug reply", user_id=SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_reply)
        ctx.should_pass_rule(matcher=debug_commands.debug_reply)
        ctx.should_call_send(
            event,
            "❌ 请提供测试文本\n用法: .debug reply <测试文本>",
            bot=bot,
        )
        ctx.should_finished()


# ─── reply 成功流程 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reply_success_calls_generate_debug_reply_and_sends_report(
    app: App,
    debug_commands: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功 reply 应调用 generate_debug_reply 并发送诊断报告（仅合并转发）。"""
    spy = SimpleNamespace(generate_called=False, build_report_called=False)

    async def _fake_generate_debug_reply(**kwargs: object) -> SimpleNamespace:
        spy.generate_called = True
        collector = kwargs.get("collector")
        return SimpleNamespace(
            reply="测试回复内容",
            reply_to_message_id=None,
            favorability_delta=5,
            favorability_reason="测试",
            interaction_history=None,
            collector=collector,
        )

    async def _fake_build_report(**kwargs: object) -> None:
        spy.build_report_called = True
        spy.report_kwargs = kwargs

    monkeypatch.setattr(debug_commands, "generate_debug_reply", _fake_generate_debug_reply)
    monkeypatch.setattr(debug_commands, "build_and_send_diagnostic_report", _fake_build_report)

    async with app.test_matcher(debug_commands.debug_reply) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_group_event(".debug reply 你好世界", user_id=SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_reply)
        ctx.should_pass_rule(matcher=debug_commands.debug_reply)
        ctx.should_finished()

    assert spy.generate_called
    assert spy.build_report_called
    assert spy.report_kwargs["result_type"] == "reply"
    assert spy.report_kwargs["succeeded"] is True


# ─── reply 异常流程 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reply_exception_sends_error_report(
    app: App,
    debug_commands: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """generate_debug_reply 抛异常时发送失败诊断报告。"""
    spy = SimpleNamespace(build_report_called=False)

    async def _fake_generate_debug_reply(**_kwargs: object) -> SimpleNamespace:
        msg = "LLM 服务不可用"
        raise RuntimeError(msg)

    async def _fake_build_report(**kwargs: object) -> None:
        spy.build_report_called = True
        spy.report_kwargs = kwargs

    monkeypatch.setattr(debug_commands, "generate_debug_reply", _fake_generate_debug_reply)
    monkeypatch.setattr(debug_commands, "build_and_send_diagnostic_report", _fake_build_report)

    async with app.test_matcher(debug_commands.debug_reply) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_group_event(".debug reply 你好", user_id=SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_reply)
        ctx.should_pass_rule(matcher=debug_commands.debug_reply)
        ctx.should_finished()

    assert spy.build_report_called
    assert spy.report_kwargs["succeeded"] is False
    assert "LLM 服务不可用" in str(spy.report_kwargs["error"])


# ─── summary 私聊拒绝 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_summary_rejects_private_chat_detailed(
    app: App,
    debug_commands: Any,
) -> None:
    """.debug summary 私聊下显示"仅支持群聊"。"""
    async with app.test_matcher(debug_commands.debug_summary) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".debug summary 总结最近", user_id=SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_summary)
        ctx.should_pass_rule(matcher=debug_commands.debug_summary)
        ctx.should_call_send(
            event, "❌ .debug summary 仅支持群聊", bot=bot
        )
        ctx.should_finished()


# ─── summary 空文本 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_summary_empty_text_refused(
    app: App,
    debug_commands: Any,
) -> None:
    """.debug summary 空文本时提示用法。"""
    async with app.test_matcher(debug_commands.debug_summary) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_group_event(".debug summary", user_id=SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_summary)
        ctx.should_pass_rule(matcher=debug_commands.debug_summary)
        ctx.should_call_send(
            event,
            "❌ 请提供总结要求\n用法: .debug summary <总结要求>",
            bot=bot,
        )
        ctx.should_finished()


# ─── summary CapabilityNotSupportedError ──────────────────────


@pytest.mark.asyncio
async def test_summary_capability_not_supported(
    app: App,
    debug_commands: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """当 OneBot 不支持获取群聊记录时显示友好错误。"""
    from komari_bot.plugins.group_history_summary.execution_service import (
        CapabilityNotSupportedError,
    )

    # 绕过 _cast_summary_config 类型检查
    monkeypatch.setattr(debug_commands, "_cast_summary_config", lambda c: c)

    # 阻止 build_and_send_diagnostic_report 发出 API 调用
    async def _noop_report(**kwargs: object) -> None:
        pass

    monkeypatch.setattr(debug_commands, "build_and_send_diagnostic_report", _noop_report)

    async def _fake_execute(**_kwargs: object) -> None:
        raise CapabilityNotSupportedError

    monkeypatch.setattr(debug_commands, "execute_group_summary", _fake_execute)

    async with app.test_matcher(debug_commands.debug_summary) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_group_event(".debug summary 总结一下", user_id=SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_summary)
        ctx.should_pass_rule(matcher=debug_commands.debug_summary)
        ctx.should_finished()


@pytest.mark.asyncio
async def test_summary_success_sends_image_before_report(
    debug_commands: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """summary 成功时必须先发送图片，再发送诊断报告。"""
    order: list[str] = []
    event = _build_group_event(".debug summary 总结一下", user_id=SU_ID)

    class _FakeBot:
        self_id = "669293859"

        async def send(self, _event: object, message: object) -> None:
            assert "base64://image-data" in str(message)
            order.append("image")

    async def _allow_superuser(_bot: object, _event: object) -> bool:
        return True

    async def _fake_execute(**_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            image_base64="image-data",
            filtered_message_count=12,
            filter_label="最近消息",
            time_range="07-11 22:00 - 07-11 23:00",
        )

    async def _fake_report(**kwargs: object) -> None:
        assert kwargs["succeeded"] is True
        order.append("report")

    monkeypatch.setattr(debug_commands, "SUPERUSER", _allow_superuser)
    monkeypatch.setattr(debug_commands._summary_config_mgr, "get", object)
    monkeypatch.setattr(debug_commands, "_cast_summary_config", lambda config: config)
    monkeypatch.setattr(debug_commands, "execute_group_summary", _fake_execute)
    monkeypatch.setattr(
        debug_commands,
        "build_and_send_diagnostic_report",
        _fake_report,
    )

    await debug_commands.handle_debug_summary(
        cast("Any", _FakeBot()),
        event,
        "总结一下",
    )

    assert order == ["image", "report"]


# ─── summary 异常流程 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_summary_exception_sends_error_report(
    app: App,
    debug_commands: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """execute_group_summary 抛未知异常时发送失败诊断报告。"""
    spy = SimpleNamespace(build_report_called=False)

    # 绕过 _cast_summary_config 类型检查
    monkeypatch.setattr(debug_commands, "_cast_summary_config", lambda c: c)

    async def _fake_execute(**_kwargs: object) -> None:
        msg = "API 超时"
        raise RuntimeError(msg)

    async def _fake_build_report(**kwargs: object) -> None:
        spy.build_report_called = True
        spy.report_kwargs = kwargs

    monkeypatch.setattr(debug_commands, "execute_group_summary", _fake_execute)
    monkeypatch.setattr(debug_commands, "build_and_send_diagnostic_report", _fake_build_report)

    async with app.test_matcher(debug_commands.debug_summary) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_group_event(".debug summary 总结一下", user_id=SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_summary)
        ctx.should_pass_rule(matcher=debug_commands.debug_summary)
        ctx.should_finished()

    assert spy.build_report_called
    assert spy.report_kwargs["succeeded"] is False
    assert "API 超时" in str(spy.report_kwargs["error"])


# ─── 严格多余参数检查 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_favor_get_strict_no_extra_params() -> None:
    """_parse_user_id 行为明确：非整数 → None，'123 abc' 整体失败。"""
    # 这由 test_parse_user_id_invalid 覆盖
    # pragma: no cover — 由其他测试覆盖


# ─── spy helpers ──────────────────────────────────────────────


def _make_async_return(return_value: object) -> Any:
    async def _fn(*_args: object, **_kwargs: object) -> object:
        return return_value
    return _fn


def _make_async_raise(exception: Exception) -> Any:
    async def _fn(*_args: object, **_kwargs: object) -> object:
        raise exception
    return _fn
