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
    module_any._lifecycle_state = "new"
    return module


class _FakeUserDataDB:
    instances: ClassVar[list[_FakeUserDataDB]] = []
    initialize_calls: ClassVar[int] = 0
    close_calls: ClassVar[int] = 0
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

    async def close(self) -> None:
        self.__class__.close_calls += 1

    async def set_user_favorability(self, user_id: str, value: int) -> object:
        raise NotImplementedError


def _patch_fake_db(user_data_module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeUserDataDB.instances = []
    _FakeUserDataDB.initialize_calls = 0
    _FakeUserDataDB.close_calls = 0
    _FakeUserDataDB.fail_next_initialize = False
    monkeypatch.setattr(user_data_module, "UserDataDB", _FakeUserDataDB)

    async def _get_config_async() -> SimpleNamespace:
        return SimpleNamespace(plugin_enable=True)

    monkeypatch.setattr(
        user_data_module,
        "_get_config_async",
        _get_config_async,
    )


class _FakeDriver:
    def __init__(self) -> None:
        self.startup_callbacks: list[object] = []
        self.shutdown_callbacks: list[object] = []

    def on_startup(self, callback: object) -> object:
        self.startup_callbacks.append(callback)
        return callback

    def on_shutdown(self, callback: object) -> object:
        self.shutdown_callbacks.append(callback)
        return callback


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


@pytest.mark.asyncio
async def test_get_db_rejects_disabled_plugin_before_lazy_initialization(
    user_data_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fake_db(user_data_module, monkeypatch)

    async def _get_disabled_config() -> SimpleNamespace:
        return SimpleNamespace(plugin_enable=False)

    monkeypatch.setattr(
        user_data_module,
        "_get_config_async",
        _get_disabled_config,
    )

    with pytest.raises(user_data_module.UserDataDisabledError, match="已禁用"):
        await user_data_module.get_db()

    assert _FakeUserDataDB.instances == []
    assert user_data_module._db is None


@pytest.mark.asyncio
async def test_get_db_does_not_return_cached_db_after_dynamic_disable(
    user_data_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fake_db(user_data_module, monkeypatch)
    cached = await user_data_module.get_db()

    async def _get_disabled_config() -> SimpleNamespace:
        return SimpleNamespace(plugin_enable=False)

    monkeypatch.setattr(
        user_data_module,
        "_get_config_async",
        _get_disabled_config,
    )

    with pytest.raises(user_data_module.UserDataDisabledError, match="已禁用"):
        await user_data_module.get_db()

    assert user_data_module._db is cached


def test_lifecycle_is_registered_with_nonebot_driver(user_data_module: Any) -> None:
    driver = _FakeDriver()

    user_data_module._register_lifecycle(driver)

    assert driver.startup_callbacks == [user_data_module.on_startup]
    assert driver.shutdown_callbacks == [user_data_module.on_shutdown]
    assert not hasattr(user_data_module, "__plugin_startup__")
    assert not hasattr(user_data_module, "__plugin_shutdown__")


@pytest.mark.asyncio
async def test_shutdown_closes_and_clears_cached_db(
    user_data_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_fake_db(user_data_module, monkeypatch)
    db = await user_data_module.get_db()

    await user_data_module.on_shutdown()

    assert db.close_calls == 1
    assert user_data_module._db is None
    assert user_data_module._lifecycle_state == "stopped"

    with pytest.raises(user_data_module.UserDataStoppingError, match="已经关闭"):
        await user_data_module.get_db()

    assert len(_FakeUserDataDB.instances) == 1


@pytest.mark.asyncio
async def test_shutdown_during_lazy_initialize_never_publishes_new_pool(
    user_data_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class _BlockingDB:
        instances: ClassVar[list[_BlockingDB]] = []

        def __init__(self, _config: object) -> None:
            self.close_calls = 0
            self.instances.append(self)

        async def initialize(self) -> None:
            started.set()
            await release.wait()

        async def close(self) -> None:
            self.close_calls += 1

    monkeypatch.setattr(user_data_module, "UserDataDB", _BlockingDB)

    async def _get_config_async() -> SimpleNamespace:
        return SimpleNamespace(plugin_enable=True)

    monkeypatch.setattr(
        user_data_module,
        "_get_config_async",
        _get_config_async,
    )

    get_task = asyncio.create_task(user_data_module.get_db())
    await started.wait()
    shutdown_task = asyncio.create_task(user_data_module.on_shutdown())
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(user_data_module.UserDataStoppingError, match="关闭状态"):
        await get_task
    await shutdown_task

    assert user_data_module._db is None
    assert user_data_module._lifecycle_state == "stopped"
    assert len(_BlockingDB.instances) == 1
    assert _BlockingDB.instances[0].close_calls == 1


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
