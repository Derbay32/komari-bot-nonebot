"""Production composition root + driver lifecycle for komari_roulette (TSK-279).

This module is the single production owner of the roulette assembly.  When the
NoneBot ``Driver`` is ready it registers exactly one ``on_startup`` hook and one
``on_shutdown`` hook; the startup hook builds the real graph

    config (top-level ``get_config_manager`` registry)
      -> command service (live config projector + live item weights)
      -> maintenance worker (canonical group admission)
      -> runtime (single-flight recovery + lifecycle publish)
      -> QQ runtime install (real gates)
      -> two owned scheduler jobs (recovery interval + cleanup cron)

and the shutdown hook removes the owned jobs, clears QQ dispatch and then
bounded-closes maintenance before the runtime.

Design rules pinned by the Stage-C1 contract:

* this module never constructs ``ConfigManager`` itself - the roulette resource
  is always obtained through the config_manager top-level registry, so the
  installed service reads the *real* live config;
* no gate is ever fabricated: the installed QQ runtime receives real
  ``plugin_enable`` + ``recheck_qq_effect`` gates, the post-lock / post-claim
  authority recheck is the same per-call closure, and ``plugin_enable`` defaults
  to ``false`` so failing closed is the contract default;
* ``nonebot_plugin_orm`` is imported lazily inside functions (its plugin entry
  touches ``get_driver()`` / plugin config and must not run at module import);
* a config-acquisition failure is *not* a process-down event: startup installs a
  fail-closed bootstrap graph (safe gate, no QQ dispatch) plus the periodic
  recovery entry, which is the only thing that later recovers the dependency and
  installs the QQ runtime;
* a shutdown that races an in-flight startup can never let that startup install
  jobs / QQ / an application afterwards.
"""

from __future__ import annotations

import asyncio
import random
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from apscheduler.jobstores.base import JobLookupError
from nonebot import get_driver, logger
from nonebot_plugin_apscheduler import scheduler
from sqlalchemy import text

from komari_bot.plugins import character_binding, group_admission
from komari_bot.plugins.character_binding import BindingTransaction
from komari_bot.plugins.group_admission import (
    AdmissionIntent,
    AdmissionQualification,
)

