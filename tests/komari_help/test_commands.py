"""Komari Help 命令展示逻辑测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from nonebot.adapters.onebot.v11 import Adapter, Bot, GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.event import Sender

from komari_bot.common.onebot_messages import plain_text_message
from komari_bot.plugins.komari_help import rendering as rendering_module
from komari_bot.plugins.komari_help.models import HelpEntry, HelpSearchResult

if TYPE_CHECKING:
    from nonebug import App


class _StubConfigManager:
    def __init__(self, config: object) -> None:
        self.config = config

    async def get_async(self) -> object:
        return self.config


class _StubPermissionManager:
    def __init__(self, result: tuple[bool, str]) -> None:
        self.result = result
        self.seen_configs: list[object] = []

    async def check_runtime_permission(
        self,
        _bot: object,
        _event: object,
        config: object,
    ) -> tuple[bool, str]:
        self.seen_configs.append(config)
        return self.result


def _create_onebot_bot(ctx: Any) -> Bot:
    adapter = ctx.create_adapter(base=Adapter)
    return cast("Bot", ctx.create_bot(base=Bot, adapter=adapter, self_id="669293859"))


def _build_group_event(message_text: str) -> GroupMessageEvent:
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


@pytest.fixture
def commands_module(app: App) -> Any:
    del app
    return import_module("komari_bot.plugins.komari_help.commands")


def _build_config(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "show_category_emoji": True,
        "default_result_limit": 5,
        "max_reply_result_count": 2,
        "max_content_preview_length": 100,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _build_result(
    title: str, content: str, plugin_name: str = "sr"
) -> HelpSearchResult:
    return HelpSearchResult(
        id=1,
        category="command",
        plugin_name=plugin_name,
        title=title,
        content=content,
        similarity=0.95,
        source="keyword",
    )


def _build_entry(title: str, plugin_name: str = "sr") -> HelpEntry:
    timestamp = datetime(2026, 4, 22, 22, 30, tzinfo=UTC)
    return HelpEntry(
        id=1,
        category="command",
        plugin_name=plugin_name,
        keywords=["帮助"],
        title=title,
        content="示例内容",
        notes=None,
        is_auto_generated=False,
        created_at=timestamp,
        updated_at=timestamp,
    )


def test_format_results_preserves_multiline_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rendering_module,
        "get_config",
        lambda: _build_config(max_reply_result_count=3),
    )

    rendered = rendering_module.format_results(
        [
            _build_result(
                "sr",
                "核心指令：\n.sr\n.sr 随机从神人榜内抽取一个\n.sr add 向神人榜内添加神人",
            )
        ]
    )

    assert "⌨️ sr" in rendered
    assert "⌨️ sr (sr)" not in rendered
    assert "  核心指令：" in rendered
    assert "  .sr" in rendered
    assert "  .sr 随机从神人榜内抽取一个" in rendered
    assert "  .sr add 向神人榜内添加神人" in rendered
    assert "核心指令： .sr" not in rendered


def test_format_results_limits_reply_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rendering_module, "get_config", lambda: _build_config())

    rendered = rendering_module.format_results(
        [
            _build_result("指令 1", "内容 1", "plugin_1"),
            _build_result("指令 2", "内容 2", "plugin_2"),
            _build_result("指令 3", "内容 3", "plugin_3"),
        ]
    )

    assert "指令 1" in rendered
    assert "指令 2" in rendered
    assert "(plugin_1)" not in rendered
    assert "(plugin_2)" not in rendered
    assert "指令 3" not in rendered
    assert "……其余 1 条结果已省略" in rendered


def test_get_search_result_limit_uses_reply_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rendering_module,
        "get_config",
        lambda: _build_config(default_result_limit=5, max_reply_result_count=2),
    )

    assert rendering_module.get_search_result_limit() == 2


def test_format_list_page_shows_page_navigation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rendering_module, "get_config", lambda: _build_config())

    rendered = rendering_module.format_list_page(
        [_build_entry("指令 1", "plugin_1")],
        21,
        2,
    )

    assert "📚 当前帮助条目共 21 条（第 2/3 页）" in rendered
    assert "⌨️ 指令 1" in rendered
    assert "(plugin_1)" not in rendered
    assert "查看下一页请使用 .docs list 3" in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("matcher_name", "message_text"),
    [
        ("help_cmd", ".docs 神人榜"),
        ("help_list_cmd", ".docs list"),
    ],
)
async def test_docs_user_entries_apply_runtime_permission_before_engine_access(
    app: App,
    commands_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    matcher_name: str,
    message_text: str,
) -> None:
    config = SimpleNamespace(
        plugin_enable=False,
        user_whitelist=[],
        group_whitelist=[],
    )
    permission_manager = _StubPermissionManager((False, "插件当前已禁用"))

    def fail_get_engine() -> None:
        msg = "权限拒绝后不应访问帮助引擎"
        raise AssertionError(msg)

    monkeypatch.setattr(
        commands_module,
        "config_manager",
        _StubConfigManager(config),
    )
    monkeypatch.setattr(
        commands_module,
        "permission_manager_plugin",
        permission_manager,
    )
    monkeypatch.setattr(commands_module, "get_engine", fail_get_engine)

    matcher = getattr(commands_module, matcher_name)
    async with app.test_matcher(matcher) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_group_event(message_text)
        ctx.receive_event(bot, event)
        ctx.should_ignore_permission(matcher=matcher)
        ctx.should_pass_rule(matcher=matcher)
        ctx.should_call_send(
            event,
            plain_text_message("❌ 插件当前已禁用"),
            bot=bot,
        )
        ctx.should_finished()

    assert permission_manager.seen_configs == [config]


@pytest.mark.asyncio
async def test_docs_refresh_checks_superuser_at_runtime_before_reading_config(
    app: App,
    commands_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailConfigManager:
        @staticmethod
        async def get_async() -> object:
            msg = "非超级用户不应读取刷新配置"
            raise AssertionError(msg)

    async def reject_superuser(_bot: object, _event: object) -> bool:
        return False

    monkeypatch.setattr(commands_module, "config_manager", _FailConfigManager())
    monkeypatch.setattr(commands_module, "SUPERUSER", reject_superuser)

    matcher = commands_module.help_refresh_cmd
    async with app.test_matcher(matcher) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_group_event(".docs refresh")
        ctx.receive_event(bot, event)
        ctx.should_ignore_permission(matcher=matcher)
        ctx.should_pass_rule(matcher=matcher)
        ctx.should_call_send(event, "❌ 仅限 SUPERUSER 使用", bot=bot)
        ctx.should_finished()


@pytest.mark.asyncio
async def test_docs_refresh_applies_runtime_plugin_status_after_superuser_check(
    app: App,
    commands_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = SimpleNamespace(
        plugin_enable=False,
        user_whitelist=[],
        group_whitelist=[],
    )
    permission_manager = _StubPermissionManager((False, "插件当前已禁用"))

    async def allow_superuser(_bot: object, _event: object) -> bool:
        return True

    monkeypatch.setattr(
        commands_module,
        "config_manager",
        _StubConfigManager(config),
    )
    monkeypatch.setattr(
        commands_module,
        "permission_manager_plugin",
        permission_manager,
    )
    monkeypatch.setattr(commands_module, "SUPERUSER", allow_superuser)

    matcher = commands_module.help_refresh_cmd
    async with app.test_matcher(matcher) as ctx:
        bot = _create_onebot_bot(ctx)
        event = _build_group_event(".docs refresh")
        ctx.receive_event(bot, event)
        ctx.should_ignore_permission(matcher=matcher)
        ctx.should_pass_rule(matcher=matcher)
        ctx.should_call_send(
            event,
            plain_text_message("❌ 插件当前已禁用"),
            bot=bot,
        )
        ctx.should_finished()

    assert permission_manager.seen_configs == [config]
