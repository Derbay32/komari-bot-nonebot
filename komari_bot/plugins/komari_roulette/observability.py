"""TSK-279 Stage-B safe observability projection for the roulette workers.

The module deliberately exposes *one* fixed-key snapshot and *one* closed set of
fault reason codes.  It never renders raw exception text, group/member identity,
message bodies, credentials, hidden chambers or future rewards, it adds no
metrics endpoint and it never sends an active notification.

The snapshot answers four operational questions with low-cardinality values:

* is the runtime ready / disabled / failed, and why;
* what did the last recovery scan and retention cleanup do;
* how many receipts are still awaiting confirmation (never claimed/retried);
* how many faults were aggregated, grouped by fixed reason.

Faults are counted *per reason*, so a multi-line malicious exception collapses
into one record instead of one record per log line.
"""

# This module deliberately keeps its operator-facing errors short.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Protocol

from sqlalchemy import text

from .reasons import (
    FAULT_REASON_CODES,
    OBSERVATION_REASON_CODES,
    PENDING_UNAVAILABLE,
    RUNTIME_STATUS_VALUES,
    UNEXPECTED_FAULT_REASON,
    UNKNOWN_RUNTIME_REASON,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from .command_service import SessionFactory
    from .maintenance import CleanupResult, RecoveryTickResult


class PendingRefreshUnavailableError(RuntimeError):
    """``refresh_pending`` has no session factory to read the count from."""


class RuntimeStateLike(Protocol):
    """Structural view of the state published by the runtime owner.

    ``runtime.py`` is a Stage-B2 module, so importing it here would couple this
    projection to a module that does not exist yet.  The projection only renders
    these two read-only fields, and both are optional on purpose: an unknown or
    half-built state must project as ``None`` instead of inventing a status.
    """

    @property
    def status(self) -> object: ...

    @property
    def reason_code(self) -> str | None: ...


#: Both reason-code sets are declared once in ``reasons.py`` (single authority)
#: and re-exported here so operators keep importing this module.

#: Exact exception type names mapped to a fixed reason.  Matching is by class
#: name (not by rendered message), so no exception body can influence the code.
_KNOWN_FAULT_REASONS: dict[str, str] = {
    "StorageUnavailableError": "storage_unavailable",
    "DBAPIError": "storage_unavailable",
    "SQLAlchemyError": "storage_unavailable",
    "ConnectionError": "storage_unavailable",
    "OSError": "storage_unavailable",
    "TimeoutError": "storage_unavailable",
    "RevisionConflictError": "recovery_failed",
    "TerminalProjectionRejectedError": "recovery_failed",
    "AggregateCorruptError": "recovery_failed",
    "RuntimeError": "recovery_failed",
}

#: Receipt/fulfillment states that are still awaiting a platform confirmation.
_PENDING_FULFILLMENT_STATES: tuple[str, ...] = (
    "NOT_STARTED",
    "PENDING_CONFIRMATION",
)
_PENDING_RECEIPTS_SQL = (
    "SELECT count(*) FROM komari_roulette_fulfillments "
    "WHERE state IN ('NOT_STARTED', 'PENDING_CONFIRMATION')"
)

#: The process-wide runtime status is published once by the runtime owner.
class _RuntimeStateHolder:
    """One-slot holder so publishing never needs a ``global`` statement."""

    __slots__ = ("state",)

    def __init__(self) -> None:
        self.state: RuntimeStateLike | None = None


_runtime_holder = _RuntimeStateHolder()


def set_runtime_state(state: RuntimeStateLike) -> None:
    """Publish the latest runtime state for every future snapshot.

    The runtime is a process singleton, so a module-level holder is the
    narrowest seam that lets the composition root hand its state over without
    making observability depend on the runtime module at import time.
    """

    _runtime_holder.state = state


def _normalize_runtime_status(status: object) -> str | None:
    """Reduce a published status to a fixed lifecycle value (or ``None``)."""

    if status is None:
        return None
    code = str(getattr(status, "value", status))
    return code if code in RUNTIME_STATUS_VALUES else None


def _normalize_runtime_reason(reason: object) -> str | None:
    """Normalize a published reason into the shared closed set.

    A foreign or identity-bearing reason is never copied through: it collapses
    to :data:`UNKNOWN_RUNTIME_REASON`, which is itself a member of the shared
    closed set.
    """

    if reason is None:
        return None
    code = str(getattr(reason, "value", reason))
    if code in OBSERVATION_REASON_CODES:
        return code
    return UNKNOWN_RUNTIME_REASON


def _runtime_projection() -> tuple[str | None, str | None]:
    """Return ``(status, reason)`` as normalized JSON-safe strings."""

    state = _runtime_holder.state
    if state is None:
        return None, None
    return (
        _normalize_runtime_status(getattr(state, "status", None)),
        _normalize_runtime_reason(getattr(state, "reason_code", None)),
    )


def safe_fault_projection(error: BaseException) -> dict[str, str]:
    """Project a fault onto fixed ``error_type`` / ``reason_code`` strings only.

    The exception's message, arguments, notes and chained causes are never
    rendered: only the type name is used, and even that is reduced to the plain
    class name so a crafted message cannot smuggle identity, bodies, secrets,
    hidden chambers or future rewards into the observation.
    """

    error_type = type(error).__name__ or UNEXPECTED_FAULT_REASON
    reason_code = _KNOWN_FAULT_REASONS.get(error_type, UNEXPECTED_FAULT_REASON)
    return {"error_type": error_type, "reason_code": reason_code}


def _safe_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(value, 0)


@dataclass(frozen=True, slots=True)
class RouletteObservation:
    """Fixed-key, low-cardinality snapshot of the roulette workers.

    ``pending_receipts`` is ``None`` (not ``0``) while the count is unknown, so
    a failed pending query can never masquerade as "nothing is waiting".
    """

    #: Fixed key set; ``as_dict()`` yields exactly these keys one for one.
    FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "runtime_status",
            "runtime_reason",
            "latest_scan",
            "latest_cleanup",
            "pending_receipts",
            "fault_counts",
        }
    )

    runtime_status: str | None = None
    runtime_reason: str | None = None
    latest_scan: dict[str, int] | None = None
    latest_cleanup: dict[str, int | bool] | None = None
    pending_receipts: int | None = None
    fault_counts: tuple[tuple[str, int], ...] = ()

    def as_dict(self) -> dict[str, object]:
        """Return the snapshot as a JSON-safe dict with a fixed key set.

        Nested counter dicts are copied so mutating the returned projection can
        never rewrite the snapshot's own state.  Tuple counters (``fault_counts``)
        are rendered as JSON arrays so the REST status body is byte-identical to
        a round-tripped JSON payload instead of ``tuple`` vs ``list``.
        """

        projected: dict[str, object] = {}
        for name in sorted(self.FIELDS):
            value = getattr(self, name)
            if isinstance(value, dict):
                projected[name] = dict(value)
            elif isinstance(value, tuple):
                projected[name] = [
                    list(item) if isinstance(item, tuple) else item for item in value
                ]
            else:
                projected[name] = value
        return projected