from .command_service import RouletteCommandService
from .config_schema import DynamicConfigSchema
from .copy_pool import compile_copy_pool
from .maintenance import (
    CLEANUP_HOUR,
    CLEANUP_JOB_ID,
    CLEANUP_MINUTE,
    RECOVERY_INTERVAL_SECONDS,
    RECOVERY_JOB_ID,
    RouletteMaintenance,
    drain_cleanup,
)
from .observability import RouletteObservability, RouletteObservation, set_runtime_state
from .qq import clear_roulette_qq_runtime, install_roulette_qq_runtime
from .qq.renderer import build_live_reply_projector
from .runtime import (
    CONFIG_UNAVAILABLE_REASON,
    NOT_READY_REASON,
    RouletteRuntime,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from nonebot.adapters.qq import Bot as QQBot
    from nonebot.adapters.qq.event import GroupAtMessageCreateEvent
    from nonebot.internal.driver import Driver

    from komari_bot.plugins.group_admission import (
        AdmissionResult,
        QQAdmissionToken,
    )

    from .command_service import (
        CommandReceipt,
        CommandRequest,
        SessionFactory,
    )
    from .copy_pool import CopyPoolSnapshot
    from .domain import ItemType
    from .maintenance import CleanupResult
    from .qq.delivery import RuntimeCheck
    from .qq.handler import BusinessGate, SendGate
    from .runtime import ConfigPort


@dataclass(frozen=True, slots=True)
class RouletteApplication:
    """The assembled roulette graph installed by the startup hook.

    ``config_manager`` is the real ``ConfigManager`` obtained from the
    config_manager top-level registry on the healthy path; on the fail-closed
    bootstrap path it is the narrow lazy port that delegates to the same
    registry, so ``runtime``/``service`` still read genuine configuration.
    """

    runtime: RouletteRuntime
    service: RouletteCommandService
    maintenance: RouletteMaintenance
    config_manager: ConfigPort
    observability: RouletteObservability


class _StartupAbortedError(RuntimeError):
    """Private control flow: a shutdown raced an in-flight startup."""


class _LazyConfigPort:
    """Deferred roulette config resource for the fail-closed bootstrap path.

    Every read fails closed until the periodic recovery entry acquires the real
    manager; a successful acquisition is cached and later reads delegate to the
    real manager, so the runtime's live ``plugin_enable`` read stays
    authoritative (this port never fabricates a switch value).
    """

    __slots__ = ("_lock", "_manager")

    def __init__(self) -> None:
        self._manager: ConfigPort | None = None
        self._lock = asyncio.Lock()

    async def initialize_async(self) -> object:
        """Acquire (once) and initialize the real config resource."""

        manager = self._manager
        if manager is None:
            async with self._lock:
                if self._manager is None:
                    self._manager = _acquire_config_manager()
                manager = self._manager
        return await manager.initialize_async()

    def get(self) -> object:
        """Fail closed until the dependency has really been acquired."""

        manager = self._manager
        if manager is None:
            message = "komari_roulette 配置依赖尚未就绪"
            raise RuntimeError(message)
        return manager.get()

    async def get_async(self) -> object:
        """Fail closed until the dependency has really been acquired."""

        manager = self._manager
        if manager is None:
            message = "komari_roulette 配置依赖尚未就绪"
            raise RuntimeError(message)
        return await manager.get_async()


async def _refresh_pending_safely(observability: RouletteObservability) -> None:
    """Refresh the read-only pending count without failing the whole tick.

    ``refresh_pending`` already keeps ``pending_receipts`` at ``None`` and
    aggregates ``pending_unavailable`` before raising; this boundary only stops
    that observation fault from being mistaken for a recovery failure (the tick
    still publishes its clean result).  A cancellation is a cancellation.
    """

    try:
        await observability.refresh_pending()
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.warning(
            "[Roulette] 待确认观测读取失败，保持未知: error_type={}",
            type(error).__name__,
        )


class _RecoveryPort:
    """Adapt the maintenance worker to the runtime's recovery port.

    ``RouletteMaintenance`` is a page-oriented worker, not a "tick" port, so the
    composition root owns this one-line adapter instead of widening the
    maintenance interface.
    """

    __slots__ = ("_maintenance", "_observability")

    def __init__(
        self,
        maintenance: RouletteMaintenance,
        observability: RouletteObservability,
    ) -> None:
        self._maintenance = maintenance
        self._observability = observability

    async def run_recovery_tick(self) -> object:
        result = await self._maintenance.advance_due()
        self._observability.note_scan(result)
        # Read-only ground truth *after* the real scan ran; never a claim/resend.
        await _refresh_pending_safely(self._observability)
        return result

    async def close(self) -> None:
        await self._maintenance.close()


class _CopyPoolRandom:
    """Isolated random source for copy-pool choices.

    Copy randomness must never consume (or be consumed by) the domain random
    source, so the projector owns its own ``random.Random`` instance.
    """

    __slots__ = ("_random",)

    def __init__(self) -> None:
        self._random = random.Random()

    def choice(self, options: Sequence[str]) -> str:
        return self._random.choice(options)


# ---------------------------------------------------------------------------
# Process-local lifecycle state (single instance per process, PluginState style)
# ---------------------------------------------------------------------------


#: Bounded wait for a shutdown to settle the startup hook's own task(s).
STARTUP_CANCEL_TIMEOUT_SECONDS = 1.0


class _LifecycleState:
    """Mutable process-local lifecycle slots (avoids ``global`` rebinding).

    The slots belong to this module: the composition root is the single owner
    of the roulette lifecycle, so a reload must rebuild them together with the
    hooks rather than keep an orphaned application alive across module
    identities.
    """

    __slots__ = ("app", "bootstrap", "generation", "qq_installed", "startup_tasks")

    def __init__(self) -> None:
        self.app: RouletteApplication | None = None
        #: Non-``None`` only on the fail-closed bootstrap path.
        self.bootstrap: _LazyConfigPort | None = None
        self.qq_installed = False
        #: Bumped by every shutdown so a racing startup can detect the race.
        self.generation = 0
        #: The startup hook's own in-flight task(s); shutdown settles them.
        self.startup_tasks: set[asyncio.Task[Any]] = set()


_state = _LifecycleState()
#: Single-flights the assembly; also serializes a restart.
_start_lock = asyncio.Lock()
#: Serializes shutdown / repeated shutdown without ever touching ``_start_lock``
#: (a shutdown racing a blocked startup must not deadlock behind it).
_stop_lock = asyncio.Lock()


def _take_startup_tasks() -> set[asyncio.Task[Any]]:
    """Drain the startup-task registry, keeping only solvable tasks.

    The caller's own task and already-settled tasks are dropped, and the
    registry is cleared so a repeated shutdown sees the same empty set.
    """

    current = asyncio.current_task()
    tasks = {
        task for task in _state.startup_tasks if task is not current and not task.done()
    }
    _state.startup_tasks.clear()
    return tasks


async def _bounded_cancel_tasks(tasks: set[asyncio.Task[Any]]) -> None:
    """Wait, then cancel and bounded-wait again, for the startup task(s).

    ``asyncio.wait`` (never ``await task``) is used so a startup that swallows
    ``CancelledError`` cannot drag shutdown past the bound.  The caller's own
    task is filtered out by :func:`_take_startup_tasks`, so this never awaits
    or cancels itself.
    """

    if not tasks:
        return
    with suppress(asyncio.CancelledError, Exception):
        await asyncio.wait(tasks, timeout=STARTUP_CANCEL_TIMEOUT_SECONDS)
    pending = {task for task in tasks if not task.done()}
    if not pending:
        return
    for task in pending:
        task.cancel()
    with suppress(asyncio.CancelledError, Exception):
        await asyncio.wait(pending, timeout=STARTUP_CANCEL_TIMEOUT_SECONDS)


def get_roulette_application() -> RouletteApplication | None:
    """Return the installed application, or ``None`` when it is not live."""

    return _state.app


def get_roulette_observation() -> RouletteObservation | None:
    """Return the owner's accumulated safe observation, or ``None`` if unowned.

    The runtime status is refreshed from the live runtime *before* the snapshot
    is built (the runtime is the source of truth for its own lifecycle), but the
    scan / cleanup records come only from the real jobs that ran: nothing here
    fabricates counts at read time.
    """

    app = _state.app
    if app is None:
        return None
    set_runtime_state(app.runtime.get_state())
    return app.observability.snapshot()


def _shared_session_factory() -> SessionFactory:
    """Resolve the shared ORM session factory at call time.

    ``nonebot_plugin_orm`` must not be imported at module import: its plugin
    entry reads the driver / plugin config, which raises before ``nonebot.init``.
    """

    from nonebot_plugin_orm import get_session

    return get_session


async def _orm_reachable() -> bool:
    """Execute one real read on the shared ORM so "importable" is not readiness.

    The contract requires the startup barrier to prove the shared storage is
    actually readable: importing ``get_session`` says nothing about whether the
    engine returned by ``nonebot_plugin_orm`` can serve a statement.  Any failure
    (uninitialised engine, connection refused, ...) is a fail-closed ``False``.
    """

    try:
        async with _shared_session_factory()() as session:
            value = await session.scalar(text("SELECT 1"))
    except asyncio.CancelledError:
        raise
    except Exception:
        return False
    return value == 1


async def _dependencies_ready() -> bool:
    """Recheck the real startup dependencies (ORM + binding + admission).

    ``character_binding`` / ``group_admission`` are resolved as *module*
    attributes at call time (never bound at import), so the live public
    ``is_ready`` seams are observed.  Any failure is fail-closed.
    """

    if not await _orm_reachable():
        return False
    try:
        binding = character_binding.get_binding_manager()
        if not bool(getattr(binding, "is_ready", False)):
            return False
    except asyncio.CancelledError:
        raise
    except Exception:
        return False
    try:
        state = group_admission.get_runtime_state()
        if not bool(getattr(state, "is_ready", False)):
            return False
    except asyncio.CancelledError:
        raise
    except Exception:
        return False
    return True


def _acquire_config_manager() -> ConfigPort:
    """Obtain the roulette config resource from the top-level registry.

    Never constructs ``ConfigManager`` directly (ADR-0006 boundary and the C1
    contract): the registry is the single owner of the config resource.
    """

    from komari_bot.plugins import config_manager as config_manager_plugin

    return config_manager_plugin.get_config_manager(
        "komari_roulette", DynamicConfigSchema
    )


def _dynamic_config(config_port: ConfigPort) -> DynamicConfigSchema:
    """Read the current validated snapshot, failing closed on any other shape."""

    snapshot = config_port.get()
    if not isinstance(snapshot, DynamicConfigSchema):
        message = "komari_roulette 配置快照不可用"
        raise TypeError(message)
    return snapshot


def _snapshot_provider(config_port: ConfigPort) -> CopyPoolSnapshot:
    """Compile the *live* copy pool snapshot on every projection."""

    config = _dynamic_config(config_port)
    return compile_copy_pool(config.action_copy_pool, config.final_copy_pool)


def _item_weights_provider(
    config_port: ConfigPort,
) -> Mapping[ItemType, int]:
    """Read the *live* item weights (frozen by the domain on ``waiting -> active``)."""

    return _dynamic_config(config_port).item_weights()


def _admission_port(
    associated_group_ids: Sequence[int],
    *,
    intent: AdmissionIntent,
) -> AdmissionResult:
    """Group-level admission lookup for the runtime (no member identity)."""

    return group_admission.adjudicate(list(associated_group_ids), intent=intent)


async def _maintenance_admission(app_id: str, group_openid: str) -> bool:
    """Resolve the canonical numeric group, then consult the real admission.

    Maintenance never gates on the business ``plugin_enable`` switch: it must
    keep advancing while business is disabled.  An unresolvable / malformed
    canonical group fails closed.
    """

    numeric_group = await _resolve_canonical_group(app_id, group_openid)
    if numeric_group is None:
        return False
    outcome = group_admission.adjudicate(
        [numeric_group], intent=AdmissionIntent.BUSINESS
    )
    return outcome.qualification is AdmissionQualification.BUSINESS


async def _resolve_canonical_group(
    app_id: str,
    group_openid: str,
) -> int | None:
    """Read the canonical numeric group from the real binding transaction."""

    async with _shared_session_factory()() as session:
        group = await BindingTransaction(session).resolve_group(
            app_id=app_id,
            group_openid=group_openid,
            lock=False,
        )
    if group is None:
        return None
    try:
        value = int(group.group_id)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _build_service(config_port: ConfigPort) -> RouletteCommandService:
    """Build the command service bound to the live real configuration."""

    return RouletteCommandService(
        session_factory=_shared_session_factory(),
        reply_projector=build_live_reply_projector(
            snapshot_provider=lambda: _snapshot_provider(config_port),
            random_source=_CopyPoolRandom(),
        ),
        item_weights_provider=lambda: _item_weights_provider(config_port),
    )


def _build_application(config_port: ConfigPort) -> RouletteApplication:
    """Assemble the whole graph around an acquired config resource."""

    service = _build_service(config_port)
    observability = RouletteObservability(
        session_factory=_shared_session_factory()
    )
    maintenance = RouletteMaintenance(
        session_factory=_shared_session_factory(),
        service=service,
        admission=_maintenance_admission,
    )
    runtime = RouletteRuntime(
        config_manager=config_port,
        recovery=_RecoveryPort(maintenance, observability),
        admission=_admission_port,
    )
    return RouletteApplication(
        runtime=runtime,
        service=service,
        maintenance=maintenance,
        config_manager=config_port,
        observability=observability,
    )


# ---------------------------------------------------------------------------
# Installed QQ authority gates (all read real authority, none is a stub)
# ---------------------------------------------------------------------------


def _business_gate(app: RouletteApplication) -> BusinessGate:
    """Front-door gate: live switch + real group-admission recheck."""

    async def gate(
        bot: QQBot,
        event: GroupAtMessageCreateEvent,
        token: QQAdmissionToken,
    ) -> bool:
        del bot, event
        if not app.runtime.accepting:
            return False
        decision = await group_admission.recheck_qq_effect(
            token, effect="business"
        )
        if not decision.allowed:
            return False
        # The remote recheck is a long wait: re-read the local owner/runtime
        # state afterwards so a switch/stop that happened during the await can
        # no longer release the effect (or the send).
        return app.runtime.accepting

    return gate


def _runtime_check(app: RouletteApplication) -> RuntimeCheck:
    """Pure-local runtime-state recheck (pre-claim and post-window).

    It reads only the live runtime's local authority, so it is safe to run both
    before the send gate and again after the final DB-clock credential window;
    it never opens a network or database round trip.
    """

    async def check(receipt: CommandReceipt) -> bool:
        del receipt
        return app.runtime.accepting

    return check


def _send_gate(app: RouletteApplication) -> SendGate:
    """Pre-claim send gate for *this call's* command request."""

    async def gate(request: CommandRequest) -> bool:
        del request
        return app.runtime.accepting

    return gate


def _install_qq(app: RouletteApplication) -> None:
    """Install the real QQ adapter runtime with the real authority gates."""

    runtime_check = _runtime_check(app)
    install_roulette_qq_runtime(
        service=app.service,
        business_gate=_business_gate(app),
        runtime_check=runtime_check,
        # The DB-clock window is the last remote/DB authority, so it is
        # re-verified with a *pure-local* runtime-state recheck afterwards
        # instead of a second remote/DB read past the window.
        post_window_check=runtime_check,
        send_gate=_send_gate(app),
    )


# ---------------------------------------------------------------------------
# Owned scheduler jobs
# ---------------------------------------------------------------------------


async def _recovery_entry() -> None:
    """Periodic recovery entry: re-acquire a missing dependency, then tick.

    On the fail-closed bootstrap path this is the *only* recovery seam.  The
    dependency probe order is pinned to shared ORM -> config -> binding /
    admission, and every not-ready branch *revokes* business authority
    (recoverably) instead of returning with a stale ``READY``; the runtime is
    published ``READY`` again only by a real successful tick.
    """

    app = _state.app
    if app is None:
        return
    generation = _state.generation
    try:
        port = _state.bootstrap
        if port is not None:
            if not await _orm_reachable():
                logger.warning("[Roulette] 共享 ORM 尚不可读，故障关闭")
                app.runtime.mark_unavailable(NOT_READY_REASON)
                return
            try:
                await port.initialize_async()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "[Roulette] 配置依赖仍未恢复，保持故障关闭: error_type={}",
                    type(error).__name__,
                )
                app.runtime.mark_unavailable(CONFIG_UNAVAILABLE_REASON)
                return
            if _state.app is not app or _state.generation != generation:
                return
        if not await _dependencies_ready():
            # A not-ready binding/admission dependency must never be mistaken
            # for a clean empty scan: revoke business authority instead of
            # leaving a stale ``READY`` published.  This is not gated by the
            # business ``plugin_enable`` switch.
            logger.warning("[Roulette] 依赖尚未就绪，故障关闭")
            app.runtime.mark_unavailable(NOT_READY_REASON)
            return
        if _state.app is not app or _state.generation != generation:
            return
        if not _state.qq_installed:
            _install_qq(app)
            _state.qq_installed = True
        await app.runtime.run_recovery_tick()
    finally:
        if _state.app is app:
            set_runtime_state(app.runtime.get_state())


