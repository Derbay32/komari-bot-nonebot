"""权限管理器白名单组合语义测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from komari_bot.plugins.permission_manager import manager as manager_module
from komari_bot.plugins.permission_manager.checker import PermissionChecker
from komari_bot.plugins.permission_manager.manager import (
    PermissionManager,
    create_whitelist_rule,
)

if TYPE_CHECKING:
    from nonebot.adapters import Bot
    from nonebot.adapters.onebot.v11 import MessageEvent


def _build_config(
    *,
    user_whitelist: list[str],
    group_whitelist: list[str],
) -> object:
    return SimpleNamespace(
        plugin_enable=True,
        user_whitelist=user_whitelist,
        group_whitelist=group_whitelist,
    )


def _build_event(*, user_id: str, group_id: str | None) -> MessageEvent:
    event = SimpleNamespace(get_user_id=lambda: user_id)
    if group_id is not None:
        event.group_id = group_id
    return cast("MessageEvent", event)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_whitelist", "group_whitelist", "user_id", "group_id", "expected"),
    [
        ([], [], "1001", "2001", "允许"),
        (["1001"], [], "1001", "2001", "允许"),
        (["1001"], [], "1002", "2001", "拒绝"),
        ([], ["2001"], "1001", "2001", "允许"),
        ([], ["2001"], "1001", "2002", "拒绝"),
        (["1001"], ["2001"], "1001", "2001", "允许"),
        (["1001"], ["2001"], "1002", "2001", "拒绝"),
        (["1001"], ["2001"], "1001", "2002", "拒绝"),
        ([], ["2001"], "1001", None, "允许"),
        (["1001"], ["2001"], "1002", None, "拒绝"),
    ],
)
async def test_can_use_command_applies_every_configured_whitelist(
    monkeypatch: pytest.MonkeyPatch,
    user_whitelist: list[str],
    group_whitelist: list[str],
    user_id: str,
    group_id: str | None,
    expected: str,
) -> None:
    async def _reject_superuser(_bot: object, _event: object) -> bool:
        return False

    monkeypatch.setattr(manager_module, "SUPERUSER", _reject_superuser)
    permission_manager = PermissionManager(
        _build_config(
            user_whitelist=user_whitelist,
            group_whitelist=group_whitelist,
        )
    )

    allowed, _reason = await permission_manager.can_use_command(
        cast("Bot", cast("Any", object())),
        _build_event(user_id=user_id, group_id=group_id),
    )

    assert allowed is (expected == "允许")


def test_can_use_context_reuses_switch_whitelist_and_superuser_semantics() -> None:
    permission_manager = PermissionManager(
        _build_config(
            user_whitelist=["1001"],
            group_whitelist=["2001"],
        )
    )

    assert permission_manager.can_use_context(
        user_id="1001",
        group_id="2001",
    ) == (True, "")
    assert permission_manager.can_use_context(
        user_id="1002",
        group_id="2001",
    )[0] is False
    assert permission_manager.can_use_context(
        user_id="1002",
        group_id="2002",
        is_superuser=True,
    ) == (True, "")


def test_static_permission_apis_emit_deprecation_warning() -> None:
    config = _build_config(user_whitelist=[], group_whitelist=[])

    with pytest.warns(DeprecationWarning, match="捕获静态配置"):
        PermissionChecker(config)
    with pytest.warns(DeprecationWarning, match="捕获静态配置"):
        create_whitelist_rule(config)
