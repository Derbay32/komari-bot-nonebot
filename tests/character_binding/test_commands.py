"""character_binding 命令测试。"""

from __future__ import annotations

import asyncio
from importlib import import_module

import nonebot
import pytest

nonebot.init()
commands = import_module("komari_bot.plugins.character_binding.commands")


class _FinishedError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _StubManager:
    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        self.bindings = dict(bindings or {})

    async def set_character_name(self, user_id: str, character_name: str) -> None:
        self.bindings[user_id] = character_name

    async def remove_character_name(self, user_id: str) -> bool:
        if user_id not in self.bindings:
            return False
        del self.bindings[user_id]
        return True

    def list_bindings(self) -> dict[str, str]:
        return self.bindings.copy()


def _patch_finish(monkeypatch: pytest.MonkeyPatch, matcher: type[object]) -> None:
    async def _fake_finish(message: str) -> None:
        raise _FinishedError(message)

    monkeypatch.setattr(matcher, "finish", _fake_finish)


def test_parse_superuser_bind_set_request_supports_explicit_target() -> None:
    request = commands.parse_superuser_bind_set_request(
        user_id="42",
        arg_text="10086 柊镜",
    )

    assert request.operator_user_id == "42"
    assert request.target_user_id == "10086"
    assert request.character_name == "柊镜"
    assert request.specified_target is True


def test_parse_self_bind_set_request_always_targets_self() -> None:
    request = commands.parse_self_bind_set_request(
        user_id="42",
        arg_text="泉此方",
    )

    assert request.operator_user_id == "42"
    assert request.target_user_id == "42"
    assert request.character_name == "泉此方"
    assert request.specified_target is False


def test_handle_set_superuser_sets_other_user_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _StubManager()
    request = commands.BindSetRequest(
        operator_user_id="42",
        target_user_id="10086",
        character_name="柊镜",
        specified_target=True,
    )
    _patch_finish(monkeypatch, commands.bind_set_superuser)

    with pytest.raises(_FinishedError, match="已为用户 10086 设置角色名为 柊镜") as exc_info:
        asyncio.run(commands.handle_set_superuser(request=request, manager=manager))

    assert exc_info.value.message == "✅ 已为用户 10086 设置角色名为 柊镜"
    assert manager.bindings == {"10086": "柊镜"}


def test_handle_del_superuser_removes_other_user_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _StubManager({"10086": "柊镜"})
    request = commands.BindDeleteRequest(
        operator_user_id="42",
        target_user_id="10086",
        specified_target=True,
    )
    _patch_finish(monkeypatch, commands.bind_del_superuser)

    with pytest.raises(_FinishedError, match="已删除用户 10086 的角色绑定") as exc_info:
        asyncio.run(commands.handle_del_superuser(request=request, manager=manager))

    assert exc_info.value.message == "✅ 已删除用户 10086 的角色绑定"
    assert manager.bindings == {}


def test_handle_list_superuser_returns_all_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _StubManager({"42": "泉此方", "10086": "柊镜"})
    _patch_finish(monkeypatch, commands.bind_list_superuser)

    with pytest.raises(_FinishedError) as exc_info:
        asyncio.run(commands.handle_list_superuser(manager=manager))

    assert exc_info.value.message == "📋 所有角色绑定列表：\n  42: 泉此方\n  10086: 柊镜"


def test_handle_list_only_returns_current_user_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _StubManager({"42": "泉此方", "10086": "柊镜"})
    _patch_finish(monkeypatch, commands.bind_list)

    with pytest.raises(_FinishedError) as exc_info:
        asyncio.run(commands.handle_list(user_id="42", manager=manager))

    assert exc_info.value.message == "📋 您的角色绑定: 泉此方"