async def _cleanup_entry() -> CleanupResult | None:
    """Periodic retention entry: drain the backlog in bounded pages."""

    app = _state.app
    if app is None:
        return None
    result = await drain_cleanup(app.maintenance)
    app.observability.note_cleanup(result)
    # Read-only ground truth *after* the real drain ran; never a claim/resend.
    await _refresh_pending_safely(app.observability)
    set_runtime_state(app.runtime.get_state())
    return result


def _register_owned_jobs() -> None:
    """Register exactly the two owned jobs (``replace_existing`` is idempotent)."""

    scheduler.add_job(
        _recovery_entry,
        trigger="interval",
        seconds=RECOVERY_INTERVAL_SECONDS,
        id=RECOVERY_JOB_ID,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        _cleanup_entry,
        trigger="cron",
        hour=CLEANUP_HOUR,
        minute=CLEANUP_MINUTE,
        id=CLEANUP_JOB_ID,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )


def _remove_owned_jobs() -> None:
    """Remove only the two owned jobs, tolerating an already-absent job.

    Deliberately uses ``remove_job`` (never ``get_job``) so any scheduler
    exposing only the APScheduler removal surface keeps working, and repeated /
    foreign-scheduler shutdowns stay idempotent.
    """

    for job_id in (RECOVERY_JOB_ID, CLEANUP_JOB_ID):
        try:
            scheduler.remove_job(job_id)
        except JobLookupError:
            continue
        except Exception as error:
            logger.warning(
                "[Roulette] 注销自有周期任务失败: error_type={}",
                type(error).__name__,
            )


