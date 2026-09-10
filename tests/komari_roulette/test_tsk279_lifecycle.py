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

import asyncio
from contextlib import suppress
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .command_support import (
    PG_REQUIRED,
    backend_pid,
    hold_group_lock,
    wait_for_blocked,
)
from .tsk279_lifecycle_support import (
    APPLICATION_FUNCTIONS,
    QQ_MODULE,
    application_api,
    delete_roulette_config,
    install_dependency_readiness,
    invoke_hook,
    lifecycle_context,
    lifecycle_symbol,
    require_single_shutdown_hook,
    require_single_startup_hook,
)
from .tsk279_support import harness_fixture_body, insert_waiting_game

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


class BlockingConfig(TogglableConfig):
    """Config port whose first ``initialize_async`` blocks until released.

    Used to prove a shutdown that races an in-flight (config-initialize) startup
    never lets the startup install the QQ runtime after shutdown returned.
    """

    def __init__(self, *, plugin_enable: bool = True) -> None:
        super().__init__(plugin_enable=plugin_enable)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def initialize_async(self, *args: object, **kwargs: object) -> Any:
        self.entered.set()
        await self.release.wait()
        return await super().initialize_async(*args, **kwargs)


class BlockingFailingConfig(TogglableConfig):
    """Config port whose ``initialize_async`` blocks, then fails.

    Used to prove a shutdown that completed first must also abort a *failed*
    startup: the generation guard currently only covers the healthy branch, so a
    dependency that fails after shutdown still installs the fail-closed bootstrap
    graph and re-registers the owned jobs on top of a stopped process.
    """

    def __init__(self, *, plugin_enable: bool = True) -> None:
        super().__init__(plugin_enable=plugin_enable)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def initialize_async(self, *args: object, **kwargs: object) -> Any:
        del args, kwargs
        self.entered.set()
        await self.release.wait()
        message = "config dependency failed after shutdown (C1 test)"
        raise RuntimeError(message)


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
        assert id(shared_engine) not in dispose_calls, (
            "shutdown must never dispose the shared nonebot-plugin-orm engine"
        )
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


# ---------------------------------------------------------------------------
# Dependency acquisition / lifecycle race: fail safe, recover, no late install
# ---------------------------------------------------------------------------


