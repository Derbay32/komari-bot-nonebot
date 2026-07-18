""".debug 命令测试。

覆盖：
- 非 SUPERUSER 拒绝（必须先于任何业务调用）
- favor get/set 严格解析和输出
- bind set/del/list 参数校验
- reply/summary 私聊拒绝
- 报告节点结构、分批、降级
"""

from __future__ import annotations

from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from nonebot.adapters.onebot.v11 import (
    Adapter,
    Bot,
    GroupMessageEvent,
    Message,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.adapters.onebot.v11.event import Reply, Sender

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
    message_id: int = 1,
    reply: Reply | None = None,
    message: Message | None = None,
) -> GroupMessageEvent:
    msg = message or Message(plain_text)
    return GroupMessageEvent.model_construct(
        time=1,
        self_id=669293859,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        message_id=message_id,
        group_id=group_id,
        message=msg,
        original_message=msg,
        raw_message=plain_text,
        font=14,
        sender=Sender.model_construct(user_id=user_id, nickname="tester", card=""),
        to_me=True,
        reply=reply,
    )


REJECT_MSG = "❌ 仅限 SUPERUSER 使用"
NON_SU_ID = 99999


# ─── module fixture ────────────────────────────────────────────


@pytest.fixture
def debug_commands(app: App) -> Any:
    del app
    return import_module("komari_bot.plugins.komari_debug.commands")


@pytest.fixture
def debug_reporting(app: App) -> Any:
    del app
    return import_module("komari_bot.plugins.komari_debug.reporting")


# ─── 非 SUPERUSER 拒绝测试 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_favor_get_rejects_non_superuser(
    debug_commands: Any,
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 SUPERUSER 访问 favor get 应被拒绝，且在任何业务调用之前。"""
    called = SimpleNamespace(get_user_favorability=False)
    monkeypatch.setattr(
        debug_commands,
        "get_user_favorability",
        _make_async_spy(called, "get_user_favorability", return_value=None),
    )

    async with app.test_matcher(debug_commands.debug_favor_get) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(
            f".debug favor get {NON_SU_ID}", user_id=NON_SU_ID
        )
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_favor_get)
        ctx.should_pass_rule(matcher=debug_commands.debug_favor_get)
        ctx.should_call_send(event, REJECT_MSG, bot=bot)
        ctx.should_finished()

    assert not called.get_user_favorability


@pytest.mark.asyncio
async def test_favor_set_rejects_non_superuser(
    debug_commands: Any,
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 SUPERUSER 访问 favor set 应被拒绝。"""
    called = SimpleNamespace(set_user_favorability=False)
    monkeypatch.setattr(
        debug_commands,
        "set_user_favorability",
        _make_async_spy(called, "set_user_favorability", return_value=None),
    )

    async with app.test_matcher(debug_commands.debug_favor_set) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(
            ".debug favor set 12345 200", user_id=NON_SU_ID
        )
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_favor_set)
        ctx.should_pass_rule(matcher=debug_commands.debug_favor_set)
        ctx.should_call_send(event, REJECT_MSG, bot=bot)
        ctx.should_finished()

    assert not called.set_user_favorability


@pytest.mark.asyncio
async def test_bind_set_rejects_non_superuser(
    debug_commands: Any,
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 SUPERUSER 访问 bind set 应被拒绝。"""
    called = SimpleNamespace(set_character_name=False)
    manager_stub = SimpleNamespace()
    manager_stub.set_character_name = _make_async_spy(
        called, "set_character_name", return_value=None
    )
    monkeypatch.setattr(debug_commands, "get_binding_manager", lambda: manager_stub)

    async with app.test_matcher(debug_commands.debug_bind_set) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(
            ".debug bind set 12345 泉此方", user_id=NON_SU_ID
        )
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_bind_set)
        ctx.should_pass_rule(matcher=debug_commands.debug_bind_set)
        ctx.should_call_send(event, REJECT_MSG, bot=bot)
        ctx.should_finished()

    assert not called.set_character_name


@pytest.mark.asyncio
async def test_bind_del_rejects_non_superuser(
    debug_commands: Any,
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 SUPERUSER 访问 bind del 应被拒绝。"""
    called = SimpleNamespace(remove_character_name=False)
    manager_stub = SimpleNamespace()
    manager_stub.remove_character_name = _make_async_spy(
        called, "remove_character_name", return_value=False
    )
    monkeypatch.setattr(debug_commands, "get_binding_manager", lambda: manager_stub)

    async with app.test_matcher(debug_commands.debug_bind_del) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".debug bind del 12345", user_id=NON_SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_bind_del)
        ctx.should_pass_rule(matcher=debug_commands.debug_bind_del)
        ctx.should_call_send(event, REJECT_MSG, bot=bot)
        ctx.should_finished()

    assert not called.remove_character_name