@dataclass(slots=True)
class RouletteObservability:
    """Accumulate safe counters and project them into one snapshot."""

    session_factory: SessionFactory | None = None
    clock: Callable[[], object] | None = None
    _pending_receipts: int | None = None
    _latest_scan: dict[str, int] | None = None
    _latest_cleanup: dict[str, int | bool] | None = None
    _fault_counts: dict[str, int] = field(default_factory=dict)

    def note_scan(self, result: RecoveryTickResult) -> None:
        """Record the *safe* counters of one recovery scan.

        The scan cursor deliberately does not appear here: it is an internal
        pagination token and must never flow into a log line or the observation.
        """

        self._latest_scan = {
            "scanned": _safe_count(getattr(result, "scanned", None)),
            "advanced": _safe_count(getattr(result, "advanced", None)),
            "skipped_restricted": _safe_count(
                getattr(result, "skipped_restricted", None)
            ),
            "failed": _safe_count(getattr(result, "failed", None)),
        }

    def note_cleanup(self, result: CleanupResult) -> None:
        """Record the *safe* counters of one retention cleanup."""

        self._latest_cleanup = {
            "receipts_deleted": _safe_count(
                getattr(result, "receipts_deleted", None)
            ),
            "games_deleted": _safe_count(getattr(result, "games_deleted", None)),
            "results_deleted": _safe_count(
                getattr(result, "results_deleted", None)
            ),
            "more_pending": bool(getattr(result, "more_pending", False)),
        }

    def note_fault(self, error: BaseException) -> None:
        """Aggregate one fault under its fixed reason code."""

        self._note_fault_reason(safe_fault_projection(error)["reason_code"])

    def _note_fault_reason(self, reason: str) -> None:
        """Aggregate one fault under an explicit, already-closed reason code."""

        code = reason if reason in FAULT_REASON_CODES else UNEXPECTED_FAULT_REASON
        self._fault_counts[code] = self._fault_counts.get(code, 0) + 1

    async def refresh_pending(self) -> int:
        """Count receipts still awaiting confirmation; never claims or retries.

        A store failure keeps ``pending_receipts`` at ``None`` (unknown) and
        aggregates the fault under :data:`PENDING_UNAVAILABLE` before
        re-raising.  It is deliberately *not* routed through the generic
        ``RuntimeError -> recovery_failed`` map, so a failed observation read
        can never masquerade as a recovery failure.
        """

        if self.session_factory is None:
            self._pending_receipts = None
            self._note_fault_reason(PENDING_UNAVAILABLE)
            raise PendingRefreshUnavailableError
        try:
            async with self.session_factory() as session:
                value = await session.scalar(text(_PENDING_RECEIPTS_SQL))
        except Exception:
            self._pending_receipts = None
            self._note_fault_reason(PENDING_UNAVAILABLE)
            raise
        self._pending_receipts = _safe_count(value)
        return self._pending_receipts

    def snapshot(self) -> RouletteObservation:
        """Freeze the current counters into an immutable observation.

        The nested counter dicts are copied so mutating a snapshot (or the
        record passed to ``note_*``) can never rewrite the accumulated state.
        """

        runtime_status, runtime_reason = _runtime_projection()
        return RouletteObservation(
            runtime_status=runtime_status,
            runtime_reason=runtime_reason,
            latest_scan=(
                None if self._latest_scan is None else dict(self._latest_scan)
            ),
            latest_cleanup=(
                None
                if self._latest_cleanup is None
                else dict(self._latest_cleanup)
            ),
            pending_receipts=self._pending_receipts,
            fault_counts=tuple(sorted(self._fault_counts.items())),
        )


__all__ = [
    "FAULT_REASON_CODES",
    "OBSERVATION_REASON_CODES",
    "PENDING_UNAVAILABLE",
    "UNEXPECTED_FAULT_REASON",
    "UNKNOWN_RUNTIME_REASON",
    "PendingRefreshUnavailableError",
    "RouletteObservability",
    "RouletteObservation",
    "RuntimeStateLike",
    "safe_fault_projection",
    "set_runtime_state",
]
