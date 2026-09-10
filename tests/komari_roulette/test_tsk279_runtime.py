"""TSK-279 Stage-B RED: roulette runtime/lifecycle seam.

This file pins the **lifecycle** business ACs from TSK-269 Resolution §2 and
the per-call authority requirement: the runtime is a deep module with a small
interface (``start`` / ``close`` / ``get_state`` / ``authorize`` /
``run_recovery_tick``), and every admission/send decision is resolved **per
call from an explicit context** — never from a process-global "current event".

The production module ``komari_bot.plugins.komari_roulette.runtime`` does not
exist yet, so the lifecycle cases fail with ``ModuleNotFoundError`` (the
expected RED signal).  The two per-call gate cases run against the **real**
handler/delivery today and fail on the missing per-call interface (an
``AssertionError``), which is the separate "assertion" RED bucket.

Production composition (which real config manager / admission resolver /
scheduler is wired, and how a real token is verified) is **Stage-C**; the
contract marks those chains as unverified here.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from komari_bot.plugins.group_admission import (
    AdmissionIntent,
    AdmissionQualification,
    AdmissionResult,
)

from .command_support import PG_REQUIRED, seed_binding
from .test_command_service import create_waiting, service_for
from .tsk279_support import (
    RUNTIME_MODULE,
    Tsk279Harness,
    harness_fixture_body,
    load_symbol,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

pytestmark = [pytest.mark.asyncio]


def _runtime_api() -> dict[str, Any]:
    """Load the proposed runtime symbols lazily (missing module → clear RED)."""

    return {
        name: load_symbol(RUNTIME_MODULE, name)
        for name in (
            "RUNTIME_REASON_CODES",
            "RouletteAuthority",
            "RouletteRuntime",
            "RouletteRuntimeState",
            "RouletteRuntimeStatus",
        )
    }


# ---------------------------------------------------------------------------
# Test ports (explicitly not production authority)
# ---------------------------------------------------------------------------


class RecordingRecovery:
    """Observe recovery ordering; never decides admission or send authority."""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.calls = 0
        self.closed = False
        self._fail_times = fail_times

    async def run_recovery_tick(self) -> object:
        self.calls += 1
        if self.calls <= self._fail_times:
            message = "recovery tick failed (transient, TSK-279 test port)"
            raise RuntimeError(message)
        return object()

    async def close(self) -> None:
        self.closed = True


class RecordingConfig:
    """Minimal config port double for failure injection only."""

    def __init__(self, *, plugin_enable: bool = True, fails: bool = False) -> None:
        self.plugin_enable = plugin_enable
        self.fails = fails
        self.calls = 0

    async def initialize_async(self) -> Any:
        self.calls += 1
        if self.fails:
            message = "config storage unavailable (TSK-279 test port)"
            raise RuntimeError(message)
        return SimpleNamespace(plugin_enable=self.plugin_enable)


def _admission_by_group(allowed: set[int]) -> Any:
    """A per-call admission lookup returning real ``AdmissionResult`` values."""

    def lookup(
        associated_group_ids: Sequence[int],
        *,
        intent: AdmissionIntent,
    ) -> AdmissionResult:
        assert intent in {
            AdmissionIntent.BUSINESS,
            AdmissionIntent.FACT_FINALIZATION,
        }
        if any(group in allowed for group in associated_group_ids):
            return AdmissionResult(
                qualification=AdmissionQualification.BUSINESS,
                effective_revision=1,
                reason_code="policy_admitted",
            )
        return AdmissionResult(
            qualification=AdmissionQualification.REJECTED,
            effective_revision=1,
            reason_code="policy_restricted",
        )

    return lookup


def _allow_business(_bot: Any, _event: Any, _token: Any) -> bool:
    return True


# ---------------------------------------------------------------------------
# Lifecycle state machine
# ---------------------------------------------------------------------------


async def test_runtime_seam_exposes_small_lifecycle_interface() -> None:
    api = _runtime_api()
    assert isinstance(api["RUNTIME_REASON_CODES"], frozenset)
    # Low-cardinality, fixed reason codes only; no dynamic text / identity.
    assert 3 <= len(api["RUNTIME_REASON_CODES"]) <= 12
    assert all(
        isinstance(code, str) and " " not in code
        for code in api["RUNTIME_REASON_CODES"]
    )
    assert api["RouletteRuntimeStatus"].READY.value == "ready"
    assert api["RouletteRuntimeStatus"].DISABLED.value == "disabled"
    assert api["RouletteRuntimeStatus"].FAILED.value == "failed"


async def test_start_reaches_ready_only_after_recovery_tick() -> None:
    api = _runtime_api()
    config = RecordingConfig()
    recovery = RecordingRecovery()
    runtime = api["RouletteRuntime"](
        config_manager=config,
        recovery=recovery,
        admission=_admission_by_group({1}),
    )
    await runtime.start()
    state = runtime.get_state()
    assert config.calls == 1
    assert recovery.calls == 1
    assert state.recovery_completed is True
    assert state.status is api["RouletteRuntimeStatus"].READY
    assert runtime.accepting is True
    await runtime.close()


async def test_runtime_rejects_business_before_recovery_completes() -> None:
    api = _runtime_api()
    recovery = RecordingRecovery()
    runtime = api["RouletteRuntime"](
        config_manager=RecordingConfig(),
        recovery=recovery,
        admission=_admission_by_group({1}),
    )
    # Never started: no recovery has completed, so no business authority at all.
    denied = runtime.authorize(scope="business", group_ids=[1])
    assert denied.allowed is False
    assert denied.reason_code in api["RUNTIME_REASON_CODES"]
    assert recovery.calls == 0
    await runtime.start()
    allowed = runtime.authorize(scope="business", group_ids=[1])
    assert allowed.allowed is True
    await runtime.close()


async def test_config_unavailable_reports_failed_without_running_recovery() -> None:
    api = _runtime_api()
    recovery = RecordingRecovery()
    runtime = api["RouletteRuntime"](
        config_manager=RecordingConfig(fails=True),
        recovery=recovery,
        admission=_admission_by_group({1}),
    )
    await runtime.start()
    state = runtime.get_state()
    assert state.status is api["RouletteRuntimeStatus"].FAILED
    assert state.reason_code == "config_unavailable"
    assert state.recovery_completed is False
    assert recovery.calls == 0
    assert runtime.authorize(scope="business", group_ids=[1]).allowed is False
    await runtime.close()


async def test_plugin_disabled_states_disabled_but_recovery_still_runs() -> None:
    api = _runtime_api()
    recovery = RecordingRecovery()
    runtime = api["RouletteRuntime"](
        config_manager=RecordingConfig(plugin_enable=False),
        recovery=recovery,
        admission=_admission_by_group({1}),
    )
    await runtime.start()
    state = runtime.get_state()
    assert state.status is api["RouletteRuntimeStatus"].DISABLED
    assert state.plugin_enable is False
    # The switch pauses *business*, not the absolute deadlines: recovery ran.
    assert recovery.calls == 1
    assert state.recovery_completed is True
    denied = runtime.authorize(scope="business", group_ids=[1])
    assert denied.allowed is False
    assert denied.reason_code == "plugin_disabled"
    await runtime.close()


async def test_transient_recovery_failure_recovers_to_ready_next_tick() -> None:
    api = _runtime_api()
    recovery = RecordingRecovery(fail_times=1)
    runtime = api["RouletteRuntime"](
        config_manager=RecordingConfig(),
        recovery=recovery,
        admission=_admission_by_group({1}),
    )
    await runtime.start()
    assert runtime.get_state().status is api["RouletteRuntimeStatus"].FAILED
    assert runtime.get_state().reason_code == "recovery_failed"
    # Not permanently failed and no silent fallback: the next tick converges.
    await runtime.run_recovery_tick()
    assert runtime.get_state().status is api["RouletteRuntimeStatus"].READY
    assert runtime.accepting is True
    await runtime.close()


async def test_close_blocks_new_dispatch_and_never_disposes_shared_engine() -> None:
    api = _runtime_api()
    recovery = RecordingRecovery()
    runtime = api["RouletteRuntime"](
        config_manager=RecordingConfig(),
        recovery=recovery,
        admission=_admission_by_group({1}),
    )
    await runtime.start()
    assert runtime.accepting is True
    await runtime.close()
    assert runtime.accepting is False
    assert runtime.authorize(scope="business", group_ids=[1]).allowed is False
    assert recovery.closed is True

    from nonebot import require

    require("nonebot_plugin_orm")
    import nonebot_plugin_orm as orm_module

    engines_before = dict(getattr(orm_module, "_engines", {}))
    await runtime.close()
    assert dict(getattr(orm_module, "_engines", {})) == engines_before


# ---------------------------------------------------------------------------
# Per-call authority isolation (no process-global "current event")
# ---------------------------------------------------------------------------


async def test_authority_is_per_call_isolated_between_groups() -> None:
    api = _runtime_api()
    runtime = api["RouletteRuntime"](
        config_manager=RecordingConfig(),
        recovery=RecordingRecovery(),
        admission=_admission_by_group({101}),
    )
    await runtime.start()
    denied = runtime.authorize(scope="business", group_ids=[202])
    allowed = runtime.authorize(scope="business", group_ids=[101])
    assert denied.allowed is False
    assert denied.reason_code == "policy_restricted"
    assert allowed.allowed is True
    assert allowed.reason_code == "policy_admitted"

    # A second runtime whose admission revoked group 101 must not leak the
    # first runtime's allowed decision into its own per-call authority.
    runtime_revoked = api["RouletteRuntime"](
        config_manager=RecordingConfig(),
        recovery=RecordingRecovery(),
        admission=_admission_by_group(set()),
    )
    await runtime_revoked.start()
    still_allowed = runtime_revoked.authorize(scope="business", group_ids=[101])
    assert still_allowed.allowed is False
    assert runtime.authorize(scope="business", group_ids=[101]).allowed is True
    await runtime.close()
    await runtime_revoked.close()


# ---------------------------------------------------------------------------
# Explicit per-call interface evolution for the existing callbacks
# ---------------------------------------------------------------------------


async def test_handler_send_gate_receives_the_per_call_request() -> None:
    """The handler's send gate must be resolved per delivered command."""

    from komari_bot.plugins.komari_roulette.qq.handler import RouletteQQHandler

    from .tsk278_support import (
        FakeCommandService,
        FakeDelivery,
        FakeQQBot,
        admission_state,
        business_token,
        make_group_at_event,
        projection,
        receipt,
    )

    seen: list[Any] = []

    def send_gate(command_request: Any) -> bool:
        seen.append(command_request)
        return True

    service = FakeCommandService()
    service.receipt = receipt(reply=projection("> 测试正文。"))
    handler = RouletteQQHandler(
        service=service,
        delivery=FakeDelivery(),
        business_gate=_allow_business,
        send_gate=send_gate,
    )
    event = make_group_at_event("/轮盘 开枪")
    token = business_token()
    await handler.handle(FakeQQBot(), event, state=admission_state(token=token))
    assert [entry.inbound_msg_id for entry in seen] == ["msg-1"]
    assert [entry.group_openid for entry in seen] == [event.group_openid]