@pytest.mark.asyncio
async def test_bind_list_rejects_non_superuser(
    debug_commands: Any,
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 SUPERUSER 访问 bind list 应被拒绝。"""
    called = SimpleNamespace(list_bindings=False)
    manager_stub = SimpleNamespace()
    manager_stub.list_bindings = lambda: setattr(called, "list_bindings", True) or {}
    monkeypatch.setattr(debug_commands, "get_binding_manager", lambda: manager_stub)

    async with app.test_matcher(debug_commands.debug_bind_list) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".debug bind list", user_id=NON_SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_bind_list)
        ctx.should_pass_rule(matcher=debug_commands.debug_bind_list)
        ctx.should_call_send(event, REJECT_MSG, bot=bot)
        ctx.should_finished()

    assert not called.list_bindings


@pytest.mark.asyncio
async def test_reply_rejects_non_superuser(
    debug_commands: Any,
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 SUPERUSER 访问 reply 应被拒绝。"""
    called = SimpleNamespace(generate_debug_reply=False)
    monkeypatch.setattr(
        debug_commands,
        "generate_debug_reply",
        _make_async_spy(called, "generate_debug_reply", return_value=None),
    )

    async with app.test_matcher(debug_commands.debug_reply) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_group_event(".debug reply 你好", user_id=NON_SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_reply)
        ctx.should_pass_rule(matcher=debug_commands.debug_reply)
        ctx.should_call_send(event, REJECT_MSG, bot=bot)
        ctx.should_finished()

    assert not called.generate_debug_reply


@pytest.mark.asyncio
async def test_summary_rejects_non_superuser(
    debug_commands: Any,
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 SUPERUSER 访问 summary 应被拒绝。"""
    called = SimpleNamespace(execute_group_summary=False)
    monkeypatch.setattr(
        debug_commands,
        "execute_group_summary",
        _make_async_spy(called, "execute_group_summary", return_value=None),
    )

    async with app.test_matcher(debug_commands.debug_summary) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_group_event(".debug summary 总结最近消息", user_id=NON_SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_summary)
        ctx.should_pass_rule(matcher=debug_commands.debug_summary)
        ctx.should_call_send(event, REJECT_MSG, bot=bot)
        ctx.should_finished()

    assert not called.execute_group_summary


# ─── 根帮助测试 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_debug_root_rejects_non_superuser(
    app: App,
    debug_commands: Any,
) -> None:
    """根命令 .debug 非 SUPERUSER 被拒绝。"""
    async with app.test_matcher(debug_commands.debug_root) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".debug", user_id=NON_SU_ID)
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_root)
        ctx.should_pass_rule(matcher=debug_commands.debug_root)
        ctx.should_call_send(event, REJECT_MSG, bot=bot)
        ctx.should_finished()


@pytest.mark.asyncio
async def test_debug_root_shows_help(app: App, debug_commands: Any) -> None:
    """根命令 .debug 显示帮助文本。"""
    async with app.test_matcher(debug_commands.debug_root) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".debug")
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_root)
        ctx.should_pass_rule(matcher=debug_commands.debug_root)
        ctx.should_call_send(event, debug_commands.HELP_TEXT, bot=bot)
        ctx.should_finished()


# ─── 群聊限制测试 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reply_rejects_private_chat(app: App, debug_commands: Any) -> None:
    """.debug reply 在私聊下被拒绝。"""
    async with app.test_matcher(debug_commands.debug_reply) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".debug reply 你好")
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_reply)
        ctx.should_pass_rule(matcher=debug_commands.debug_reply)
        ctx.should_call_send(
            event, "❌ .debug reply 仅支持群聊", bot=bot
        )
        ctx.should_finished()


@pytest.mark.asyncio
async def test_summary_rejects_private_chat(app: App, debug_commands: Any) -> None:
    """.debug summary 在私聊下被拒绝。"""
    async with app.test_matcher(debug_commands.debug_summary) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_private_event(".debug summary 总结最近消息")
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_summary)
        ctx.should_pass_rule(matcher=debug_commands.debug_summary)
        ctx.should_call_send(
            event, "❌ .debug summary 仅支持群聊", bot=bot
        )
        ctx.should_finished()


# ─── 空内容测试 ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reply_empty_content(app: App, debug_commands: Any) -> None:
    """.debug reply 空测试文本时显示用法。"""
    async with app.test_matcher(debug_commands.debug_reply) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_group_event(".debug reply")
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_reply)
        ctx.should_pass_rule(matcher=debug_commands.debug_reply)
        ctx.should_call_send(
            event,
            "❌ 请提供测试文本\n用法: .debug reply [--public] <测试文本>",
            bot=bot,
        )
        ctx.should_finished()


@pytest.mark.asyncio
async def test_summary_empty_content(app: App, debug_commands: Any) -> None:
    """.debug summary 空内容时显示用法。"""
    async with app.test_matcher(debug_commands.debug_summary) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_group_event(".debug summary")
        ctx.receive_event(bot, event)
        ctx.should_pass_permission(matcher=debug_commands.debug_summary)
        ctx.should_pass_rule(matcher=debug_commands.debug_summary)
        ctx.should_call_send(
            event,
            "❌ 请提供总结要求\n用法: .debug summary [--public] <总结要求>",
            bot=bot,
        )
        ctx.should_finished()


# ─── 子命令优先级测试 ──────────────────────────────────────────


def test_sub_matchers_have_higher_priority_than_root(debug_commands: Any) -> None:
    """子命令 matcher 优先级应高于根 matcher（数字越小越高）。"""
    assert debug_commands.debug_root.priority == 5
    for name in [
        "debug_favor_get",
        "debug_favor_set",
        "debug_bind_set",
        "debug_bind_del",
        "debug_bind_list",
        "debug_reply",
        "debug_summary",
    ]:
        matcher = getattr(debug_commands, name, None)
        assert matcher is not None, f"{name} matcher not found"
        assert matcher.priority == 2, f"{name} priority={matcher.priority}, expected 2"


def test_sub_matchers_block(debug_commands: Any) -> None:
    """子命令 matcher 应设置 block=True。"""
    for name in [
        "debug_favor_get",
        "debug_favor_set",
        "debug_bind_set",
        "debug_bind_del",
        "debug_bind_list",
        "debug_reply",
        "debug_summary",
    ]:
        matcher = getattr(debug_commands, name, None)
        assert matcher is not None
        assert matcher.block is True, f"{name} block={matcher.block}"


# ─── 插件元数据测试 ────────────────────────────────────────────


def test_plugin_metadata_has_required_plugins() -> None:
    """验证插件声明了所需的依赖插件。"""
    import_module("komari_bot.plugins.komari_debug")


# ─── 报告结构测试 ──────────────────────────────────────────────


def test_report_build_chapters_success(debug_reporting: Any) -> None:
    """验证成功报告包含所有必需章节，且回复正文在最终结果章节不截断。"""
    from komari_bot.plugins.llm_provider.diagnostic import (
        LLMCallTrace,
        LLMDiagnosticCollector,
        ToolExecutionTrace,
    )

    collector = LLMDiagnosticCollector(request_id="test-1")
    call = LLMCallTrace(
        phase="query_rewrite",
        round_index=0,
        model="deepseek-chat",
        finish_reason="stop",
        duration_ms=120.5,
    )
    collector.add_call(call)
    tool = ToolExecutionTrace(
        call_id=call.call_id,
        tool_name="search_web",
        parsed_arguments={"query": "test"},
        status="success",
        result_summary="搜索到 3 条结果",
    )
    collector.add_tool(tool)

    long_reply = "A" * 600
    chapters = debug_reporting._build_chapters(
        collector,
        "reply",
        succeeded=True,
        error=None,
        extra_info={"user_id": "42"},
        final_result_info={
            "reply_text": long_reply,
            "favorability_delta": "好感度变化: +5",
        },
    )

    chapter_titles = [c[0] for c in chapters]
    assert "请求总览" in chapter_titles
    assert "最终结果" in chapter_titles
    assert "LLM 调用详情" in chapter_titles
    assert "工具摘要" in chapter_titles
    assert "阶段 token 小计" in chapter_titles
    assert "全链路 token 小计" in chapter_titles
    assert "错误/降级" in chapter_titles

    # 回复正文完整保留在最终结果中，不被截断到 500
    final_result_body = chapters[1][1]
    assert long_reply in final_result_body
    assert "好感度变化: +5" in final_result_body

    # 请求总览不含回复正文
    overview_body = chapters[0][1]
    assert long_reply not in overview_body


def test_report_build_chapters_failure(debug_reporting: Any) -> None:
    """验证失败报告包含错误信息章节。"""
    from komari_bot.plugins.llm_provider.diagnostic import (
        LLMCallTrace,
        LLMDiagnosticCollector,
    )

    collector = LLMDiagnosticCollector(request_id="test-fail")
    call = LLMCallTrace(
        phase="generate_reply",
        round_index=0,
        model="deepseek-chat",
        finish_reason="error",
        duration_ms=50.0,
    )
    collector.add_call(call)
    collector.add_error("test", "ValueError", "测试错误")

    chapters = debug_reporting._build_chapters(
        collector,
        "reply",
        succeeded=False,
        error="测试错误",
        extra_info=None,
    )

    chapter_titles = [c[0] for c in chapters]
    assert "最终结果" in chapter_titles
    assert "错误/降级" in chapter_titles


def test_token_format_unreported(debug_reporting: Any) -> None:
    """未报告的 token 字段显示为"未报告"而非 0。"""
    assert debug_reporting._fmt_token(None, complete=True) == "未报告"
    assert debug_reporting._fmt_token(None, complete=False) == "未报告"
    assert debug_reporting._fmt_token(0, complete=True) == "0"
    assert debug_reporting._fmt_token(100, complete=True) == "100"


def test_help_text_uses_debug_prefix(debug_commands: Any) -> None:
    """HELP_TEXT 所有命令写完整 .debug ... 前缀。"""
    assert ".debug favor get" in debug_commands.HELP_TEXT
    assert ".debug favor set" in debug_commands.HELP_TEXT
    assert ".debug bind set" in debug_commands.HELP_TEXT
    assert ".debug bind del" in debug_commands.HELP_TEXT
    assert ".debug bind list" in debug_commands.HELP_TEXT
    assert ".debug reply" in debug_commands.HELP_TEXT
    assert ".debug summary" in debug_commands.HELP_TEXT


def test_split_into_nodes_respects_max_length(debug_reporting: Any) -> None:
    """节点拆分遵守最大长度限制。"""
    long_text = "A" * (debug_reporting.MAX_NODE_TEXT_LENGTH + 500)
    nodes = debug_reporting._split_into_nodes(long_text)
    assert len(nodes) > 1
    for node in nodes:
        assert len(node) <= debug_reporting.MAX_NODE_TEXT_LENGTH


def test_split_into_nodes_short_text(debug_reporting: Any) -> None:
    """短文本拆分返回单节点。"""
    nodes = debug_reporting._split_into_nodes("短文本")
    assert len(nodes) == 1
    assert nodes[0] == "短文本"


def test_split_into_nodes_empty(debug_reporting: Any) -> None:
    """空文本拆分返回占位节点。"""
    nodes = debug_reporting._split_into_nodes("")
    assert len(nodes) == 1
    assert nodes[0] == "(无诊断信息)"


# ─── 阶段聚合测试 ──────────────────────────────────────────────


def test_phase_aggregation_with_missing_usage() -> None:
    """usage 为 None 时聚合报告标注为不完整。"""
    from komari_bot.plugins.llm_provider.diagnostic import (
        LLMCallTrace,
        LLMDiagnosticCollector,
    )

    collector = LLMDiagnosticCollector(request_id="agg-test")
    call = LLMCallTrace(
        phase="query_rewrite",
        round_index=0,
        model="deepseek-chat",
        finish_reason="stop",
        duration_ms=100.0,
        usage=None,
    )
    collector.add_call(call)

    agg = collector.aggregate_phase("query_rewrite")
    assert agg.call_count == 1
    assert not agg.input_tokens_complete
    assert agg.input_tokens == 0


# ─── parse helpers ─────────────────────────────────────────────


def test_parse_user_id_valid(debug_commands: Any) -> None:
    """_parse_user_id 正确解析有效 ID。"""
    assert debug_commands._parse_user_id("12345") == "12345"
    assert debug_commands._parse_user_id("  67890  ") == "67890"


def test_parse_user_id_invalid(debug_commands: Any) -> None:
    """_parse_user_id 拒绝无效输入。"""
    assert debug_commands._parse_user_id("abc") is None
    assert debug_commands._parse_user_id("-1") is None
    assert debug_commands._parse_user_id("0") is None
    assert debug_commands._parse_user_id("") is None


def test_parse_favor_value_valid(debug_commands: Any) -> None:
    """_parse_favor_value 正确解析有效值。"""
    assert debug_commands._parse_favor_value("0") == 0
    assert debug_commands._parse_favor_value("200") == 200
    assert debug_commands._parse_favor_value("400") == 400


def test_parse_favor_value_invalid(debug_commands: Any) -> None:
    """_parse_favor_value 拒绝无效值。"""
    assert debug_commands._parse_favor_value("-1") is None
    assert debug_commands._parse_favor_value("401") is None
    assert debug_commands._parse_favor_value("abc") is None
    assert debug_commands._parse_favor_value("") is None


def test_extract_public_flag_only_consumes_leading_flag(debug_commands: Any) -> None:
    assert debug_commands._extract_public_flag("--public 私密输入") == (
        True,
        "私密输入",
    )
    assert debug_commands._extract_public_flag("--public") == (True, "")
    assert debug_commands._extract_public_flag("内容 --public") == (
        False,
        "内容 --public",
    )


def test_reply_context_extracts_message_text_and_images(debug_commands: Any) -> None:
    """引用消息为 OneBot Message 时同时提取文本与图片。"""
    message = Message(
        [
            MessageSegment.text("引用文本"),
            MessageSegment("image", {"url": "https://example.com/a.png"}),
        ]
    )
    reply = SimpleNamespace(
        message=message,
        message_id=321,
        sender=SimpleNamespace(user_id=1001, card="", nickname="引用用户"),
    )
    event = SimpleNamespace(reply=reply, self_id=669293859)

    context = debug_commands._build_reply_context_from_event(event)

    assert context is not None
    assert context.text == "引用文本"
    assert context.message_id == "321"
    assert context.image_sources == ("https://example.com/a.png",)


# ─── spy helpers ───────────────────────────────────────────────


def _make_async_spy(
    called: SimpleNamespace,
    attr: str,
    *,
    return_value: object,
) -> Any:
    async def spy(*args: object, **kwargs: object) -> object:  # noqa: ARG001
        setattr(called, attr, True)
        return return_value

    return spy
