"""聊天插件入口权限测试。"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import nonebot.plugin
import pytest

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
    from nonebug import App


def _install_allowed_entry_dependencies(
    chat_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    handler: object,
) -> None:
    config = SimpleNamespace(plugin_enable=True, group_whitelist=[])

    class _PermissionPlugin:
        @staticmethod
        async def check_runtime_permission(
            _bot: object,
            _event: object,
            _config: object,
        ) -> tuple[bool, str]:
            return True, ""

    class _BanPlugin:
        class BanServiceUnavailableError(Exception):
            pass

        @staticmethod
        async def is_event_banned(
            _bot: object,
            _event: object,
            _scope: str,
        ) -> bool:
            return False

    monkeypatch.setattr(chat_module, "get_config", lambda: config)
    monkeypatch.setattr(chat_module, "_get_or_build_handler", lambda: handler)
    monkeypatch.setattr(chat_module, "permission_manager_plugin", _PermissionPlugin())
    monkeypatch.setattr(chat_module, "user_ban_plugin", _BanPlugin())


@pytest.fixture
def chat_module(app: App, monkeypatch: pytest.MonkeyPatch) -> Any:
    del app
    original_require = nonebot.plugin.require

    def _require(plugin_name: str) -> object:
        if plugin_name in {"komari_memory", "komari_decision"}:
            return SimpleNamespace(get_plugin_manager=lambda: None)
        return original_require(plugin_name)

    monkeypatch.setattr(nonebot.plugin, "require", _require)
    decision_package_name = "komari_bot.plugins.komari_decision"
    if decision_package_name not in sys.modules:
        decision_package = types.ModuleType(decision_package_name)
        decision_package.__path__ = [  # type: ignore[attr-defined]
            str(
                Path(__file__).resolve().parents[2]
                / "komari_bot"
                / "plugins"
                / "komari_decision"
            )
        ]
        sys.modules[decision_package_name] = decision_package

    module_name = "komari_bot.plugins.komari_chat._entry_under_test"
    module_path = (
        Path(__file__).resolve().parents[2]
        / "komari_bot"
        / "plugins"
        / "komari_chat"
        / "__init__.py"
    )
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        msg = "无法加载聊天插件入口"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_empty_group_whitelist_is_delegated_to_permission_manager(
    chat_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = SimpleNamespace(permission=False, process=False)
    config = SimpleNamespace(plugin_enable=True, group_whitelist=[])

    class _PermissionPlugin:
        @staticmethod
        async def check_runtime_permission(
            _bot: object,
            _event: object,
            actual_config: object,
        ) -> tuple[bool, str]:
            assert actual_config is config
            calls.permission = True
            return True, ""

    class _BanPlugin:
        class BanServiceUnavailableError(Exception):
            pass

        @staticmethod
        async def is_event_banned(
            _bot: object,
            _event: object,
            _scope: str,
        ) -> bool:
            return False

    class _Handler:
        @staticmethod
        async def process_message(
            _bot: object,
            _event: object,
            **_kwargs: object,
        ) -> None:
            calls.process = True

    monkeypatch.setattr(chat_module, "get_config", lambda: config)
    monkeypatch.setattr(chat_module, "_get_or_build_handler", lambda: _Handler())
    monkeypatch.setattr(chat_module, "permission_manager_plugin", _PermissionPlugin())
    monkeypatch.setattr(chat_module, "user_ban_plugin", _BanPlugin())

    await chat_module.handle_group_message(
        cast("Bot", cast("Any", object())),
        cast("GroupMessageEvent", SimpleNamespace(group_id=114514)),
    )

    assert calls.permission
    assert calls.process


@pytest.mark.asyncio
async def test_send_failure_does_not_commit_reply_side_effects(
    chat_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = SimpleNamespace(send=False, commit=False)
    pending_reply = SimpleNamespace(reply="测试回复", reply_to_message_id=None)

    class _Handler:
        @staticmethod
        async def process_message(
            _bot: object,
            _event: object,
            **_kwargs: object,
        ) -> object:
            return pending_reply

        @staticmethod
        async def commit_delivered_reply(_pending_reply: object) -> None:
            calls.commit = True

    async def _fail_send(_message: object) -> None:
        calls.send = True
        msg = "模拟发送失败"
        raise RuntimeError(msg)

    _install_allowed_entry_dependencies(chat_module, monkeypatch, _Handler())
    monkeypatch.setattr(chat_module.matcher, "send", _fail_send)

    await chat_module.handle_group_message(
        cast("Bot", cast("Any", object())),
        cast("GroupMessageEvent", SimpleNamespace(group_id=114514)),
    )

    assert calls.send
    assert not calls.commit


@pytest.mark.asyncio
async def test_successful_send_commits_reply_side_effects_after_delivery(
    chat_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    pending_reply = SimpleNamespace(reply="测试回复", reply_to_message_id=None)

    class _Handler:
        @staticmethod
        async def process_message(
            _bot: object,
            _event: object,
            **_kwargs: object,
        ) -> object:
            return pending_reply

        @staticmethod
        async def commit_delivered_reply(actual_pending_reply: object) -> None:
            assert actual_pending_reply is pending_reply
            order.append("提交")

    async def _send(_message: object) -> None:
        order.append("发送")

    _install_allowed_entry_dependencies(chat_module, monkeypatch, _Handler())
    monkeypatch.setattr(chat_module.matcher, "send", _send)

    await chat_module.handle_group_message(
        cast("Bot", cast("Any", object())),
        cast("GroupMessageEvent", SimpleNamespace(group_id=114514)),
    )

    assert order == ["发送", "提交"]
