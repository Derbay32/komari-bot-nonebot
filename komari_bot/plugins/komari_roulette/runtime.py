"""Roulette lifecycle / per-call authority runtime (TSK-279 Stage-B2).

The runtime is a deep module with a deliberately small interface:

* :meth:`RouletteRuntime.start` reads the persisted config once, awaits the
  first recovery tick to settle, and only then publishes the lifecycle, so
  authority is never released on a merely *scheduled* recovery.  Concurrent
  callers share that single startup pass and later calls are idempotent;
* :meth:`RouletteRuntime.close` blocks every new dispatch *first*, then
  bounded-cancels the in-flight recovery tick and the recovery port's own
  ``close`` (which may swallow a cancellation), and never disposes the shared
  ``nonebot_plugin_orm`` engine (it does not own it);
* :meth:`RouletteRuntime.authorize` resolves admission from the *arguments of
  that call* (scope + group ids), never from a process-global "current event";
* the ``plugin_enable`` switch is re-read live on every authoritative read, so a
  persisted flip stops new business/send immediately without a restart, while a
  live read failure is recorded as ``config_unavailable`` instead of keeping a
  stale ``READY``.

Recovery is single-flight: the startup tick, the scheduled tick and any caller
of :meth:`run_recovery_tick` all share **one** in-flight task, so two callers
can never advance the same deadline twice.  A tick that reports per-group
failures, raises, or returns a malformed result downgrades the runtime to
``failed`` and withholds business authority until a later clean tick.

No always-true authority is fabricated here: ``admission`` is injected by the
composition root and an unknown scope is always denied.  Real token / binding /
``user_ban`` translation and the driver assembly are Stage-C.
"""

# This module deliberately keeps its operator-facing errors short.

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from komari_bot.plugins.group_admission import (
    AdmissionIntent,
    AdmissionQualification,
    AdmissionResult,
)

from .maintenance import RecoveryTickResult
from .reasons import RUNTIME_REASON_CODES

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Bounded wait for an in-flight dispatch / recovery ``close`` to honour it.
CLOSE_CANCEL_TIMEOUT_SECONDS = 1.0

#: Scopes this runtime knows how to adjudicate.  Anything else is denied.
_KNOWN_SCOPES: frozenset[str] = frozenset({"business", "send"})

#: Process-wide recovery tick failures map to one fixed reason.
RECOVERY_FAILED_REASON = "recovery_failed"
CONFIG_UNAVAILABLE_REASON = "config_unavailable"
NOT_READY_REASON = "not_ready"
PLUGIN_DISABLED_REASON = "plugin_disabled"
POLICY_ADMITTED_REASON = "policy_admitted"
POLICY_RESTRICTED_REASON = "policy_restricted"
ADMISSION_UNAVAILABLE_REASON = "admission_unavailable"


class RouletteRuntimeStatus(StrEnum):
    """Published lifecycle status."""

    READY = "ready"
    DISABLED = "disabled"
    FAILED = "failed"


class ConfigPort(Protocol):
    """Narrow view of the real ``ConfigManager`` read API."""

    async def initialize_async(self) -> object: ...

    def get(self) -> object: ...

    async def get_async(self) -> object: ...


class RecoveryPort(Protocol):
    """Narrow view of the maintenance recovery worker."""

    async def run_recovery_tick(self) -> object: ...

    async def close(self) -> None: ...


class AdmissionPort(Protocol):
    """Group-level admission lookup; carries no member identity."""

    def __call__(
        self,
        associated_group_ids: Sequence[int],
        *,
        intent: AdmissionIntent,
    ) -> AdmissionResult: ...


@dataclass(frozen=True, slots=True)
class RouletteRuntimeState:
    """Immutable projection of the runtime lifecycle."""

    status: RouletteRuntimeStatus
    reason_code: str | None
    recovery_completed: bool
    plugin_enable: bool


@dataclass(frozen=True, slots=True)
class RouletteAuthority:
    """One per-call authority decision; never cached across calls."""

    allowed: bool
    reason_code: str
    scope: str