# ---------------------------------------------------------------------------
# Fail-closed / degraded installers
# ---------------------------------------------------------------------------


def _install_bootstrap(generation: int) -> RouletteApplication:
    """Install the fail-closed bootstrap graph the periodic entry recovers.

    The app is built over :class:`_LazyConfigPort`, so every read fails closed
    until the periodic entry acquires the real manager; the owned jobs are
    registered so that entry really runs.  The generation is rechecked first so
    a startup that lost the race with a completed shutdown never resurrects an
    application or its jobs.
    """

    if generation != _state.generation:
        raise _StartupAbortedError
    lazy = _LazyConfigPort()
    app = _build_application(lazy)
    _state.bootstrap = lazy
    _state.app = app
    _register_owned_jobs()
    return app


def _install_degraded(
    config_port: ConfigPort,
    generation: int,
) -> RouletteApplication:
    """Install a real-config graph whose dependencies are not ready yet.

    The config resource is genuine, but ``runtime.start()`` is deliberately not
    called (no recovery, no READY) and QQ dispatch is not installed; the
    periodic entry rechecks the real dependencies and only then resumes.
    """

    if generation != _state.generation:
        raise _StartupAbortedError
    app = _build_application(config_port)
    _state.app = app
    _register_owned_jobs()
    return app


