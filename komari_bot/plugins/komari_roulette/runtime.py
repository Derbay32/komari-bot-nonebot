"""Roulette lifecycle / per-call authority runtime (TSK-279 Stage-B2).

The runtime is a deep module with a deliberately small interface:

* :meth:`RouletteRuntime.start` reads the persisted config once, publishes the
  lifecycle and *dispatches* one recovery tick, so authority is only ever
  released through a runtime that has already gone through startup;
* :meth:`RouletteRuntime.close` blocks every new dispatch *first*, then
  bounded-cancels the in-flight recovery tick, and never disposes the shared
  ``nonebot_plugin_orm`` engine (it does not own it);
* :meth:`RouletteRuntime.authorize` resolves admission from the *arguments of
  that call* (scope + group ids), never from a process-global "current event";
* the ``plugin_enable`` switch is re-read live on every authority check, so a
  persisted flip stops new business/send immediately without a restart, while
  maintenance keeps running on the absolute deadlines.

Recovery is single-flight: the startup tick, the scheduled tick and any caller
of :meth:`run_recovery_tick` all share **one** in-flight task, so two callers
can never advance the same deadline twice.  A tick that reports per-group
failures (or raises) downgrades the runtime to ``failed`` and withholds
business authority until a later clean tick.

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

from .reasons import RUNTIME_REASON_CODES

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: Bounded wait for an in-flight dispatch to honour a cancellation.
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


def _retrieve_background_error(task: asyncio.Task[object]) -> None:
    """Mark a fire-and-forget tick task's exception as retrieved.

    The startup dispatch does not await its tick; without this the event loop
    would log "Task exception was never retrieved" for a failing recovery while
    the state transition itself was already recorded.
    """

    with suppress(asyncio.CancelledError, Exception):
        task.exception()


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
        self._tick_task: asyncio.Task[object] | None = None
        self._start_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Read config once, dispatch one recovery tick, publish the lifecycle.

        A config failure is recorded as ``FAILED`` (never a silent fallback) and
        no tick is dispatched.  A valid config releases the startup barrier and
        dispatches the single-flight recovery tick; that tick's result is
        reconciled asynchronously and downgrades the runtime if it reports
        per-group failures.  ``CancelledError`` is a real cancellation and
        propagates.
        """

        async with self._start_lock:
            if self._closed:
                return
            try:
                snapshot = await self._config_manager.initialize_async()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._set_failed(CONFIG_UNAVAILABLE_REASON)
                return
            enabled = getattr(snapshot, "plugin_enable", None)
            if enabled is None:
                self._set_failed(CONFIG_UNAVAILABLE_REASON)
                return
            self._plugin_enable = bool(enabled)
            # Startup barrier: authority is released only for a runtime whose
            # startup has completed and whose recovery has been dispatched.
            self._recovery_completed = True
            self._publish_lifecycle()
            self._ensure_tick_task()
            # Let an immediately-settling tick (no internal awaits) reconcile
            # before ``start`` returns; a slow tick keeps running in the
            # background and settles through the same guarded path.
            await asyncio.sleep(0)

    async def close(self) -> None:
        """Block new dispatch, then bounded-cancel the in-flight tick.

        The closed flag is set *before* any wait so a task holding the runtime
        lock can never deadlock shutdown.  The shared ORM engine is owned by
        ``nonebot_plugin_orm`` and is never disposed from here.
        """

        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            task = self._tick_task
            self._tick_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=CLOSE_CANCEL_TIMEOUT_SECONDS)
        with suppress(Exception):
            await self._recovery.close()

    @property
    def accepting(self) -> bool:
        """Whether the runtime is alive, configured and switched on.

        The plugin switch is read live: a persisted ``True -> False`` flip stops
        business immediately.  A runtime that recorded a failed recovery is not
        accepting, and a runtime past ``close()`` is never accepting again.
        """

        if self._closed or self._status is RouletteRuntimeStatus.FAILED:
            return False
        return self._try_read_plugin_enable() is True

    def get_state(self) -> RouletteRuntimeState:
        """Return an immutable snapshot of the current lifecycle."""

        return RouletteRuntimeState(
            status=self._status,
            reason_code=self._reason_code,
            recovery_completed=self._recovery_completed,
            plugin_enable=self._try_read_plugin_enable() is True,
        )

    async def run_recovery_tick(self) -> RouletteRuntimeState:
        """Join (or start) the single-flight recovery tick and report state.

        When a tick is already in flight the caller joins that exact task, so
        two concurrent workers never advance one deadline twice.  A failed tick
        is re-raised after the failure has been recorded.
        """

        if self._closed:
            return self.get_state()
        await self._ensure_tick_task()
        return self.get_state()

    def authorize(  # noqa: PLR0911 - one guard per closed-set decision
        self,
        *,
        scope: str,
        group_ids: Sequence[int],
        token: Mapping[str, Any] | None = None,
    ) -> RouletteAuthority:
        """Adjudicate *this* call's scope/group ids; never a global event.

        ``token`` is accepted for the Stage-C token/binding translation and is
        not interpreted here.  The admission port is queried with the group ids
        of this call only, so two concurrent groups can never borrow each
        other's decision.
        """

        del token
        if self._closed:
            return RouletteAuthority(
                allowed=False, reason_code=NOT_READY_REASON, scope=scope
            )
        if scope not in _KNOWN_SCOPES:
            return RouletteAuthority(
                allowed=False, reason_code=POLICY_RESTRICTED_REASON, scope=scope
            )
        if (
            self._status is RouletteRuntimeStatus.FAILED
            or not self._recovery_completed
        ):
            return RouletteAuthority(
                allowed=False, reason_code=NOT_READY_REASON, scope=scope
            )
        enabled = self._try_read_plugin_enable()
        if enabled is None:
            return RouletteAuthority(
                allowed=False, reason_code=CONFIG_UNAVAILABLE_REASON, scope=scope
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

    def _ensure_tick_task(self) -> asyncio.Task[object]:
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
            self._set_failed(RECOVERY_FAILED_REASON)
            raise
        self._apply_tick_result(result)
        return result

    def _apply_tick_result(self, result: object) -> None:
        failed = _safe_count(getattr(result, "failed", 0))
        if failed > 0:
            self._set_failed(RECOVERY_FAILED_REASON)
            return
        enabled = self._try_read_plugin_enable()
        if enabled is None:
            self._set_failed(CONFIG_UNAVAILABLE_REASON)
            return
        self._plugin_enable = enabled
        self._recovery_completed = True
        self._publish_lifecycle()

    def _publish_lifecycle(self) -> None:
        if self._plugin_enable:
            self._status = RouletteRuntimeStatus.READY
            self._reason_code = None
        else:
            self._status = RouletteRuntimeStatus.DISABLED
            self._reason_code = PLUGIN_DISABLED_REASON

    def _set_failed(self, reason: str) -> None:
        self._status = RouletteRuntimeStatus.FAILED
        self._reason_code = reason
        self._recovery_completed = False

    def _try_read_plugin_enable(self) -> bool | None:
        """Read the live ``plugin_enable`` flag; ``None`` means unreadable.

        The real ``ConfigManager.get`` and the test double both expose this
        synchronous read, so no arbitrary-attribute fallback is used.
        """

        try:
            snapshot = self._config_manager.get()
        except Exception:
            return None
        value = getattr(snapshot, "plugin_enable", None)
        if value is None:
            return None
        return bool(value)


def _qualifies_for_business(outcome: object) -> bool:
    qualification = getattr(outcome, "qualification", None)
    return qualification is AdmissionQualification.BUSINESS


def _safe_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(value, 0)


__all__ = [
    "RUNTIME_REASON_CODES",
    "RouletteAuthority",
    "RouletteRuntime",
    "RouletteRuntimeState",
    "RouletteRuntimeStatus",
]