@pytest.fixture
async def harness() -> AsyncIterator[Tsk279Harness]:
    async for current in harness_fixture_body():
        yield current


@PG_REQUIRED
async def test_delivery_runtime_check_receives_the_per_call_receipt(
    harness: Tsk279Harness,
) -> None:
    """The delivery runtime recheck must see the receipt it is about to send."""

    from komari_bot.plugins.komari_roulette.qq.delivery import (
        DeliveryOutcome,
        RouletteDelivery,
    )

    from .test_command_service import CountingRandom

    class RecordingSender:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_to_group(
            self,
            group_openid: str,
            message: Any,
            *,
            msg_id: str | None = None,
            msg_seq: int | None = None,
        ) -> Any:
            del message, msg_id, msg_seq
            self.sent.append(group_openid)
            return SimpleNamespace(id=f"platform-{len(self.sent)}")

    service = service_for(harness, random_source=CountingRandom())
    sender = RecordingSender()
    async with AsyncExitStack() as stack:
        scopes = []
        receipts = []
        for tag in ("allowed", "revoked"):
            current = await stack.enter_async_context(
                harness.scope(f"percall-{tag}")
            )
            scopes.append(current)
            await seed_binding(harness.binding_manager, current, 1)
            receipts.append(
                await create_waiting(service, current, message_id=f"pc-{tag}")
            )

        revoked_group = scopes[1].group_openid

        def runtime_check(command_receipt: Any) -> bool:
            # Per-call decision: this receipt's own group decides, nothing global.
            return command_receipt.group_openid != revoked_group

        delivery = RouletteDelivery(
            service,
            runtime_check=runtime_check,
            payload_builder=lambda command_receipt: command_receipt.reply.body,
        )
        allowed, revoked = await asyncio.gather(
            delivery.deliver(receipts[0], sender),
            delivery.deliver(receipts[1], sender),
        )
    assert allowed is DeliveryOutcome.DELIVERED
    assert revoked is DeliveryOutcome.NOT_DELIVERED
    assert sender.sent == [scopes[0].group_openid]