def _retrieve_background_error(task: asyncio.Task[Any]) -> None:
    """Mark a fire-and-forget task's exception as retrieved.

    Background dispatches and the recovery ``close`` task are not awaited by
    their creator in every path; without this the event loop would log "Task
    exception was never retrieved" while the state transition (if any) was
    already recorded.
    """

    with suppress(asyncio.CancelledError, Exception):
        task.exception()


def _snapshot_plugin_enable(snapshot: object) -> bool | None:
    """Return the snapshot's ``plugin_enable`` only when it is a real ``bool``.

    A missing field, ``None`` or any other shape (e.g. the string ``"false"``)
    is *not* coerced; it means the config cannot be trusted and the caller must
    fail closed.
    """

    value = getattr(snapshot, "plugin_enable", None)
    if not isinstance(value, bool):
        return None
    return value


def _validated_failure_count(result: object) -> int | None:
    """Return ``result.failed`` only for a contract-valid recovery tick.

    The port must yield the real :class:`RecoveryTickResult`; a foreign object
    (``object`` / ``SimpleNamespace`` / text) and a missing / ``None`` / text /
    negative / bool ``failed`` are never coerced to ``0`` and must not be read
    as a clean recovery.
    """

    if not isinstance(result, RecoveryTickResult):
        return None
    failed = result.failed
    if isinstance(failed, bool) or not isinstance(failed, int) or failed < 0:
        return None
    return failed


async def _bounded_cancel(task: asyncio.Task[Any]) -> None:
    """Wait for a task, then cancel and bounded-wait again if it is stubborn.

    ``asyncio.wait`` (not ``asyncio.timeout`` around ``await task``) is used so
    a dependency that swallows ``CancelledError`` can never drag shutdown past
    the bound.  The task keeps its owning reference for error retrieval.
    """

    if task.done():
        return
    with suppress(asyncio.CancelledError, Exception):
        await asyncio.wait({task}, timeout=CLOSE_CANCEL_TIMEOUT_SECONDS)
    if task.done():
        return
    task.cancel()
    with suppress(asyncio.CancelledError, Exception):
        await asyncio.wait({task}, timeout=CLOSE_CANCEL_TIMEOUT_SECONDS)


