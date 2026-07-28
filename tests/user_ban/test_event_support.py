"""user_ban 用户事件识别与 SUPERUSER 绕过测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from komari_bot.plugins.user_ban import event_support


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def is_user_banned(self, user_id: str, scope: str) -> bool:
        self.calls.append((user_id, scope))
        return True


class _Event:
    def __init__(self, user_id: object = None, *, raises: bool = False) -> None:
        self.user_id = user_id
        self.raises = raises

    def get_user_id(self) -> str:
        if self.raises:
            raise NotImplementedError
        return str(self.user_id)


def _bot() -> Any:
    return SimpleNamespace(config=SimpleNamespace(superusers={"42"}))


@pytest.mark.asyncio
async def test_superuser_bypasses_without_querying_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _Service()
    monkeypatch.setattr(event_support, "get_service", lambda: service)

    result = await event_support.is_event_banned(
        cast("Any", _bot()),
        cast("Any", _Event("42")),
        "command",
    )

    assert result is False
    assert service.calls == []


@pytest.mark.asyncio
async def test_event_without_reliable_qq_bypasses_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _Service()
    monkeypatch.setattr(event_support, "get_service", lambda: service)

    result = await event_support.is_event_banned(
        cast("Any", _bot()),
        cast("Any", _Event(None, raises=True)),
        "command",
    )

    assert result is False
    assert service.calls == []


@pytest.mark.asyncio
async def test_generic_notice_falls_back_to_user_id_attribute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _Service()
    monkeypatch.setattr(event_support, "get_service", lambda: service)

    result = await event_support.is_event_banned(
        cast("Any", _bot()),
        cast("Any", _Event(10086, raises=True)),
        "chat",
    )

    assert result is True
    assert service.calls == [("10086", "chat")]

