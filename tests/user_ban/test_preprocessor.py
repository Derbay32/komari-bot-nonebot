"""user_ban matcher 预处理器测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from komari_bot.plugins.user_ban import preprocessor
from komari_bot.plugins.user_ban.service import BanServiceUnavailableError


def _matcher(plugin_name: str, *, block: bool) -> Any:
    return SimpleNamespace(
        plugin_name=plugin_name,
        block=block,
        remain_handlers=[object(), object()],
    )


@pytest.mark.asyncio
async def test_command_ban_clears_handlers_and_preserves_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def blocked(*_args: object) -> bool:
        return True

    monkeypatch.setattr(preprocessor, "is_event_banned", blocked)
    matcher = _matcher("group_history_summary", block=True)

    await preprocessor.enforce_command_ban(
        cast("Any", matcher),
        cast("Any", object()),
        cast("Any", object()),
    )

    assert matcher.remain_handlers == []
    assert matcher.block is True


@pytest.mark.asyncio
async def test_chat_matcher_is_not_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(*_args: object) -> bool:
        raise AssertionError

    monkeypatch.setattr(preprocessor, "is_event_banned", fail)
    matcher = _matcher("komari_chat", block=False)

    await preprocessor.enforce_command_ban(
        cast("Any", matcher),
        cast("Any", object()),
        cast("Any", object()),
    )

    assert len(matcher.remain_handlers) == 2


@pytest.mark.asyncio
async def test_storage_failure_clears_non_chat_matcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable(*_args: object) -> bool:
        raise BanServiceUnavailableError("离线")

    monkeypatch.setattr(preprocessor, "is_event_banned", unavailable)
    matcher = _matcher("komari_custom", block=False)

    await preprocessor.enforce_command_ban(
        cast("Any", matcher),
        cast("Any", object()),
        cast("Any", object()),
    )

    assert matcher.remain_handlers == []
    assert matcher.block is False