@PG_REQUIRED
async def test_start_config_acquisition_failure_is_safe_and_recovers(
    harness: Tsk279Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config-acquisition failure must fail closed but stay recoverable.

    The startup hook must not raise the process down: it installs a safe
    failed / not-ready gate with no QQ dispatch, and the registered periodic
    recovery entry is the only thing that later turns the runtime ready and
    installs the QQ runtime (no manual re-install).  It must never claim success
    while the dependency is still down.
    """

    from komari_bot.plugins.komari_roulette.maintenance import RECOVERY_JOB_ID

    await delete_roulette_config(harness.engine)
    try:
        async with lifecycle_context(
            monkeypatch,
            acquisition_error=RuntimeError("config storage unavailable (C1 test)"),
        ) as ctx:
            startup = require_single_startup_hook(ctx)
            await invoke_hook(startup)

            qq = __import__(QQ_MODULE, fromlist=["get_roulette_qq_runtime"])
            assert qq.get_roulette_qq_runtime() is None, (
                "a failed config acquisition must never install the QQ runtime"
            )

            app = application_api()["get_roulette_application"]()
            assert app is not None, (
                "the failed gate must stay installed so the periodic entry can "
                "recover it"
            )
            assert app.runtime.get_state().status.value in {"failed", "disabled"}
            assert app.runtime.accepting is False
            assert (
                app.runtime.authorize(scope="business", group_ids=[1]).allowed
                is False
            )

            job = ctx.scheduler.get_job(RECOVERY_JOB_ID)
            assert job is not None, (
                "the controlled periodic recovery entry must be registered even "
                "when config acquisition failed"
            )

            # Controlled recovery: the dependency comes back.  The real default
            # config disables business (plugin_enable=false), so the runtime must
            # land DISABLED with recovery completed - never a fabricated READY.
            ctx.config_getter_state["error"] = None
            await job["func"]()
            disabled = app.runtime.get_state()
            assert disabled.status.value == "disabled", (
                "recovering with the real default config (plugin_enable=false) "
                f"must be DISABLED, got {disabled.status.value}"
            )
            assert disabled.recovery_completed is True
            assert app.runtime.accepting is False
            assert qq.get_roulette_qq_runtime() is not None, (
                "recovery must install the QQ runtime once the dependency is "
                "healthy"
            )

            # A real live flip through the same registry manager (not a fake
            # port mutation) must reach READY through the periodic entry.
            manager = ctx.config_managers["komari_roulette"]
            await manager.update_field_async("plugin_enable", value=True)
            await job["func"]()
            ready = app.runtime.get_state()
            assert ready.status.value == "ready", (
                f"an enabled live config must be READY, got {ready.status.value}"
            )
            assert ready.recovery_completed is True
            assert app.runtime.accepting is True
    finally:
        await delete_roulette_config(harness.engine)


@PG_REQUIRED
async def test_stop_while_owned_recovery_waits_on_group_lock_settles(
    harness: Tsk279Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown must settle an owned recovery callback blocked on the group lock.

    The recovery callback is a real owned task; while it waits on the group
    advisory lock a shutdown arrives.  Shutdown must not leave a blocked owner
    running indefinitely: it removes the owned jobs, clears QQ dispatch and
    leaves the queued effect un-applied (the maintenance post-lock gate observes
    the owner's stop state).  A foreign plugin job must be untouched.
    """

    from komari_bot.plugins import group_admission
    from komari_bot.plugins.komari_roulette.maintenance import (
        CLEANUP_JOB_ID,
        RECOVERY_JOB_ID,
    )
    from tests.group_admission.management_support import prepare_control_plane
    from tests.group_admission.runtime_support import (
        AdmissionStorageFake,
        stored_policy,
    )

    numeric_group = 770041
    storage = AdmissionStorageFake(
        stored_policy(1, {"mode": "blacklist", "group_ids": []})
    )
    await prepare_control_plane(monkeypatch, storage)

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

            qq = __import__(QQ_MODULE, fromlist=["get_roulette_qq_runtime"])
            async with harness.scope("stop-lock") as current:
                # Canonical numeric mapping so the installed maintenance
                # admission can really adjudicate this group.
                await harness.binding_manager.bind_group_member(
                    app_id=current.app_id,
                    group_id=str(numeric_group),
                    group_openid=current.group_openid,
                    member_qq="7700410001",
                    member_openid=current.member_openid,
                    character_name="Seat 1",
                    bot_self_id="tsk279-test-bot",
                )
                await insert_waiting_game(
                    harness.session_factory,
                    game_id=str(uuid4()),
                    app_id=current.app_id,
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                    # Very old deadline so this candidate is the first keyset
                    # page and the worker reliably reaches the group lock.
                    deadline_age_seconds=315_360_000,
                )
                job = ctx.scheduler.get_job(RECOVERY_JOB_ID)
                assert job is not None
                async with harness.session_factory() as blocker:
                    await blocker.begin()
                    blocker_pid = await backend_pid(blocker)
                    await hold_group_lock(blocker, current)
                    owner_task = asyncio.create_task(job["func"]())
                    try:
                        await wait_for_blocked(harness.session_factory, blocker_pid)
                        stop_task = asyncio.create_task(invoke_hook(shutdown))
                        await asyncio.sleep(0.2)
                    finally:
                        await blocker.commit()
                    async with asyncio.timeout(10):
                        await stop_task
                    with suppress(Exception):
                        await asyncio.wait_for(owner_task, timeout=10)

                async with harness.session_factory() as session:
                    lifecycle = await session.scalar(
                        text(
                            "SELECT lifecycle FROM komari_roulette_games "
                            "WHERE app_id = :app_id AND group_openid = :group_openid"
                        ),
                        {
                            "app_id": current.app_id,
                            "group_openid": current.group_openid,
                        },
                    )
                assert lifecycle == "waiting", (
                    "a recovery callback stopped while queued must not advance "
                    "the game after shutdown"
                )

            assert qq.get_roulette_qq_runtime() is None
            assert ctx.scheduler.get_job(RECOVERY_JOB_ID) is None
            assert ctx.scheduler.get_job(CLEANUP_JOB_ID) is None
            assert ctx.scheduler.get_job("foreign-other-plugin-job") is not None
    finally:
        group_admission.register_qq_group_resolver(None)
        await delete_roulette_config(harness.engine)


@PG_REQUIRED
async def test_stop_during_blocking_config_initialize_never_installs_later(
    harness: Tsk279Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shutdown racing a blocked startup must never let it install afterwards.

    The startup hook is suspended inside ``ConfigManager.initialize_async`` when
    shutdown runs; once the blocked initialize is released the startup must not
    register owned jobs, install the QQ runtime or leave an app behind.  This is
    the "no new tasks / no later QQ install" ordering guarantee.
    """

    from komari_bot.plugins.komari_roulette.maintenance import (
        CLEANUP_JOB_ID,
        RECOVERY_JOB_ID,
    )

    await delete_roulette_config(harness.engine)
    holder: dict[str, BlockingConfig] = {}
    constructed = asyncio.Event()

    def factory(
        plugin_name: str,
        config_schema: type[object],
        env_config_schema: type[object] | None,
    ) -> BlockingConfig:
        del plugin_name, config_schema, env_config_schema
        port = BlockingConfig(plugin_enable=True)
        holder["port"] = port
        constructed.set()
        return port

    try:
        async with lifecycle_context(monkeypatch, manager_factory=factory) as ctx:
            startup = require_single_startup_hook(ctx)
            shutdown = require_single_shutdown_hook(ctx)
            start_task = asyncio.create_task(invoke_hook(startup))
            # The manager factory only runs once the startup task is scheduled;
            # wait for the handshake before reading the port (never a KeyError).
            async with asyncio.timeout(5):
                await constructed.wait()
            port = holder["port"]
            async with asyncio.timeout(5):
                await port.entered.wait()

            async with asyncio.timeout(10):
                await invoke_hook(shutdown)

            port.release.set()
            with suppress(Exception):
                await asyncio.wait_for(start_task, timeout=10)

            qq = __import__(QQ_MODULE, fromlist=["get_roulette_qq_runtime"])
            assert qq.get_roulette_qq_runtime() is None, (
                "a startup that lost the race with shutdown must never install "
                "the QQ runtime"
            )
            assert ctx.scheduler.get_job(RECOVERY_JOB_ID) is None
            assert ctx.scheduler.get_job(CLEANUP_JOB_ID) is None
    finally:
        await delete_roulette_config(harness.engine)


# ---------------------------------------------------------------------------
# Dependency readiness (binding / admission) + real job -> observation
# ---------------------------------------------------------------------------


async def test_character_binding_manager_is_ready_is_false_after_failed_initialize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public ``is_ready`` property must report a failed initialize as not
    ready, without reading private state."""

    from komari_bot.plugins.character_binding.database import CharacterBindingDB
    from komari_bot.plugins.character_binding.manager import CharacterBindingManager

    manager = CharacterBindingManager()

    async def _boom(self: object) -> None:
        del self
        raise RuntimeError("binding storage unavailable (C1 test)")  # noqa: TRY003

    monkeypatch.setattr(CharacterBindingDB, "initialize", _boom)
    await manager.initialize()
    assert manager.is_ready is False


_DEPENDENCY_RED_CASES = ("binding", "admission")


@PG_REQUIRED
@pytest.mark.parametrize("dependency", _DEPENDENCY_RED_CASES)
async def test_start_fails_closed_when_dependency_not_ready_and_recovers(
    harness: Tsk279Harness,
    monkeypatch: pytest.MonkeyPatch,
    dependency: str,
) -> None:
    """A not-ready binding/admission dependency must fail closed, never pass as
    an empty-scan success; the controlled periodic entry must recover it.
    """

    from komari_bot.plugins.komari_roulette.maintenance import RECOVERY_JOB_ID

    toggles = install_dependency_readiness(
        monkeypatch,
        binding_ready=dependency != "binding",
        admission_ready=dependency != "admission",
    )
    await delete_roulette_config(harness.engine)
    try:
        async with lifecycle_context(monkeypatch, ready_dependencies=False) as ctx:
            await invoke_hook(require_single_startup_hook(ctx))
            app = application_api()["get_roulette_application"]()
            assert app is not None, (
                "the fail-closed bootstrap graph must stay installed so the "
                "periodic entry can recover the dependency"
            )
            state = app.runtime.get_state()
            assert state.status.value == "failed", (
                f"a not-ready {dependency} dependency must not pass as "
                f"{state.status.value}: an empty scan is not a ready graph"
            )
            assert state.recovery_completed is False
            assert app.runtime.accepting is False
            qq = __import__(QQ_MODULE, fromlist=["get_roulette_qq_runtime"])
            assert qq.get_roulette_qq_runtime() is None, (
                "a not-ready dependency must never install QQ dispatch"
            )

            toggles.binding.ready = True
            toggles.admission.ready = True
            job = ctx.scheduler.get_job(RECOVERY_JOB_ID)
            assert job is not None
            await job["func"]()
            recovered = app.runtime.get_state()
            assert recovered.status.value in {"ready", "disabled"}, (
                "the periodic entry must recover to ready/disabled once the "
                f"dependency is healthy, got {recovered.status.value}"
            )
            assert qq.get_roulette_qq_runtime() is not None, (
                "recovery must install QQ dispatch"
            )
    finally:
        await delete_roulette_config(harness.engine)


@PG_REQUIRED
async def test_registered_jobs_feed_the_owner_observability_snapshot(
    harness: Tsk279Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real registered jobs must synchronise the owner's safe observation.

    The snapshot must come from the jobs that actually ran (startup recovery +
    registered recovery/cleanup callbacks), never be fabricated at read time,
    and must never claim or resend a pending receipt.
    """

    from komari_bot.plugins.komari_roulette.maintenance import (
        CLEANUP_JOB_ID,
        RECOVERY_JOB_ID,
    )

    get_observation = lifecycle_symbol("get_roulette_observation")
    await delete_roulette_config(harness.engine)
    try:
        async with lifecycle_context(monkeypatch) as ctx:
            await invoke_hook(require_single_startup_hook(ctx))
            app = application_api()["get_roulette_application"]()
            assert app is not None

            # The startup recovery tick really ran, so its safe record exists
            # before any read-time fabrication could happen.
            snapshot = get_observation()
            assert snapshot is not None, (
                "the composition root must expose a public observation snapshot"
            )
            assert snapshot.latest_scan is not None, (
                "the real startup recovery tick must synchronise latest_scan"
            )
            assert snapshot.runtime_status == app.runtime.get_state().status.value, (
                "the observation runtime status must track the live runtime"
            )

            await ctx.scheduler.get_job(RECOVERY_JOB_ID)["func"]()
            after_recovery = get_observation()
            assert (
                after_recovery.runtime_status
                == app.runtime.get_state().status.value
            )
            scan_after_recovery = after_recovery.latest_scan
            assert scan_after_recovery is not None

            await ctx.scheduler.get_job(CLEANUP_JOB_ID)["func"]()
            after_cleanup = get_observation()
            assert after_cleanup.latest_cleanup is not None, (
                "the cleanup callback must synchronise latest_cleanup"
            )
            # A different job must not rewrite the already-run scan record.
            assert after_cleanup.latest_scan == scan_after_recovery, (
                "cleanup must not replace the recorded recovery scan"
            )
            assert after_cleanup.runtime_status == after_recovery.runtime_status
    finally:
        await delete_roulette_config(harness.engine)


@PG_REQUIRED
async def test_stop_then_failed_config_initialize_never_installs_bootstrap(
    harness: Tsk279Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A *failed* startup that lost the race with shutdown must not install.

    The healthy branch guards late installs with the generation check; the
    failure branch (which installs the fail-closed bootstrap graph and the owned
    jobs) must apply the same guard.  Otherwise a dependency that fails after
    shutdown completed resurrects an application and its scheduler jobs on a
    stopped process.
    """

    from komari_bot.plugins.komari_roulette.maintenance import (
        CLEANUP_JOB_ID,
        RECOVERY_JOB_ID,
    )

    await delete_roulette_config(harness.engine)
    holder: dict[str, BlockingFailingConfig] = {}
    constructed = asyncio.Event()

    def factory(
        plugin_name: str,
        config_schema: type[object],
        env_config_schema: type[object] | None,
    ) -> BlockingFailingConfig:
        del plugin_name, config_schema, env_config_schema
        port = BlockingFailingConfig(plugin_enable=True)
        holder["port"] = port
        constructed.set()
        return port

    try:
        async with lifecycle_context(monkeypatch, manager_factory=factory) as ctx:
            startup = require_single_startup_hook(ctx)
            shutdown = require_single_shutdown_hook(ctx)
            start_task = asyncio.create_task(invoke_hook(startup))
            async with asyncio.timeout(5):
                await constructed.wait()
            port = holder["port"]
            async with asyncio.timeout(5):
                await port.entered.wait()

            # Shutdown wins the race: the blocked initialize has not returned.
            async with asyncio.timeout(10):
                await invoke_hook(shutdown)

            # The dependency then fails; the startup must not install anything.
            port.release.set()
            with suppress(Exception):
                await asyncio.wait_for(start_task, timeout=10)

            assert application_api()["get_roulette_application"]() is None, (
                "a failed startup that raced a completed shutdown must not "
                "install the fail-closed bootstrap application"
            )
            qq = __import__(QQ_MODULE, fromlist=["get_roulette_qq_runtime"])
            assert qq.get_roulette_qq_runtime() is None
            assert ctx.scheduler.get_job(RECOVERY_JOB_ID) is None, (
                "a failed startup that raced a completed shutdown must not "
                "re-register the recovery job"
            )
            assert ctx.scheduler.get_job(CLEANUP_JOB_ID) is None, (
                "a failed startup that raced a completed shutdown must not "
                "re-register the cleanup job"
            )
    finally:
        await delete_roulette_config(harness.engine)