class RouletteRuntime:
    """Own the roulette lifecycle and per-call admission authority."""

    def __init__(
        self,
        *,
        config_manager: ConfigPort,
        recovery: RecoveryPort,
        admission: AdmissionPort,
    ) -> None:
        self._config_manager = config_manager
        self._recovery = recovery
        self._admission = admission
        self._status = RouletteRuntimeStatus.FAILED
        self._reason_code: str | None = NOT_READY_REASON
        self._recovery_completed = False
        self._plugin_enable = False
        self._closed = False
        self._start_done = False
        self._tick_task: asyncio.Task[Any] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._recovery_close_task: asyncio.Task[None] | None = None
        self._start_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Read config once, await one recovery tick, publish the lifecycle.

        A config read that is unavailable or malformed is recorded as
        ``FAILED`` / ``config_unavailable`` (never a silent fallback) and no
        tick is dispatched.  A valid config releases the startup barrier and
        dispatches the single-flight recovery tick; ``start`` does not return
        until that tick has settled, so ``await start()`` means "startup
        recovery has really run", not "a tick was scheduled".  A failing tick
        is recorded as ``failed`` without raising, and ``CancelledError`` is a
        real cancellation and propagates.  Overlapping calls share this one
        pass; a later call is idempotent.
        """

        async with self._start_lock:
            if self._closed or self._start_done:
                return
            try:
                snapshot = await self._config_manager.initialize_async()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._set_failed(CONFIG_UNAVAILABLE_REASON)
                self._start_done = True
                return
            if self._closed:
                self._start_done = True
                return
            enabled = _snapshot_plugin_enable(snapshot)
            if enabled is None:
                self._set_failed(CONFIG_UNAVAILABLE_REASON)
                self._start_done = True
                return
            self._plugin_enable = enabled
            task = self._ensure_tick_task()
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                raise
            except Exception:
                # The guarded tick already recorded the failure; startup itself
                # still completes so the caller can observe the FAILED state.
                pass
            finally:
                self._start_done = True

    async def close(self) -> None:
        """Block new dispatch immediately, then bounded-cancel the in-flight work.

        The closed flag is set *before* any wait, so a task holding the runtime
        lock can never deadlock shutdown and a late tick result can no longer
        write ``READY`` back.  Concurrent / repeated callers all join the same
        close task instead of assuming the first one finished.  The shared ORM
        engine is owned by ``nonebot_plugin_orm`` and is never disposed here.
        """

        async with self._close_lock:
            if self._close_task is None:
                self._closed = True
                self._recovery_completed = False
                self._status = RouletteRuntimeStatus.FAILED
                self._reason_code = NOT_READY_REASON
                self._close_task = asyncio.ensure_future(self._close_impl())
                self._close_task.add_done_callback(_retrieve_background_error)
            task = self._close_task
        await asyncio.shield(task)

    @property
    def accepting(self) -> bool:
        """Whether the runtime is alive, configured and switched on.

        The plugin switch is read live: a persisted ``True -> False`` flip stops
        business immediately.  A runtime that recorded a failed recovery is not
        accepting, and a runtime past ``close()`` is never accepting again.
        """

        if self._closed:
            return False
        self._refresh_safe_state()
        if self._status is RouletteRuntimeStatus.FAILED:
            return False
        return self._plugin_enable

    def get_state(self) -> RouletteRuntimeState:
        """Refresh the safe state, then return an immutable snapshot.

        The live read is applied *before* the snapshot is built, so the status
        and the ``plugin_enable`` flag can never come from different points in
        time; a live read failure downgrades the state to ``FAILED`` /
        ``config_unavailable`` instead of reporting ``READY`` alongside an
        unavailable config.
        """

        self._refresh_safe_state()
        return RouletteRuntimeState(
            status=self._status,
            reason_code=self._reason_code,
            recovery_completed=self._recovery_completed,
            plugin_enable=self._plugin_enable,
        )

    async def run_recovery_tick(self) -> RouletteRuntimeState:
        """Join (or start) the single-flight recovery tick and report state.

        A tick is only dispatched when the config is legally readable; an
        unreadable config fails closed (``config_unavailable``) without running
        recovery.  When a tick is already in flight the caller joins that exact
        task, so two concurrent workers never advance one deadline twice.  A
        failed tick is re-raised after the failure has been recorded.
        """

        if self._closed:
            return self.get_state()
        if self._refresh_safe_state() is None:
            return self.get_state()
        task = self._ensure_tick_task()
        await asyncio.shield(task)
        return self.get_state()

    def authorize(  # noqa: PLR0911 - one guard per closed-set decision
        self,
        *,
        scope: str,
        group_ids: Sequence[int],
    ) -> RouletteAuthority:
        """Adjudicate *this* call's scope/group ids; never a global event.

        The admission port is queried with the group ids of this call only, so
        two concurrent groups can never borrow each other's decision.
        """

        if self._closed:
            return RouletteAuthority(
                allowed=False, reason_code=NOT_READY_REASON, scope=scope
            )
        if scope not in _KNOWN_SCOPES:
            return RouletteAuthority(
                allowed=False, reason_code=POLICY_RESTRICTED_REASON, scope=scope
            )
        enabled = self._refresh_safe_state()
        if enabled is None:
            return RouletteAuthority(
                allowed=False, reason_code=CONFIG_UNAVAILABLE_REASON, scope=scope
            )
        if self._status is RouletteRuntimeStatus.FAILED or not self._recovery_completed:
            return RouletteAuthority(
                allowed=False, reason_code=NOT_READY_REASON, scope=scope
            )
        if not enabled:
            return RouletteAuthority(
                allowed=False, reason_code=PLUGIN_DISABLED_REASON, scope=scope
            )
        try:
            outcome = self._admission(
                list(group_ids),
                intent=AdmissionIntent.BUSINESS,
            )
        except Exception:
            return RouletteAuthority(
                allowed=False,
                reason_code=ADMISSION_UNAVAILABLE_REASON,
                scope=scope,
            )
        if _qualifies_for_business(outcome):
            return RouletteAuthority(
                allowed=True, reason_code=POLICY_ADMITTED_REASON, scope=scope
            )
        return RouletteAuthority(
            allowed=False, reason_code=POLICY_RESTRICTED_REASON, scope=scope
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _close_impl(self) -> None:
        """Cancel the in-flight tick, then bounded-close the recovery port."""

        task = self._tick_task
        self._tick_task = None
        if task is not None:
            await _bounded_cancel(task)
        await self._close_recovery_bounded()

    async def _close_recovery_bounded(self) -> None:
        """Bound the recovery port's ``close`` even if it swallows cancellation."""

        try:
            close_coro = self._recovery.close()
        except Exception:
            return
        task = asyncio.ensure_future(close_coro)
        task.add_done_callback(_retrieve_background_error)
        self._recovery_close_task = task
        await _bounded_cancel(task)

    def _ensure_tick_task(self) -> asyncio.Task[Any]:
        """Return the in-flight tick, or dispatch exactly one new tick."""

        task = self._tick_task
        if task is None or task.done():
            task = asyncio.ensure_future(self._run_tick_guarded())
            self._tick_task = task
            task.add_done_callback(_retrieve_background_error)
        return task

    async def _run_tick_guarded(self) -> object:
        try:
            result = await self._recovery.run_recovery_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self._closed:
                self._set_failed(RECOVERY_FAILED_REASON)
            raise
        self._apply_tick_result(result)
        return result

    def _apply_tick_result(self, result: object) -> None:
        """Reconcile one settled tick; a late result after close is dropped."""

        if self._closed:
            return
        failures = _validated_failure_count(result)
        if failures is None or failures > 0:
            self._set_failed(RECOVERY_FAILED_REASON)
            return
        enabled = self._try_read_plugin_enable()
        if enabled is None:
            self._plugin_enable = False
            self._set_failed(CONFIG_UNAVAILABLE_REASON)
            return
        self._plugin_enable = enabled
        self._recovery_completed = True
        self._publish_lifecycle()

    def _publish_lifecycle(self) -> None:
        if self._closed:
            return
        if self._plugin_enable:
            self._status = RouletteRuntimeStatus.READY
            self._reason_code = None
        else:
            self._status = RouletteRuntimeStatus.DISABLED
            self._reason_code = PLUGIN_DISABLED_REASON

    def _refresh_safe_state(self) -> bool | None:
        """Apply a live read, downgrading on failure and **never** upgrading.

        A readable config alone never clears a recorded failure: only a clean
        recovery tick does that.  Returns the live ``plugin_enable`` or ``None``
        when the config cannot be trusted.
        """

        if self._closed:
            return None
        enabled = self._try_read_plugin_enable()
        if enabled is None:
            self._plugin_enable = False
            self._set_failed(CONFIG_UNAVAILABLE_REASON)
            return None
        self._plugin_enable = enabled
        return enabled

    def _set_failed(self, reason: str) -> None:
        self._status = RouletteRuntimeStatus.FAILED
        self._reason_code = reason
        self._recovery_completed = False

    def _try_read_plugin_enable(self) -> bool | None:
        """Read the live ``plugin_enable`` flag; ``None`` means untrustworthy.

        The real ``ConfigManager.get`` and the test double both expose this
        synchronous read.  Only an actual ``bool`` is accepted; a missing or
        malformed field fails closed rather than being coerced.
        """

        try:
            snapshot = self._config_manager.get()
        except Exception:
            return None
        return _snapshot_plugin_enable(snapshot)


def _qualifies_for_business(outcome: object) -> bool:
    qualification = getattr(outcome, "qualification", None)
    return qualification is AdmissionQualification.BUSINESS


__all__ = [
    "RUNTIME_REASON_CODES",
    "RouletteAuthority",
    "RouletteRuntime",
    "RouletteRuntimeState",
    "RouletteRuntimeStatus",
]
