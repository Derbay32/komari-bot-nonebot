"""TSK-279 Stage-C1 RED: real production lifecycle assembly.

C1 pins the **real** assembly: the roulette package registers exactly one
driver startup/shutdown hook, the startup hook builds the real application
(config -> storage recovery -> QQ install -> scheduler jobs) and the shutdown
hook tears it down in order.  These cases verify the *actual* registered hooks,
the *actual* installed QQ runtime and the *real* registered ``scheduler.func``
objects - never a hand-written fake runtime standing in for the assembly.

Production module ``komari_bot.plugins.komari_roulette.lifecycle`` does not
exist yet, so the C1 cases fail with ``ModuleNotFoundError`` / ``AttributeError``
(the expected "missing seam" RED bucket), never with a whole-file collection
error.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .command_support import PG_REQUIRED
from .tsk279_lifecycle_support import (
    APPLICATION_FUNCTIONS,
    LIFECYCLE_MODULE,
    QQ_MODULE,
    application_api,
    delete_roulette_config,
    invoke_hook,
    lifecycle_context,
    require_single_shutdown_hook,
    require_single_startup_hook,
)
from .tsk279_support import harness_fixture_body

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from .tsk279_support import Tsk279Harness

pytestmark = [pytest.mark.asyncio]


class TogglableConfig:
    """Config port double mirroring the real ``ConfigManager`` read API.

    ``fail`` is live: the startup snapshot and the per-call re-reads can be
    forced to fail after the runtime already reached READY, which is how the
    "recovery job really drives the runtime state machine" case is observed.
    """

    def __init__(self, *, plugin_enable: bool = True) -> None:
        self.plugin_enable = plugin_enable
        self.fail = False
        self.initialize_calls = 0
        self.get_calls = 0
        self.updates: list[tuple[str, object]] = []

    def _snapshot(self) -> Any:
        if self.fail:
            message = "config storage unavailable (C1 test port)"
            raise RuntimeError(message)
        return SimpleNamespace(plugin_enable=self.plugin_enable)

    async def initialize_async(self, *args: object, **kwargs: object) -> Any:
        del args, kwargs
        self.initialize_calls += 1
        return self._snapshot()

    def get(self) -> Any:
        self.get_calls += 1
        return self._snapshot()

    async def get_async(self) -> Any:
        self.get_calls += 1
        return self._snapshot()

    async def update_field_async(self, field: str, **kwargs: object) -> Any:
        value = kwargs.get("value")
        self.updates.append((field, value))
        setattr(self, field, value)
        return self._snapshot()


@pytest.fixture
async def harness() -> AsyncIterator[Tsk279Harness]:
    async for current in harness_fixture_body():
        yield current


# ---------------------------------------------------------------------------
# Seam exposure + real driver hook registration
# ---------------------------------------------------------------------------


def test_lifecycle_seam_exposes_application_functions() -> None:
    api = application_api()
    for name in APPLICATION_FUNCTIONS:
        assert callable(api[name]), f"{name} must be callable"


async def test_package_import_registers_single_driver_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The package import registers exactly one startup + one shutdown hook."""

    async with lifecycle_context(monkeypatch) as ctx:
        startup = require_single_startup_hook(ctx)
        shutdown = require_single_shutdown_hook(ctx)
        assert startup.__module__.startswith(
            "komari_bot.plugins.komari_roulette"
        ), f"startup hook must belong to the roulette package: {startup}"
        assert shutdown.__module__.startswith(
            "komari_bot.plugins.komari_roulette"
        ), f"shutdown hook must belong to the roulette package: {shutdown}"
        assert ctx.config_manager_calls == []


def test_lifecycle_module_path_is_frozen() -> None:
    assert LIFECYCLE_MODULE == "komari_bot.plugins.komari_roulette.lifecycle"
    assert QQ_MODULE == "komari_bot.plugins.komari_roulette.qq"


# ---------------------------------------------------------------------------
# Start: config acquisition, QQ install, owned jobs
# ---------------------------------------------------------------------------