# ---------------------------------------------------------------------------
# Start / stop
# ---------------------------------------------------------------------------


async def start_roulette_application() -> RouletteApplication:
    """Assemble and install the roulette application (single-flight).

    A healthy config dependency yields the real graph, installs the QQ runtime
    and registers the owned jobs.  A dependency acquisition / initialization
    failure installs the fail-closed bootstrap graph plus the periodic recovery
    entry instead of raising, and never installs QQ dispatch while the
    dependency is down.  The caller's task is registered so a shutdown that
    races this startup can settle it instead of leaving it pending.
    """

    task = asyncio.current_task()
    if task is not None:
        _state.startup_tasks.add(task)
    try:
        return await _start_roulette_locked()
    finally:
        if task is not None:
            _state.startup_tasks.discard(task)


async def _start_roulette_locked() -> RouletteApplication:
    """The single-flighted assembly body (see :func:`start_roulette_application`)."""

    async with _start_lock:
        if _state.app is not None:
            return _state.app
        generation = _state.generation
        # Real dependency barrier: the shared ORM must serve a read *before* any
        # config acquisition, so an uninitialised engine cannot masquerade as a
        # healthy start.
        orm_ok = await _orm_reachable()
        if generation != _state.generation:
            raise _StartupAbortedError
        if not orm_ok:
            logger.error("[Roulette] 共享 ORM 尚不可读，故障关闭并等待周期恢复")
            return _install_bootstrap(generation)
        try:
            config_port = _acquire_config_manager()
            await config_port.initialize_async()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "[Roulette] 配置依赖获取失败，故障关闭并等待周期恢复: error_type={}",
                type(error).__name__,
            )
            return _install_bootstrap(generation)
        if generation != _state.generation:
            raise _StartupAbortedError
        # The binding/admission dependencies are part of the barrier: a not-ready
        # dependency must fail closed instead of passing as an empty-scan start.
        if not await _dependencies_ready():
            logger.error("[Roulette] 依赖尚未就绪，故障关闭并等待周期恢复")
            return _install_degraded(config_port, generation)
        if generation != _state.generation:
            raise _StartupAbortedError
        app = _build_application(config_port)
        await app.runtime.start()
        if generation != _state.generation:
            with suppress(Exception):
                await app.runtime.close()
            raise _StartupAbortedError
        _install_qq(app)
        _state.qq_installed = True
        _register_owned_jobs()
        _state.app = app
        set_runtime_state(app.runtime.get_state())
        return app


