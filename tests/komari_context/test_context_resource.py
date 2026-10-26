"""TSK-192 phase A / TSK-197: komari_context owns the shared Redis resource.

These cases exercise the registered plugin lifecycle (not a test-only factory)
and the neutral store's ownership boundary.  Behavioral append/trim coverage
against a real Redis lives in ``test_context_redis_real.py``.
"""

from __future__ import annotations

import importlib.util
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar, cast

import pytest

from komari_bot.context.redis_keys import RedisKeys
from komari_bot.context.redis_store import MessageSchema, RecentContextStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = PROJECT_ROOT / "komari_bot" / "plugins" / "komari_context"
PACKAGE = "komari_bot.plugins.komari_context"


class _LifecycleRedis:
    """Minimal supplied-client double for the plugin lifecycle."""

    instances: ClassVar[list[_LifecycleRedis]] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.ping_error: BaseException | None = None
        self.pinged = False
        self.closed = 0
        self.__class__.instances.append(self)

    async def ping(self) -> bool:
        self.pinged = True
        if self.ping_error is not None:
            raise self.ping_error
        return True

    async def aclose(self) -> None:
        self.closed += 1


class _ConfigManager:
    def __init__(self, redis_db: int = 6) -> None:
        self.redis_db = redis_db
        self.initialize_calls = 0

    async def get_async(self) -> Any:
        return SimpleNamespace(redis_db=self.redis_db)

    def get(self) -> Any:
        return SimpleNamespace(redis_db=self.redis_db)

    async def initialize_async(self) -> Any:
        self.initialize_calls += 1
        return await self.get_async()


@asynccontextmanager
async def _loaded_plugin(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """Execute the real plugin entry and expose the hooks it registers."""
    from nonebot import get_driver

    lifespan = get_driver()._lifespan
    startup_before = list(lifespan._startup_funcs)
    shutdown_before = list(lifespan._shutdown_funcs)
    saved_package = sys.modules.get(PACKAGE)
    saved_schema = sys.modules.get(f"{PACKAGE}.config_schema")
    _LifecycleRedis.instances.clear()

    config_manager = _ConfigManager()

    def _config_manager_getter(
        name: str,
        schema: object,
        *,
        env_config_schema: object | None = None,
    ) -> _ConfigManager:
        del schema, env_config_schema
        assert name == "komari_context"
        return config_manager

    from komari_bot.plugins import config_manager as config_manager_package

    monkeypatch.setattr(
        config_manager_package,
        "get_config_manager",
        _config_manager_getter,
        raising=False,
    )
    spec = importlib.util.spec_from_file_location(
        PACKAGE,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    try:
        spec.loader.exec_module(module)
        startup_hooks = [
            hook for hook in lifespan._startup_funcs if hook not in startup_before
        ]
        shutdown_hooks = [
            hook for hook in lifespan._shutdown_funcs if hook not in shutdown_before
        ]
        assert len(startup_hooks) == 1
        assert len(shutdown_hooks) == 1
        yield SimpleNamespace(
            module=module,
            startup=startup_hooks[0],
            shutdown=shutdown_hooks[0],
            config_manager=config_manager,
        )
    finally:
        lifespan._startup_funcs[:] = startup_before
        lifespan._shutdown_funcs[:] = shutdown_before
        if saved_package is None:
            sys.modules.pop(PACKAGE, None)
        else:
            sys.modules[PACKAGE] = saved_package
        if saved_schema is None:
            sys.modules.pop(f"{PACKAGE}.config_schema", None)
        else:
            sys.modules[f"{PACKAGE}.config_schema"] = saved_schema


def test_context_key_strings_are_preserved() -> None:
    """Active buffer and fulfillment key identity must not change."""
    assert RedisKeys.PREFIX == "komari_memory"
    assert RedisKeys.buffer("g1") == "komari_memory:buffer:g1"
    assert (
        RedisKeys.chat_commit_step("op-1", "ai_history")
        == "komari_memory:chat_commit:op-1:ai_history"
    )
    # Retired legacy interaction namespaces and dead session metadata are gone.
    for name in (
        "GLOBAL_INTERACTION",
        "GLOBAL_INTERACTION_PATTERN",
        "GLOBAL_INTERACTION_PENDING",
        "LAST_MESSAGE",
        "SESSION_START",
    ):
        assert not hasattr(RedisKeys, name), name
    for name in ("global_interaction", "last_message", "session_start"):
        assert not hasattr(RedisKeys, name), name


def test_recent_context_store_uses_only_the_supplied_client() -> None:
    supplied = SimpleNamespace()
    store = RecentContextStore(cast("Any", supplied))

    assert store.redis is supplied
    assert not hasattr(store, "initialize")
    assert not hasattr(store, "close")
    # Retired legacy surfaces are gone.
    for method in (
        "push_global_interaction",
        "push_global_interaction_once",
        "get_global_interaction_buffer",
        "get_last_message_time",
        "get_session_start_time",
        "get_active_groups",
        "delete_buffer",
        "get_context_around_message",
    ):
        assert not hasattr(store, method), method


@pytest.mark.asyncio
async def test_registered_lifecycle_exposes_store_idempotently_and_closes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _loaded_plugin(monkeypatch) as ctx:
        module = ctx.module
        monkeypatch.setattr(
            module,
            "get_shared_redis_config",
            lambda: SimpleNamespace(
                redis_host="redis.internal",
                redis_port=6380,
                redis_password="secret",
            ),
        )
        client = _LifecycleRedis()
        _LifecycleRedis.instances.clear()

        def _factory(**kwargs: object) -> _LifecycleRedis:
            client.kwargs = kwargs
            _LifecycleRedis.instances.append(client)
            return client

        monkeypatch.setattr(module.aioredis, "Redis", _factory)

        assert module.get_context_store() is None
        await ctx.startup()
        assert module.get_context_config().redis_db == 6
        store = module.get_context_store()
        assert store is not None
        assert store.redis is client
        assert client.pinged is True
        assert client.kwargs["db"] == 6
        assert client.kwargs["decode_responses"] is True

        await ctx.startup()
        assert len(_LifecycleRedis.instances) == 1
        assert module.get_context_store() is store

        await ctx.shutdown()
        await ctx.shutdown()
        assert client.closed == 1
        assert module.get_context_store() is None


@pytest.mark.asyncio
async def test_failed_context_startup_cleans_only_its_new_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _loaded_plugin(monkeypatch) as ctx:
        module = ctx.module
        monkeypatch.setattr(
            module,
            "get_shared_redis_config",
            lambda: SimpleNamespace(
                redis_host="redis.internal",
                redis_port=6380,
                redis_password="",
            ),
        )
        failing = _LifecycleRedis()
        failing.ping_error = ConnectionError("ping failed")
        _LifecycleRedis.instances.clear()
        monkeypatch.setattr(module.aioredis, "Redis", lambda **_kwargs: failing)

        await ctx.startup()

        assert failing.pinged is True
        assert failing.closed == 1
        assert module.get_context_store() is None


def test_message_schema_shape_is_unchanged() -> None:
    message = MessageSchema(
        user_id="u1",
        user_nickname="用户",
        group_id="g1",
        content="正文",
        timestamp=1.5,
        message_id="m1",
        is_bot=True,
    )
    assert message.is_bot is True