@PG_REQUIRED
async def test_start_installs_qq_runtime_and_registers_owned_jobs_once(
    harness: Tsk279Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from komari_bot.plugins.komari_roulette.config_schema import DynamicConfigSchema
    from komari_bot.plugins.komari_roulette.maintenance import (
        CLEANUP_JOB_ID,
        RECOVERY_JOB_ID,
    )

    await delete_roulette_config(harness.engine)
    try:
        async with lifecycle_context(monkeypatch) as ctx:
            startup = require_single_startup_hook(ctx)
            await invoke_hook(startup)

            api = application_api()
            app = api["get_roulette_application"]()
            assert app is not None, "startup hook must install the application"
            assert app.runtime is not None
            assert ctx.config_manager_calls == [
                ("komari_roulette", DynamicConfigSchema)
            ]

            qq = __import__(QQ_MODULE, fromlist=["get_roulette_qq_runtime"])
            assert qq.get_roulette_qq_runtime() is not None, (
                "startup must install the QQ runtime"
            )

            job_ids = {
                job["kwargs"].get("id") for job in ctx.scheduler.jobs
            }
            assert job_ids == {RECOVERY_JOB_ID, CLEANUP_JOB_ID}, (
                f"startup must register exactly the owned jobs, got {job_ids}"
            )

            # Repeated startup is single-flight: no duplicate jobs / re-acquire.
            await invoke_hook(startup)
            assert len(ctx.scheduler.add_job_calls) == 2
            assert ctx.config_manager_calls == [
                ("komari_roulette", DynamicConfigSchema)
            ]
    finally:
        await delete_roulette_config(harness.engine)


@PG_REQUIRED
async def test_recovery_job_func_reaches_runtime_state_machine(
    harness: Tsk279Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registered recovery ``func`` must flow through the runtime.

    After the runtime records a failure, invoking the *registered* job callback
    must clear it only by running the runtime's own recovery path (config
    re-read -> tick -> publish).  A callback that bypassed the runtime and
    called ``advance_due`` directly would leave the runtime FAILED.
    """

    await delete_roulette_config(harness.engine)
    config_holder: dict[str, TogglableConfig] = {}

    def factory(
        plugin_name: str,
        config_schema: type[object],
        env_config_schema: type[object] | None,
    ) -> TogglableConfig:
        del plugin_name, config_schema, env_config_schema
        port = TogglableConfig(plugin_enable=True)
        config_holder["port"] = port
        return port

    try:
        async with lifecycle_context(monkeypatch, manager_factory=factory) as ctx:
            startup = require_single_startup_hook(ctx)
            await invoke_hook(startup)
            app = application_api()["get_roulette_application"]()
            assert app is not None
            port = config_holder["port"]
            if app.runtime.get_state().status.value == "failed":
                # start() itself may have recorded a transient failure; recover
                # once so the baseline really is READY before injecting.
                await app.runtime.run_recovery_tick()

            from komari_bot.plugins.komari_roulette.maintenance import (
                RECOVERY_JOB_ID,
            )

            job = ctx.scheduler.get_job(RECOVERY_JOB_ID)
            assert job is not None
            callback = job["func"]

            # Force a recorded failure that only a real runtime tick can clear.
            port.fail = True
            assert app.runtime.get_state().status.value == "failed"
            port.fail = False
            assert app.runtime.get_state().status.value == "failed", (
                "a readable config alone must not clear a recorded failure"
            )

            await callback()
            assert app.runtime.get_state().status.value != "failed", (
                "the registered recovery callback must drive the runtime "
                "state machine, not bypass it"
            )
            assert app.runtime.get_state().recovery_completed is True
    finally:
        await delete_roulette_config(harness.engine)


@PG_REQUIRED
async def test_business_disabled_keeps_maintenance_and_never_allows(
    harness: Tsk279Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``plugin_enable=false`` pauses business but keeps recovery scheduled."""

    from komari_bot.plugins.komari_roulette.maintenance import (
        CLEANUP_JOB_ID,
        RECOVERY_JOB_ID,
    )

    await delete_roulette_config(harness.engine)
    try:
        async with lifecycle_context(monkeypatch) as ctx:
            startup = require_single_startup_hook(ctx)
            await invoke_hook(startup)
            app = application_api()["get_roulette_application"]()
            assert app is not None

            job_ids = {job["kwargs"].get("id") for job in ctx.scheduler.jobs}
            assert job_ids == {RECOVERY_JOB_ID, CLEANUP_JOB_ID}

            # The recovery callback still runs while business is disabled.
            recovery_job = ctx.scheduler.get_job(RECOVERY_JOB_ID)
            await recovery_job["func"]()

            authority = app.runtime.authorize(scope="business", group_ids=[1])
            assert authority.allowed is False
            assert authority.reason_code == "plugin_disabled"
            assert app.runtime.accepting is False
    finally:
        await delete_roulette_config(harness.engine)


# ---------------------------------------------------------------------------
# Shutdown: remove owned jobs, clear QQ dispatch, bounded + idempotent
# ---------------------------------------------------------------------------


@PG_REQUIRED
async def test_stop_removes_owned_jobs_and_clears_qq_runtime(
    harness: Tsk279Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from komari_bot.plugins.komari_roulette.maintenance import (
        CLEANUP_JOB_ID,
        RECOVERY_JOB_ID,
    )

    await delete_roulette_config(harness.engine)
    try:
        async with lifecycle_context(monkeypatch) as ctx:
            startup = require_single_startup_hook(ctx)
            shutdown = require_single_shutdown_hook(ctx)
            await invoke_hook(startup)
            app = application_api()["get_roulette_application"]()
            assert app is not None

            qq = __import__(QQ_MODULE, fromlist=["get_roulette_qq_runtime"])
            assert qq.get_roulette_qq_runtime() is not None

            await invoke_hook(shutdown)

            assert application_api()["get_roulette_application"]() is None
            assert qq.get_roulette_qq_runtime() is None
            assert ctx.scheduler.jobs == []
            assert set(ctx.scheduler.removed_job_ids) == {
                RECOVERY_JOB_ID,
                CLEANUP_JOB_ID,
            }

            # Idempotent repeated shutdown: no exception, nothing left.
            await invoke_hook(shutdown)
            assert ctx.scheduler.jobs == []
    finally:
        await delete_roulette_config(harness.engine)


@PG_REQUIRED
async def test_stop_never_disposes_shared_orm_engine(
    harness: Tsk279Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import patch

    from nonebot import require

    require("nonebot_plugin_orm")
    import nonebot_plugin_orm as orm_module

    probe_session = orm_module.get_session()
    try:
        assert await probe_session.scalar(text("SELECT 1")) == 1
    finally:
        await probe_session.close()
    shared_engine = getattr(orm_module, "_engines", {}).get("")
    assert isinstance(shared_engine, AsyncEngine)

    await delete_roulette_config(harness.engine)
    dispose_calls: list[int] = []
    original_dispose = AsyncEngine.dispose

    async def spy_dispose(
        engine_self: AsyncEngine, *args: Any, **kwargs: Any
    ) -> None:
        dispose_calls.append(id(engine_self))
        await original_dispose(engine_self, *args, **kwargs)

    try:
        async with lifecycle_context(monkeypatch) as ctx:
            startup = require_single_startup_hook(ctx)
            shutdown = require_single_shutdown_hook(ctx)
            await invoke_hook(startup)
            with patch.object(AsyncEngine, "dispose", spy_dispose):
                await invoke_hook(shutdown)
                await invoke_hook(shutdown)
        assert dispose_calls == []
    finally:
        await delete_roulette_config(harness.engine)


@PG_REQUIRED
async def test_stop_does_not_remove_foreign_jobs(
    harness: Tsk279Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await delete_roulette_config(harness.engine)
    try:
        async with lifecycle_context(monkeypatch) as ctx:
            ctx.scheduler.add_job(
                lambda: None,
                "interval",
                seconds=7,
                id="foreign-other-plugin-job",
                replace_existing=True,
            )
            startup = require_single_startup_hook(ctx)
            shutdown = require_single_shutdown_hook(ctx)
            await invoke_hook(startup)
            await invoke_hook(shutdown)
            assert ctx.scheduler.get_job("foreign-other-plugin-job") is not None
    finally:
        await delete_roulette_config(harness.engine)