async def stop_roulette_application() -> None:
    """Tear the application down in dependency order (idempotent).

    The generation bump happens first and *before* any wait, so a startup
    blocked on the config dependency can never install a late graph.  Owned jobs
    and QQ dispatch are cleared before the bounded closes; maintenance is closed
    before the runtime so an in-flight tick observes the owner's stop state, and
    the startup hook's own task(s) are bounded-cancelled/joined last.  The
    shared ORM engine is never disposed, and repeated shutdowns share the same
    idempotent path.
    """

    async with _stop_lock:
        _state.generation += 1
        app = _state.app
        _state.app = None
        _state.bootstrap = None
        _state.qq_installed = False
        _remove_owned_jobs()
        clear_roulette_qq_runtime()
        # Drain the startup registry *before* any wait: a blocked startup must
        # be settled by this shutdown, and clearing it here keeps a repeated
        # stop idempotent (it observes the same, already-empty, registry).
        startup_tasks = _take_startup_tasks()
        if app is not None:
            # Revoke local authority *synchronously* before any bounded wait: an
            # already-installed gate must reject immediately, not only after the
            # maintenance drain releases the group lock.  The maintenance stop
            # flag is flipped for the same reason, so a blocked round's post-lock
            # gate observes it when the lock is handed over.
            app.runtime.freeze()
            app.maintenance.mark_stopped()
            with suppress(Exception):
                await app.maintenance.close()
            with suppress(Exception):
                await app.runtime.close()
        # Settle the startup hook's own task(s) last: the generation guard
        # already blocks a late install, and this bound guarantees shutdown does
        # not return while that task is still pending (never awaiting self).
        await _bounded_cancel_tasks(startup_tasks)


