"""TSK-192 phase A: komari_context owns the shared recent-context Redis client.

These are the RED acceptance tests for the new independent resource slice.  They
exercise the registered plugin lifecycle (not a test-only factory) and the
neutral store through its public seam.
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
    """Execute the real plugin entry and expose the hooks it registers.

    The suite shim for this package is temporarily replaced, so the production
    module body (require + driver hook registration + config manager creation)
    really runs.  All external boundaries are doubled below.
    """
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

    import komari_bot.plugins.config_manager as config_manager_package

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


def test_context_key_strings_are_preserved_and_memory_keys_extend_them() -> None:
    """Active fulfillment key identity must not change with the move."""
    from komari_bot.plugins.komari_memory.services.redis_keys import (
        RedisKeys as MemoryRedisKeys,
    )

    assert RedisKeys.PREFIX == "komari_memory"
    assert RedisKeys.buffer("g1") == "komari_memory:buffer:g1"
    assert RedisKeys.last_message("g1") == "komari_memory:last_message:g1"
    assert RedisKeys.session_start("g1") == "komari_memory:session_start:g1"
    assert (
        RedisKeys.chat_commit_step("op-1", "ai_history")
        == "komari_memory:chat_commit:op-1:ai_history"
    )
    assert RedisKeys.global_interaction("u1") == "komari_memory:global_interaction:u1"
    assert issubclass(MemoryRedisKeys, RedisKeys)
    assert (
        MemoryRedisKeys.buffer_processing("g1", "snapshot")
        == "komari_memory:buffer:processing:g1:snapshot"
    )


def test_recent_context_store_uses_only_the_supplied_client() -> None:
    from tests.komari_memory.test_redis_manager import _FakeRedis

    fake = _FakeRedis()
    store = RecentContextStore(cast("Any", fake))

    assert store.redis is fake
    assert not hasattr(store, "initialize")
    assert not hasattr(store, "close")


def test_recent_context_store_preserves_once_only_evidence_and_reads() -> None:
    from tests.komari_memory.test_redis_manager import _FakeRedis

    fake = _FakeRedis()
    store = RecentContextStore(cast("Any", fake))
    first = MessageSchema(
        user_id="bot",
        user_nickname="小鞠",
        group_id="g1",
        content="只写一次",
        timestamp=1.5,
        message_id="m1",
        is_bot=True,
    )

    assert _run(store.push_message_once("g1", first, operation_id="op-1")) is True
    assert _run(store.push_message_once("g1", first, operation_id="op-1")) is False
    assert _run(store.push_global_interaction_once(
        user_id="u1",
        record={"event": "e", "result": "r", "emotion": "m"},
        trigger_size=1,
        operation_id="op-1",
    )) is True
    assert _run(store.push_global_interaction_once(
        user_id="u1",
        record={"event": "e", "result": "r", "emotion": "m"},
        trigger_size=1,
        operation_id="op-1",
    )) is False

    messages = _run(store.get_buffer("g1"))
    assert [message.message_id for message in messages] == ["m1"]
    assert _run(store.get_context_around_message("g1", "m1"))[0].content == "只写一次"
    assert _run(store.get_last_message_time("g1")) is not None
    assert _run(store.get_session_start_time("g1")) is not None
    assert _run(store.get_active_groups()) == ["g1"]
    assert _run(store.delete_chat_commit_evidence("op-1")) == 2
    assert _run(store.delete_chat_commit_evidence("op-1")) == 0


def _run(coroutine: Any) -> Any:
    import asyncio

    return asyncio.run(coroutine)


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
