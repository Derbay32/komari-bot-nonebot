"""Hindsight shared-memory caller owned by the komari_memory plugin.

The module owns one shared bank for the whole process.  It never creates or
mutates a bank, never calls reflect, and never exposes the SDK client,
address, credential or bank selector outside this boundary.

Readiness is request driven: a successful bank configuration read plus a real
``arecall`` probe makes the current settings fingerprint ready.  A failure
starts a bounded recovery cooldown for that fingerprint.  Every new task
session still re-reads the bank so a stale readiness flag cannot hide a bank
that disappeared.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from hindsight_client import Hindsight, RecallResponse

from .hindsight_retain import (
    HindsightRetainRequest,
    derive_target_fingerprint,
    perform_retain,
)

HINDSIGHT_PROBE_QUERY = "记忆服务健康检查"
HINDSIGHT_PROBE_BUDGET = "low"
HINDSIGHT_PROBE_MAX_TOKENS = 1
HINDSIGHT_RECALL_BUDGET = "mid"
HINDSIGHT_RECALL_MAX_TOKENS = 4096
_CLOSE_TIMEOUT_SECONDS = 1.0
_EMPTY_RECALL_QUERY_ERROR = "recall query must be a non-empty string"
_RETAIN_TYPE_ERROR = "request must be a HindsightRetainRequest"
_RETAIN_FAILURES_WITHOUT_COOLDOWN: frozenset[str] = frozenset(
    {
        "document_conflict",
        "session_closed",
        "submission_not_admitted",
        "target_changed",
    }
)

HindsightFailureCode = Literal[
    "configuration_unavailable",
    "configuration_disabled",
    "configuration_incomplete",
    "configuration_invalid",
    "client_unavailable",
    "service_closed",
    "session_closed",
    "recovery_cooldown",
    "bank_unavailable",
    "bank_identity_mismatch",
    "recall_failed",
    "request_timeout",
    "response_invalid",
    "document_conflict",
    "document_read_failed",
    "retain_failed",
    "submission_not_admitted",
    "target_changed",
    "target_version_unsupported",
]

HINDSIGHT_FAILURE_CODES: frozenset[str] = frozenset(
    {
        "configuration_unavailable",
        "configuration_disabled",
        "configuration_incomplete",
        "configuration_invalid",
        "client_unavailable",
        "service_closed",
        "session_closed",
        "recovery_cooldown",
        "bank_unavailable",
        "bank_identity_mismatch",
        "recall_failed",
        "request_timeout",
        "response_invalid",
        "document_conflict",
        "document_read_failed",
        "retain_failed",
        "submission_not_admitted",
        "target_changed",
        "target_version_unsupported",
    }
)


class HindsightUnavailableError(RuntimeError):
    """A safe, fixed-code Hindsight availability failure.

    The error never carries an SDK exception as ``__cause__`` or
    ``__context__`` and never carries a response body.
    """

    def __init__(self, reason_code: str) -> None:
        code = (
            reason_code
            if reason_code in HINDSIGHT_FAILURE_CODES
            else "configuration_unavailable"
        )
        self.reason_code: HindsightFailureCode = code  # type: ignore[assignment]
        super().__init__(f"Hindsight unavailable: {code}")


ConfigGetter = Callable[[], Awaitable[object]]


@dataclass(frozen=True, slots=True)
class HindsightSettings:
    """One frozen task-session settings snapshot."""

    base_url: str = field(repr=False)
    api_key: str = field(repr=False)
    bank_id: str = field(repr=False)
    request_timeout_seconds: float
    recovery_interval_seconds: float

    @classmethod
    def from_config(cls, config: object) -> "HindsightSettings":
        """Validate and freeze the configured connection settings.

        Missing, disabled or invalid configuration raises a fixed safe code
        before any client is built.
        """
        if config is None:
            raise HindsightUnavailableError("configuration_unavailable")
        if not bool(getattr(config, "plugin_enable", False)):
            raise HindsightUnavailableError("configuration_disabled")

        base_url = getattr(config, "hindsight_base_url", None)
        api_key = getattr(config, "hindsight_api_key", None)
        bank_id = getattr(config, "hindsight_bank_id", None)
        if not (
            isinstance(base_url, str)
            and base_url.strip()
            and isinstance(api_key, str)
            and api_key
            and isinstance(bank_id, str)
            and bank_id.strip()
        ):
            raise HindsightUnavailableError("configuration_incomplete")

        normalized_base_url = base_url.strip().rstrip("/")
        normalized_bank_id = bank_id.strip()
        if not normalized_base_url.startswith(("http://", "https://")) or any(
            ord(character) < 32 for character in normalized_bank_id
        ):
            raise HindsightUnavailableError("configuration_invalid")

        timeout = getattr(config, "hindsight_request_timeout_seconds", None)
        recovery = getattr(config, "hindsight_recovery_interval_seconds", None)
        if not _valid_number(timeout, maximum=120.0) or not _valid_number(
            recovery,
            maximum=3600.0,
        ):
            raise HindsightUnavailableError("configuration_invalid")

        return cls(
            base_url=normalized_base_url,
            api_key=api_key,
            bank_id=normalized_bank_id,
            request_timeout_seconds=float(cast("int | float", timeout)),
            recovery_interval_seconds=float(cast("int | float", recovery)),
        )

    @property
    def fingerprint(self) -> str:
        """Stable fingerprint of every value that affects readiness."""
        material = "\x1f".join(
            (
                self.base_url,
                self.api_key,
                self.bank_id,
                repr(self.request_timeout_seconds),
                repr(self.recovery_interval_seconds),
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @property
    def target_fingerprint(self) -> str:
        """Stable identity of the normalized endpoint and bank only."""
        return derive_target_fingerprint(
            base_url=self.base_url,
            bank_id=self.bank_id,
        )


def _valid_number(value: object, *, maximum: float) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return 0.0 < float(value) <= maximum


def _build_client(settings: HindsightSettings) -> Hindsight:
    return Hindsight(
        base_url=settings.base_url,
        api_key=settings.api_key,
        timeout=settings.request_timeout_seconds,
        max_attempts=1,
    )


async def _close_client(client: Hindsight) -> None:
    """Close one owned client, bounded and idempotent at the SDK boundary."""
    try:
        await asyncio.wait_for(client.aclose(), timeout=_CLOSE_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception:
        # A close failure never masks the operation result and never exposes
        # the SDK exception.
        return


async def _verify_bank(client: Hindsight, settings: HindsightSettings) -> str | None:
    """Read bank configuration and require the exact configured bank ID."""
    response: Any = None
    failure: str | None = None
    try:
        async with asyncio.timeout(settings.request_timeout_seconds):
            response = await client.banks.get_bank_config(
                settings.bank_id,
                _request_timeout=settings.request_timeout_seconds,
            )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        failure = "request_timeout"
    except Exception:
        failure = "bank_unavailable"
    if failure is not None:
        return failure

    returned_bank_id = getattr(response, "bank_id", None)
    if returned_bank_id is None and isinstance(response, dict):
        returned_bank_id = response.get("bank_id")
    if not isinstance(returned_bank_id, str) or returned_bank_id != settings.bank_id:
        return "bank_identity_mismatch"
    return None


async def _recall(
    client: Hindsight,
    settings: HindsightSettings,
    *,
    query: str,
    budget: str,
    max_tokens: int,
) -> tuple[RecallResponse | None, str | None]:
    """Call one official recall and normalize every failure to a safe code."""
    response: Any = None
    failure: str | None = None
    try:
        async with asyncio.timeout(settings.request_timeout_seconds):
            response = await client.arecall(
                bank_id=settings.bank_id,
                query=query,
                budget=budget,
                max_tokens=max_tokens,
            )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        failure = "request_timeout"
    except Exception:
        failure = "recall_failed"
    if failure is not None:
        return None, failure
    if not isinstance(response, RecallResponse):
        return None, "response_invalid"
    return response, None


class HindsightRecallSession:
    """One reply task's owned client and frozen settings snapshot."""

    def __init__(
        self,
        *,
        settings: HindsightSettings,
        client: Hindsight,
        on_failure: Callable[[HindsightSettings, str], Awaitable[None]],
        on_close: Callable[["HindsightRecallSession"], Awaitable[None]],
    ) -> None:
        self._settings = settings
        self._client = client
        self._on_failure = on_failure
        self._on_close = on_close
        self._close_lock = asyncio.Lock()
        self._closed = False
        self._version_verified = False

    @property
    def target_fingerprint(self) -> str:
        """Read-only identity of this session's frozen target."""
        return self._settings.target_fingerprint

    async def verify_bank(self) -> str | None:
        """Check bank existence and exact identity for this task session."""
        failure = await _verify_bank(self._client, self._settings)
        if failure is not None:
            await self._on_failure(self._settings, failure)
        return failure

    async def recall(self, query: str) -> RecallResponse:
        """Recall the shared bank with the ordinary conversation budget."""
        if not isinstance(query, str) or not query.strip():
            raise ValueError(_EMPTY_RECALL_QUERY_ERROR)
        if self._closed:
            raise HindsightUnavailableError("session_closed")

        response, failure = await _recall(
            self._client,
            self._settings,
            query=query.strip(),
            budget=HINDSIGHT_RECALL_BUDGET,
            max_tokens=HINDSIGHT_RECALL_MAX_TOKENS,
        )
        if failure is not None:
            await self._on_failure(self._settings, failure)
            raise HindsightUnavailableError(failure)
        assert response is not None
        return response

    async def retain(
        self,
        request: HindsightRetainRequest,
        *,
        submission_guard: Callable[[], bool],
    ) -> None:
        """Submit one immutable async retain and require a valid receipt."""
        if self._closed:
            raise HindsightUnavailableError("session_closed")
        if not isinstance(request, HindsightRetainRequest):
            raise TypeError(_RETAIN_TYPE_ERROR)
        if request.target_fingerprint != self._settings.target_fingerprint:
            raise HindsightUnavailableError("target_changed")

        outcome = await perform_retain(
            self._client,
            bank_id=self._settings.bank_id,
            request=request,
            timeout_seconds=self._settings.request_timeout_seconds,
            version_verified=self._version_verified,
            submission_guard=submission_guard,
            is_session_open=lambda: not self._closed,
        )
        self._version_verified = outcome.version_verified
        failure = outcome.failure_code
        if failure is None:
            return
        if failure not in _RETAIN_FAILURES_WITHOUT_COOLDOWN:
            await self._on_failure(self._settings, failure)
        raise HindsightUnavailableError(failure)

    async def aclose(self) -> None:
        """Close this session's owned client exactly once."""
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
        await _close_client(self._client)
        await self._on_close(self)


