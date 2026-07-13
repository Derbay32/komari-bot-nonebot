"""user_data 插件入口测试。"""

from __future__ import annotations

import asyncio
import sys
from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar, cast

import pytest

if TYPE_CHECKING:
    from nonebug import App


@pytest.fixture
def user_data_module(app: App) -> Any:
    del app
    module_name = "komari_bot.plugins.user_data.__init__"
    sys.modules.pop(module_name, None)
    module = import_module(module_name)
    module_any = cast("Any", module)
    module_any._db = None
    module_any._db_init_lock = None
    module_any._db_init_lock_loop = None
    return module


class _FakeUserDataDB:
    instances: ClassVar[list[_FakeUserDataDB]] = []
    initialize_calls: ClassVar[int] = 0
    fail_next_initialize: ClassVar[bool] = False

    def __init__(self, config: object) -> None:
        self.config = config
        self.initialized = False
        self.instances.append(self)

    async def initialize(self) -> None:
        self.__class__.initialize_calls += 1
        await asyncio.sleep(0)
        if self.__class__.fail_next_initialize:
            self.__class__.fail_next_initialize = False
            msg = "初始化失败"
            raise RuntimeError(msg)
        self.initialized = True

    async def set_user_favorability(self, user_id: str, value: int) -> object:
        raise NotImplementedError


def _patch_fake_db(user_data_module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeUserDataDB.instances = []
    _FakeUserDataDB.initialize_calls = 0
    _FakeUserDataDB.fail_next_initialize = False
    monkeypatch.setattr(user_data_module, "UserDataDB", _FakeUserDataDB)
    monkeypatch.setattr(user_data_module, "get_config", lambda: SimpleNamespace())


@pytest.mark.asyncio
async def test_get_db_concurrent_initializes_once(
    user_data_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fake_db(user_data_module, monkeypatch)

    first, second = await asyncio.gather(
        user_data_module.get_db(),
        user_data_module.get_db(),
    )

    assert first is second
    assert first.initialized is True
    assert len(_FakeUserDataDB.instances) == 1
    assert _FakeUserDataDB.initialize_calls == 1


@pytest.mark.asyncio
async def test_get_db_failed_initialize_does_not_cache_db(
    user_data_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fake_db(user_data_module, monkeypatch)
    _FakeUserDataDB.fail_next_initialize = True

    with pytest.raises(RuntimeError, match="初始化失败"):
        await user_data_module.get_db()

    assert user_data_module._db is None

    db = await user_data_module.get_db()

    assert db.initialized is True
    assert len(_FakeUserDataDB.instances) == 2
    assert _FakeUserDataDB.initialize_calls == 2


def test_set_user_favorability_is_exported_in_all(user_data_module: Any) -> None:
    """验证 set_user_favorability 和 FavorabilitySetResult 已加入 __all__。"""
    assert "set_user_favorability" in user_data_module.__all__
    assert "FavorabilitySetResult" in user_data_module.__all__


@pytest.mark.asyncio
async def test_set_user_favorability_calls_db_method(
    user_data_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """导出函数 set_user_favorability 应委托给 UserDataDB.set_user_favorability。"""
    _patch_fake_db(user_data_module, monkeypatch)

    call_record: dict[str, object] = {}

    async def fake_set(_self: object, user_id: str, value: int) -> object:
        call_record["user_id"] = user_id
        call_record["value"] = value
        from komari_bot.plugins.user_data.models import FavorabilitySetResult
        return FavorabilitySetResult.from_values(
            user_id=user_id,
            before=0,
            after=value,
            updated_at="2026-07-11T23:00:00+08:00",
        )

    monkeypatch.setattr(
        user_data_module.UserDataDB,
        "set_user_favorability",
        fake_set,
    )

    result = await user_data_module.set_user_favorability("42", 300)

    assert call_record == {"user_id": "42", "value": 300}
    assert result.user_id == "42"
    assert result.after == 300
    assert result.stage_index == 4