async def _startup() -> None:
    """Driver startup hook: assemble the application; never crash the process."""

    try:
        await start_roulette_application()
    except _StartupAbortedError:
        logger.warning("[Roulette] 启动流程已中止：shutdown 先到达")
    except Exception as error:
        logger.error(
            "[Roulette] 生命周期启动失败，故障关闭: error_type={}",
            type(error).__name__,
        )


async def _shutdown() -> None:
    """Driver shutdown hook: tear down; never crash the process."""

    try:
        await stop_roulette_application()
    except Exception as error:
        logger.error(
            "[Roulette] 生命周期关闭失败: error_type={}",
            type(error).__name__,
        )


def _install_lifecycle(driver: Driver) -> None:
    """Register exactly one startup + one shutdown hook (private, once)."""

    driver.on_startup(_startup)
    driver.on_shutdown(_shutdown)


try:
    _driver: Driver | None = get_driver()
except ValueError:
    # Test / tooling import before ``nonebot.init()``: skip the driver assembly.
    # The supported production path initializes the driver before loading plugins.
    _driver = None

if _driver is not None:
    _install_lifecycle(_driver)


__all__ = [
    "RouletteApplication",
    "get_roulette_application",
    "get_roulette_observation",
    "start_roulette_application",
    "stop_roulette_application",
]