class _HindsightSessionContext:
    """Project-owned async context manager for ``open_session``."""

    def __init__(
        self,
        service: "HindsightMemoryService",
        *,
        expected_target_fingerprint: str | None,
    ) -> None:
        self._service = service
        self._expected_target_fingerprint = expected_target_fingerprint
        self._session: HindsightRecallSession | None = None

    async def __aenter__(self) -> HindsightRecallSession:
        try:
            self._session = await self._service._open_session(
                expected_target_fingerprint=self._expected_target_fingerprint
            )
        except BaseException:
            session = self._session
            if session is not None:
                await session.aclose()
            raise
        return self._session

    async def __aexit__(
        self,
        _exc_type: object,
        _exc: object,
        _traceback: object,
    ) -> None:
        session = self._session
        self._session = None
        if session is not None:
            await session.aclose()


class HindsightMemoryService:
    """Stable runtime object owned by the komari_memory plugin lifecycle."""

    def __init__(self, *, config_getter: ConfigGetter) -> None:
        self._config_getter = config_getter
        self._state_lock = asyncio.Lock()
        self._closed = False
        self._active_sessions: set[HindsightRecallSession] = set()
        self._probe_task: asyncio.Task[None] | None = None
        self._probe_fingerprint: str | None = None
        self._probe_tasks: set[asyncio.Task[None]] = set()
        self._current_fingerprint: str | None = None
        self._readiness_fingerprint: str | None = None
        self._ready = False
        self._failure_code: str | None = None
        self._next_retry_at = 0.0

    def open_session(
        self,
        *,
        expected_target_fingerprint: str | None = None,
    ) -> _HindsightSessionContext:
        """Return one reply task's async context manager."""
        return _HindsightSessionContext(
            self,
            expected_target_fingerprint=expected_target_fingerprint,
        )

    async def startup_probe(self) -> None:
        """Attempt one bounded startup probe and isolate every failure."""
        try:
            settings = await self._load_settings()
            await self._ensure_ready(settings)
        except asyncio.CancelledError:
            raise
        except HindsightUnavailableError:
            return

    async def close(self) -> None:
        """Prevent new work and close every owned active session."""
        async with self._state_lock:
            if self._closed:
                return
            self._closed = True
            probe_tasks = [
                task
                for task in (self._probe_task, *self._probe_tasks)
                if task is not None and not task.done()
            ]
            self._probe_task = None
            self._probe_fingerprint = None
            self._probe_tasks.clear()
            sessions = list(self._active_sessions)
            self._active_sessions.clear()

        for task in probe_tasks:
            task.cancel()
        for task in probe_tasks:
            with suppress(asyncio.CancelledError):
                await task

        if sessions:
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(
                        *(session.aclose() for session in sessions),
                        return_exceptions=True,
                    ),
                    timeout=_CLOSE_TIMEOUT_SECONDS,
                )

    async def _load_settings(self) -> HindsightSettings:
        config: object = None
        failure = False
        try:
            config = await self._config_getter()
        except asyncio.CancelledError:
            raise
        except Exception:
            failure = True
        if failure:
            raise HindsightUnavailableError("configuration_unavailable")
        return HindsightSettings.from_config(config)

    async def _open_session(
        self,
        *,
        expected_target_fingerprint: str | None = None,
    ) -> HindsightRecallSession:
        async with self._state_lock:
            if self._closed:
                raise HindsightUnavailableError("service_closed")

        settings = await self._load_settings()
        if (
            expected_target_fingerprint is not None
            and settings.target_fingerprint != expected_target_fingerprint
        ):
            raise HindsightUnavailableError("target_changed")
        await self._ensure_ready(settings)

        client: Hindsight | None = None
        construction_failed = False
        try:
            client = _build_client(settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            construction_failed = True
        if construction_failed or client is None:
            await self._note_failure(settings, "client_unavailable")
            raise HindsightUnavailableError("client_unavailable")

        session = HindsightRecallSession(
            settings=settings,
            client=client,
            on_failure=self._note_failure,
            on_close=self._unregister_session,
        )

        try:
            # Own the client before any cancellable verification await so that
            # cancellation and shutdown both close it exactly once.
            async with self._state_lock:
                if self._closed:
                    raise HindsightUnavailableError("service_closed")  # noqa: TRY301
                self._active_sessions.add(session)

            failure = await session.verify_bank()
            if failure is not None:
                raise HindsightUnavailableError(failure)  # noqa: TRY301

            # Shutdown may have won the race while the bank check was waiting.
            async with self._state_lock:
                shutdown_won = self._closed or session not in self._active_sessions
            if shutdown_won:
                raise HindsightUnavailableError("service_closed")  # noqa: TRY301
            return session  # noqa: TRY300
        except asyncio.CancelledError:
            await session.aclose()
            raise
        except BaseException:
            await session.aclose()
            raise

    async def _unregister_session(self, session: HindsightRecallSession) -> None:
        async with self._state_lock:
            self._active_sessions.discard(session)

    async def _note_failure(
        self,
        settings: HindsightSettings,
        reason_code: str,
    ) -> None:
        async with self._state_lock:
            if self._closed or self._current_fingerprint != settings.fingerprint:
                return
            self._readiness_fingerprint = settings.fingerprint
            self._ready = False
            self._failure_code = reason_code
            self._next_retry_at = time.monotonic() + settings.recovery_interval_seconds

    async def _ensure_ready(self, settings: HindsightSettings) -> None:
        fingerprint = settings.fingerprint
        while True:
            async with self._state_lock:
                if self._closed:
                    raise HindsightUnavailableError("service_closed")
                self._current_fingerprint = fingerprint
                if self._readiness_fingerprint != fingerprint:
                    self._readiness_fingerprint = fingerprint
                    self._ready = False
                    self._failure_code = None
                    self._next_retry_at = 0.0
                if self._ready:
                    return
                if (
                    self._failure_code is not None
                    and time.monotonic() < self._next_retry_at
                ):
                    raise HindsightUnavailableError("recovery_cooldown")
                task = self._probe_task
                if (
                    task is None
                    or task.done()
                    or self._probe_fingerprint != fingerprint
                ):
                    task = asyncio.create_task(self._run_probe(settings))
                    self._probe_task = task
                    self._probe_fingerprint = fingerprint
                    self._probe_tasks.add(task)
                    task.add_done_callback(self._probe_tasks.discard)

            await asyncio.shield(task)

            async with self._state_lock:
                if self._closed:
                    raise HindsightUnavailableError("service_closed")
                if self._readiness_fingerprint == fingerprint and self._ready:
                    return
                code = (
                    self._failure_code
                    if self._readiness_fingerprint == fingerprint
                    else None
                )
            raise HindsightUnavailableError(code or "recall_failed")

    async def _run_probe(self, settings: HindsightSettings) -> None:
        failure: str | None = None
        client: Hindsight | None = None
        try:
            client = _build_client(settings)
            failure = await _verify_bank(client, settings)
            if failure is None:
                _response, failure = await _recall(
                    client,
                    settings,
                    query=HINDSIGHT_PROBE_QUERY,
                    budget=HINDSIGHT_PROBE_BUDGET,
                    max_tokens=HINDSIGHT_PROBE_MAX_TOKENS,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            failure = "client_unavailable"
        finally:
            if client is not None:
                await _close_client(client)

        async with self._state_lock:
            if self._readiness_fingerprint != settings.fingerprint:
                return
            if failure is None:
                self._ready = True
                self._failure_code = None
                self._next_retry_at = 0.0
            else:
                self._ready = False
                self._failure_code = failure
                self._next_retry_at = (
                    time.monotonic() + settings.recovery_interval_seconds
                )


__all__ = [
    "HINDSIGHT_PROBE_BUDGET",
    "HINDSIGHT_PROBE_MAX_TOKENS",
    "HINDSIGHT_PROBE_QUERY",
    "HINDSIGHT_RECALL_BUDGET",
    "HINDSIGHT_RECALL_MAX_TOKENS",
    "HindsightFailureCode",
    "HindsightMemoryService",
    "HindsightRecallSession",
    "HindsightSettings",
    "HindsightUnavailableError",
]
