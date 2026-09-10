"""TSK-279 Stage-B: safe observability projection and fault redaction.

GREEN probe: the existing ``_safe_details`` allow-list already drops every
identity / body / secret / hidden-chamber / future-reward key from reply
details, so the new observability layer can reuse that discipline.

RED: the missing ``komari_bot.plugins.komari_roulette.observability`` seam must
expose a fixed-key, low-cardinality projection with closed reason codes, a
bounded pending count, and a fault redactor that never leaks a malicious
exception's message.  Those cases fail with ``ModuleNotFoundError``.
"""

from __future__ import annotations

import json
from enum import StrEnum
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Self
from uuid import uuid4

import pytest

from .command_support import PG_REQUIRED
from .tsk279_support import (
    MAINTENANCE_MODULE,
    OBSERVABILITY_MODULE,
    Tsk279Harness,
    harness_fixture_body,
    load_module,
    load_symbol,
    seed_aged_receipt,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_LEAKY_TOKENS = (
    "SECRET-OPENID-123",
    "SECRET-BODY-456",
    "SECRET-KEY-789",
    "SECRET-CHAMBER",
    "SECRET-REWARD",
)


def _observability_api() -> dict[str, Any]:
    return {
        name: load_symbol(OBSERVABILITY_MODULE, name)
        for name in (
            "OBSERVATION_REASON_CODES",
            "RouletteObservation",
            "RouletteObservability",
            "safe_fault_projection",
            "set_runtime_state",
        )
    }


class _Status(StrEnum):
    """Stand-in for the runtime status enum (observability must not import it)."""

    READY = "ready"
    DISABLED = "disabled"
    FAILED = "failed"


#: Legal runtime reason codes that must stay meaningful *and* live in the same
#: shared closed set the fault reasons come from (no duplicated drifting set).
_LEGAL_RUNTIME_REASONS: tuple[str, ...] = (
    "plugin_disabled",
    "policy_restricted",
    "policy_admitted",
    "not_ready",
)


def _state(status: _Status, reason: str | None) -> SimpleNamespace:
    """A duck-typed runtime state: the observability seam must not import runtime."""

    return SimpleNamespace(status=status, reason_code=reason)


def _neutral_state() -> SimpleNamespace:
    """Restore value: valid, low-cardinality, never a raw payload."""

    return _state(_Status.DISABLED, "plugin_disabled")


def _try_mutate(container: Any, key: str, value: Any) -> None:
    """Mutate a projected dict, accepting a read-only mapping as protection."""

    try:
        container[key] = value
    except TypeError:
        # A read-only projection is an acceptable way to keep the copy isolated.
        return


class _FailingReadSession:
    """A real ``AsyncSession`` proxy whose pending read raises on demand."""

    def __init__(self, session: Any, error: BaseException) -> None:
        self._session = session
        self._error = error

    async def __aenter__(self) -> Self:
        await self._session.__aenter__()
        return self

    async def __aexit__(self, *exc_info: object) -> Any:
        return await self._session.__aexit__(*exc_info)

    async def scalar(self, *_args: Any, **_kwargs: Any) -> Any:
        raise self._error


class _PendingReadFactory:
    """A real session factory that succeeds, then raises on the pending read.

    The first call is delegated to the genuine ``async_sessionmaker`` (a real
    PostgreSQL read), so the success count is real.  Every later call wraps a
    real session too and fails inside ``scalar`` -- never with a fake ``0``.
    """

    def __init__(
        self,
        real_factory: Any,
        error: BaseException,
        *,
        fail_after: int = 1,
    ) -> None:
        self._real_factory = real_factory
        self._error = error
        self._fail_after = fail_after
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        session = self._real_factory()
        if self.calls > self._fail_after:
            return _FailingReadSession(session, self._error)
        return session


@pytest.fixture
async def harness() -> AsyncIterator[Tsk279Harness]:
    async for current in harness_fixture_body():
        yield current


def _leaky_round_message() -> str:
    return (
        f"member_openid={_LEAKY_TOKENS[0]} body={_LEAKY_TOKENS[1]} "
        f"api_key={_LEAKY_TOKENS[2]} chamber={_LEAKY_TOKENS[3]} "
        f"reward={_LEAKY_TOKENS[4]}\nline-2\nline-3"
    )


# ---------------------------------------------------------------------------
# GREEN probe: the real reply-detail redactor already drops the sensitive keys
# ---------------------------------------------------------------------------


def test_existing_reply_details_redaction_drops_hidden_and_secret_keys() -> None:
    service_module = load_module("komari_bot.plugins.komari_roulette.command_service")
    redact = service_module._safe_details
    safe = redact(
        {
            "member_openid": _LEAKY_TOKENS[0],
            "raw_message": _LEAKY_TOKENS[1],
            "api_key": _LEAKY_TOKENS[2],
            "ordered_chamber": (_LEAKY_TOKENS[3], _LEAKY_TOKENS[3]),
            "future_rewards": (_LEAKY_TOKENS[4],),
            "reason": "forfeited",
            "remaining_live": 1,
            "rewards": ("magnifier",),
        }
    )
    assert set(safe) == {"reason", "remaining_live", "rewards"}
    rendered = json.dumps(safe, ensure_ascii=False)
    for token in _LEAKY_TOKENS:
        assert token not in rendered


# ---------------------------------------------------------------------------
# RED: fixed-key projection, closed reason codes and bounded pending count
# ---------------------------------------------------------------------------


def test_observability_seam_exposes_fixed_projection() -> None:
    api = _observability_api()
    assert api["RouletteObservation"] is not None
    assert api["OBSERVATION_REASON_CODES"]
    assert isinstance(api["OBSERVATION_REASON_CODES"], frozenset)


def test_fault_projection_strips_a_malicious_exception() -> None:
    api = _observability_api()
    error = RuntimeError(_leaky_round_message())
    projection = api["safe_fault_projection"](error)
    rendered = json.dumps(projection, ensure_ascii=False)
    for token in _LEAKY_TOKENS:
        assert token not in rendered
    assert projection["error_type"] == "RuntimeError"
    assert projection["reason_code"] in api["OBSERVATION_REASON_CODES"]


def test_fault_projection_collapses_multiline_payload_to_one_record() -> None:
    api = _observability_api()
    observability = api["RouletteObservability"]()
    for _ in range(2):
        observability.note_fault(RuntimeError(_leaky_round_message()))
    observation = observability.snapshot()
    # One aggregated record per fixed reason, never one record per log line.
    assert sum(count for _reason, count in observation.fault_counts) == 2
    assert "\n" not in json.dumps(observation.as_dict(), ensure_ascii=False)
    assert len(observation.as_dict()) == len(api["RouletteObservation"].FIELDS)


@PG_REQUIRED
async def test_pending_count_reflects_real_receipts(
    harness: Tsk279Harness,
) -> None:
    api = _observability_api()
    async with harness.scope("observe-pending") as current:
        for index in range(2):
            await seed_aged_receipt(
                harness.session_factory,
                current,
                inbound_msg_id=f"observe-{index}-{uuid4().hex}",
                age_seconds=30,
            )
        observability = api["RouletteObservability"](
            session_factory=harness.session_factory
        )
        pending = await observability.refresh_pending()
        assert pending >= 2
        assert isinstance(observability.snapshot().pending_receipts, int)


# ---------------------------------------------------------------------------
# RED: runtime reason normalization, unknown pending faults, mutable exposure
# ---------------------------------------------------------------------------


def test_runtime_reason_is_normalized_into_the_shared_closed_set() -> None:
    """A foreign ``reason_code`` must never reach the projection verbatim.

    Legal runtime reasons keep their meaning, every one of them lives in the
    same closed set the fault reasons use (so the two definitions cannot drift
    apart), and an unknown/identity-bearing reason is normalized to a member of
    that set instead of being copied into ``as_dict()`` raw.
    """

    api = _observability_api()
    codes = api["OBSERVATION_REASON_CODES"]
    assert isinstance(codes, frozenset)
    for code in _LEGAL_RUNTIME_REASONS:
        assert code in codes, (
            "the shared closed set must also carry the legal runtime reason "
            "codes, not a second drifting set"
        )
    observability = api["RouletteObservability"]()
    try:
        for status_value, reason in (
            ("ready", "not_ready"),
            ("disabled", "plugin_disabled"),
            ("failed", "recovery_failed"),
        ):
            api["set_runtime_state"](_state(_Status(status_value), reason))
            observation = observability.snapshot()
            assert observation.runtime_status == status_value
            assert observation.runtime_reason == reason
            assert reason in codes

        leaky_reason = (
            f"member_openid={_LEAKY_TOKENS[0]} body={_LEAKY_TOKENS[1]}"
        )
        api["set_runtime_state"](_state(_Status.READY, leaky_reason))
        observation = observability.snapshot()
        assert observation.runtime_reason in codes
        assert observation.runtime_reason != leaky_reason
        rendered = json.dumps(observation.as_dict(), ensure_ascii=False)
        for token in _LEAKY_TOKENS:
            assert token not in rendered
    finally:
        api["set_runtime_state"](_neutral_state())


@PG_REQUIRED
async def test_pending_read_failure_is_unknown_pending_unavailable_and_never_leaks(
    harness: Tsk279Harness,
) -> None:
    """A failed pending read is *unknown*, never a fake ``0`` and never
    mis-bucketed as ``recovery_failed``.

    The first refresh uses the genuine ``async_sessionmaker`` (real count), the
    second fails inside a real session's ``scalar`` with a leaky
    ``RuntimeError``.  The snapshot must keep ``pending_receipts`` at ``None``,
    aggregate the fault under ``pending_unavailable``, and never project the
    raw exception message.
    """

    api = _observability_api()
    async with harness.scope("observe-pending-fault") as current:
        await seed_aged_receipt(
            harness.session_factory,
            current,
            inbound_msg_id=f"observe-fault-{uuid4().hex}",
            age_seconds=30,
        )
        leaky = RuntimeError(_leaky_round_message())
        factory = _PendingReadFactory(harness.session_factory, leaky, fail_after=1)
        observability = api["RouletteObservability"](session_factory=factory)

        first = await observability.refresh_pending()
        assert first >= 1
        assert observability.snapshot().pending_receipts == first

        with pytest.raises(RuntimeError):
            await observability.refresh_pending()
        assert factory.calls == 2

        observation = observability.snapshot()
        # Unknown, not ``0`` and not the stale success count.
        assert observation.pending_receipts is None
        reasons = dict(observation.fault_counts)
        assert reasons.get("pending_unavailable") == 1
        assert "recovery_failed" not in reasons
        rendered = json.dumps(observation.as_dict(), ensure_ascii=False)
        for token in _LEAKY_TOKENS:
            assert token not in rendered


def test_snapshot_projection_isolates_internal_mutable_dicts() -> None:
    """Mutating a projection (or the record passed to a note_*) must not leak
    into a later snapshot.

    A read-only projection (``TypeError`` on write) is accepted, as is an
    independent copy; the concrete wrapper type is deliberately not pinned.
    """

    api = _observability_api()
    recovery_tick_result = load_symbol(MAINTENANCE_MODULE, "RecoveryTickResult")
    cleanup_result = load_symbol(MAINTENANCE_MODULE, "CleanupResult")
    observability = api["RouletteObservability"]()
    observability.note_scan(
        recovery_tick_result(
            scanned=3,
            advanced=1,
            skipped_restricted=2,
            failed=0,
            cursor="opaque-cursor-token",
        )
    )
    observability.note_cleanup(
        cleanup_result(
            receipts_deleted=7,
            games_deleted=2,
            results_deleted=2,
            more_pending=True,
        )
    )

    observation = observability.snapshot()
    _try_mutate(observation.latest_scan, "scanned", 999)
    _try_mutate(observation.latest_cleanup, "receipts_deleted", 999)
    projected = observation.as_dict()
    _try_mutate(projected["latest_scan"], "advanced", 999)
    _try_mutate(projected["latest_cleanup"], "games_deleted", 999)

    fresh = observability.snapshot()
    assert fresh.latest_scan == {
        "scanned": 3,
        "advanced": 1,
        "skipped_restricted": 2,
        "failed": 0,
    }
    assert fresh.latest_cleanup == {
        "receipts_deleted": 7,
        "games_deleted": 2,
        "results_deleted": 2,
        "more_pending": True,
    }

    # Mutating the record passed to ``note_*`` (structurally typed) must not
    # retro-actively rewrite the recorded counters either.
    mutable_scan = SimpleNamespace(
        scanned=11,
        advanced=0,
        skipped_restricted=0,
        failed=0,
        cursor=None,
    )
    observability.note_scan(mutable_scan)
    mutable_scan.scanned = 999
    assert observability.snapshot().latest_scan["scanned"] == 11
