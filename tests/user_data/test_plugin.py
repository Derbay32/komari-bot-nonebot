"""user_data 插件入口测试。"""

from __future__ import annotations

import asyncio
import sys
from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

if TYPE_CHECKING:
    from nonebug import App


@pytest.fixture
def user_data_module(app: App) -> Any:
    del app
    module_name = "komari_bot.plugins.user_data.__init__"
    sys.modules.pop(module_name, None)
    module = import_module(module_name)
    module._db = None
    module._db_init_lock = None
    module._db_init_lock_loop = None
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
