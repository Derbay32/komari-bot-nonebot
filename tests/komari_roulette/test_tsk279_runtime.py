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
from contextlib import AsyncExitStack, suppress
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest
from sqlalchemy import text

from komari_bot.plugins.group_admission import (
    AdmissionIntent,
    AdmissionQualification,
    AdmissionResult,
)

from .command_support import (
    PG_REQUIRED,
    command_factory,
    observation,
    request,
    seed_binding,
)
from .test_command_service import (
    CountingRandom,
    create_waiting,
    current_game_row,
    join_player,
    seed_players,
    service_for,
    start_game,
)
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
    """Observe recovery ordering; never decides admission or send authority.

    ``fail`` is a live switch: a test can force the *next* tick to fail without
    rebuilding the runtime, which is how the "recover before ready" contract is
    driven.
    """

    def __init__(self, *, fail_times: int = 0) -> None:
        self.calls = 0
        self.closed = False
        self.fail = False
        self._fail_times = fail_times

    async def run_recovery_tick(self) -> object:
        self.calls += 1
        if self.fail or self.calls <= self._fail_times:
            message = "recovery tick failed (transient, TSK-279 test port)"
            raise RuntimeError(message)
        return object()

    async def close(self) -> None:
        self.closed = True


class BlockingRecovery:
    """A recovery port whose tick blocks until it is cancelled.

    Used to prove ``close()`` bounded-cancels an in-flight dispatch instead of
    hanging the shutdown path, and records a *real* cancellation rather than a
    boolean a test sets by hand.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.cancelled = 0
        self.finished = 0
        self.closed = False

    async def run_recovery_tick(self) -> object:
        self.calls += 1
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        self.finished += 1
        return object()

    async def close(self) -> None:
        self.closed = True


class RecordingConfig:
    """Config port double mirroring the real ``ConfigManager`` read API.

    ``initialize_async`` is the start snapshot; ``get`` / ``get_async`` are the
    dynamic per-call re-reads.  A live flip only needs those reads, so the
    double must expose them instead of forcing an implementation to cache the
    initialization snapshot forever.
    """

    def __init__(self, *, plugin_enable: bool = True, fails: bool = False) -> None:
        self.plugin_enable = plugin_enable
        self.fails = fails
        self.calls = 0
        self.read_calls = 0

    def _snapshot(self) -> Any:
        if self.fails:
            message = "config storage unavailable (TSK-279 test port)"
            raise RuntimeError(message)
        return SimpleNamespace(plugin_enable=self.plugin_enable)

    async def initialize_async(self) -> Any:
        self.calls += 1
        return self._snapshot()

    def get(self) -> Any:
        self.read_calls += 1
        return self._snapshot()

    async def get_async(self) -> Any:
        self.read_calls += 1
        return self._snapshot()


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


class _PerRequestService:
    """A service that returns one distinct receipt per request.

    The existing ``FakeCommandService`` returns a single configured receipt, so
    it cannot prove two concurrent requests keep their own identity.  This port
    derives the receipt from the request itself (app/group/message id) so a
    cross-group leak would be visible in the delivered payload.
    """

    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def observe_current(self, group: Any) -> Any:
        del group
        return None

    async def execute_group_command(
        self,
        request: Any,
        *,
        observation: Any = None,
    ) -> Any:
        del observation
        from .tsk278_support import projection, receipt

        self.requests.append(request)
        return receipt(
            receipt_id=f"r-{request.inbound_msg_id}",
            app_id=request.app_id,
            group_openid=request.group_openid,
            inbound_msg_id=request.inbound_msg_id,
            reply=projection("> 测试正文。"),
        )


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


async def test_dynamic_disable_flips_live_without_restart_or_tick() -> None:
    """The plugin switch is re-read per call, not frozen at ``start()``."""

    api = _runtime_api()
    config = RecordingConfig(plugin_enable=True)
    recovery = RecordingRecovery()
    runtime = api["RouletteRuntime"](
        config_manager=config,
        recovery=recovery,
        admission=_admission_by_group({1}),
    )
    await runtime.start()
    assert runtime.authorize(scope="business", group_ids=[1]).allowed is True
    assert runtime.authorize(scope="send", group_ids=[1]).allowed is True

    # Live flip down: the same runtime must reject business/send immediately,
    # without a restart and without waiting for the next 60s recovery sweep.
    config.plugin_enable = False
    assert runtime.accepting is False
    denied = runtime.authorize(scope="business", group_ids=[1])
    assert denied.allowed is False
    assert denied.reason_code == "plugin_disabled"
    assert runtime.authorize(scope="send", group_ids=[1]).allowed is False
    # Reacting to the flip must not dispatch a recovery tick by itself.
    assert recovery.calls == 1

    # Flipping back on while recovery is failing must not short-circuit READY:
    # only a successful recovery tick releases business again.
    recovery.fail = True
    with suppress(RuntimeError):
        await runtime.run_recovery_tick()
    config.plugin_enable = True
    assert runtime.accepting is False
    assert runtime.authorize(scope="business", group_ids=[1]).allowed is False

    recovery.fail = False
    await runtime.run_recovery_tick()
    assert runtime.accepting is True
    assert runtime.authorize(scope="business", group_ids=[1]).allowed is True
    await runtime.close()


@PG_REQUIRED
async def test_close_blocks_new_dispatch_and_never_disposes_shared_engine() -> None:
    """Shutdown must bounded-cancel in-flight dispatch and keep the ORM engine."""

    api = _runtime_api()

    from nonebot import require

    require("nonebot_plugin_orm")
    import nonebot_plugin_orm as orm_module
    from sqlalchemy.ext.asyncio import AsyncEngine

    # Hold the *real* shared factory and force the shared engine to exist, so
    # the dispose probe observes the process-wide engine rather than a private
    # mapping snapshot (and never a test-nailed global).
    shared_factory = orm_module.get_session
    probe_session = shared_factory()
    try:
        assert await probe_session.scalar(text("SELECT 1")) == 1
    finally:
        await probe_session.close()
    shared_engine = getattr(orm_module, "_engines", {}).get("")
    if shared_engine is None:
        message = "nonebot_plugin_orm has no default shared engine"
        raise AssertionError(message)
    assert isinstance(shared_engine, AsyncEngine)

    recovery = BlockingRecovery()
    runtime = api["RouletteRuntime"](
        config_manager=RecordingConfig(),
        recovery=recovery,
        admission=_admission_by_group({1}),
    )
    await runtime.start()
    assert runtime.accepting is True

    dispose_calls: list[int] = []
    original_dispose = AsyncEngine.dispose

    async def spy_dispose(
        engine_self: AsyncEngine, *args: Any, **kwargs: Any
    ) -> None:
        dispose_calls.append(id(engine_self))
        await original_dispose(engine_self, *args, **kwargs)

    blocked = asyncio.create_task(runtime.run_recovery_tick())
    try:
        await asyncio.wait_for(recovery.started.wait(), timeout=2)
        with patch.object(AsyncEngine, "dispose", spy_dispose):
            # A real in-flight, never-finishing recovery tick must not hang the
            # shutdown: close cancels it with a bound.
            await asyncio.wait_for(runtime.close(), timeout=2)
            assert runtime.accepting is False
            assert runtime.authorize(scope="business", group_ids=[1]).allowed is False
            assert runtime.authorize(scope="send", group_ids=[1]).allowed is False
            # Idempotent second close must still never dispose the shared engine.
            await asyncio.wait_for(runtime.close(), timeout=2)
        assert dispose_calls == []
        # The blocked dispatch was really cancelled and nobody finished it.
        assert recovery.cancelled == 1
        assert recovery.finished == 0
        # No new dispatch after close, even for the same recovery port.
        calls_after_close = recovery.calls
        await runtime.run_recovery_tick()
        assert recovery.calls == calls_after_close
    finally:
        blocked.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await blocked


# ---------------------------------------------------------------------------
# Per-call authority isolation (no process-global "current event")
# ---------------------------------------------------------------------------


async def test_authority_is_per_call_isolated_between_concurrent_groups() -> None:
    """Two interleaved calls on one instance must not share authority state."""

    api = _runtime_api()
    allowed_groups = {101}
    barrier = asyncio.Barrier(2)
    decisions: dict[str, Any] = {}

    def admission(associated_group_ids: Sequence[int], *, intent: AdmissionIntent) -> AdmissionResult:
        assert intent is AdmissionIntent.BUSINESS
        # The answer must be derived from *this* call's own group ids, never a
        # process-global "current event" another task could have overwritten.
        if any(group in allowed_groups for group in associated_group_ids):
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

    runtime = api["RouletteRuntime"](
        config_manager=RecordingConfig(),
        recovery=RecordingRecovery(),
        admission=admission,
    )
    await runtime.start()

    async def call(name: str, group: int) -> None:
        await barrier.wait()
        decisions[name] = runtime.authorize(scope="business", group_ids=[group])

    await asyncio.gather(call("allowed", 101), call("revoked", 202))
    assert decisions["allowed"].allowed is True
    assert decisions["allowed"].reason_code == "policy_admitted"
    assert decisions["revoked"].allowed is False
    assert decisions["revoked"].reason_code == "policy_restricted"

    # Revoking one group later narrows only that group; it never widens another
    # held decision and no cross-group authority was cached.
    allowed_groups.discard(101)
    revoked_after = runtime.authorize(scope="business", group_ids=[101])
    assert revoked_after.allowed is False
    await runtime.close()


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


async def test_concurrent_send_gates_resolve_their_own_request() -> None:
    """Two interleaved handler dispatches must not share a send gate decision."""

    from komari_bot.plugins.komari_roulette.qq.handler import RouletteQQHandler

    from .tsk278_support import (
        FakeDelivery,
        FakeQQBot,
        admission_state,
        business_token,
        make_group_at_event,
    )

    service = _PerRequestService()
    delivery = FakeDelivery()
    revoked_group = "tsk279-group-revoked"
    observed: dict[str, str] = {}

    def send_gate(command_request: Any) -> bool:
        recorded = observed.setdefault(
            command_request.group_openid,
            command_request.inbound_msg_id,
        )
        assert recorded == command_request.inbound_msg_id
        return command_request.group_openid != revoked_group

    handler = RouletteQQHandler(
        service=service,
        delivery=delivery,
        business_gate=_allow_business,
        send_gate=send_gate,
    )
    barrier = asyncio.Barrier(2)

    async def run(group: str, member: str, message_id: str) -> Any | None:
        event = make_group_at_event(
            "/轮盘 开枪",
            message_id=message_id,
            group_openid=group,
            member_openid=member,
        )
        token = business_token(
            group_openid=group,
            member_openid=member,
            qq_message_id=message_id,
        )
        await barrier.wait()
        return await handler.handle(
            FakeQQBot(), event, state=admission_state(token=token)
        )

    await asyncio.gather(
        run("tsk279-group-allowed", "tsk279-member-1", "msg-1"),
        run(revoked_group, "tsk279-member-2", "msg-2"),
    )
    assert observed == {
        "tsk279-group-allowed": "msg-1",
        revoked_group: "msg-2",
    }
    # Each request's receipt keeps its own group/message; only the allowed one is
    # delivered.  A shared "current" gate would have delivered the wrong receipt.
    delivered = [entry[0] for entry in delivery.deliver_calls]
    assert [item.group_openid for item in delivered] == ["tsk279-group-allowed"]
    assert [item.inbound_msg_id for item in delivered] == ["msg-1"]
    assert [item.receipt_id for item in delivered] == ["r-msg-1"]


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
            self.sent: list[dict[str, Any]] = []

        async def send_to_group(
            self,
            group_openid: str,
            message: Any,
            *,
            msg_id: str | None = None,
            msg_seq: int | None = None,
        ) -> Any:
            self.sent.append(
                {
                    "group_openid": group_openid,
                    "message": message,
                    "msg_id": msg_id,
                    "msg_seq": msg_seq,
                }
            )
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
        barrier = asyncio.Barrier(2)

        async def deliver(command_receipt: Any) -> Any:
            await barrier.wait()
            return await delivery.deliver(command_receipt, sender)

        allowed, revoked = await asyncio.gather(
            deliver(receipts[0]),
            deliver(receipts[1]),
        )
    assert allowed is DeliveryOutcome.DELIVERED
    assert revoked is DeliveryOutcome.NOT_DELIVERED
    # The delivered payload must belong to the allowed group/receipt, and it must
    # answer the original inbound message (no cross-group msg_id).
    assert len(sender.sent) == 1
    sent = sender.sent[0]
    assert sent["group_openid"] == scopes[0].group_openid
    assert sent["msg_id"] == receipts[0].inbound_msg_id
    assert sent["msg_seq"] == 1
    assert sent["message"] == receipts[0].reply.body


# ---------------------------------------------------------------------------
# Real ConfigManager + real PG: the live flip is not a fake field mutation
# ---------------------------------------------------------------------------


async def _delete_roulette_config(harness: Tsk279Harness) -> None:
    async with harness.engine.begin() as connection:
        await connection.execute(text("DELETE FROM komari_roulette_config WHERE id = 1"))


@PG_REQUIRED
async def test_real_config_manager_read_api_supports_live_flip(
    harness: Tsk279Harness,
) -> None:
    """Old-API probe: the fixture the live-flip RED depends on really works."""

    from komari_bot.plugins.config_manager.manager import ConfigManager
    from komari_bot.plugins.komari_roulette.config_schema import DynamicConfigSchema

    await _delete_roulette_config(harness)
    manager = ConfigManager("komari_roulette", DynamicConfigSchema)
    try:
        initial = cast("DynamicConfigSchema", await manager.initialize_async())
        assert initial.plugin_enable is False
        await manager.update_field_async("plugin_enable", value=True)
        enabled = cast("DynamicConfigSchema", await manager.get_async())
        assert enabled.plugin_enable is True
        assert cast("DynamicConfigSchema", manager.get()).plugin_enable is True
        await manager.update_field_async("plugin_enable", value=False)
        disabled = cast("DynamicConfigSchema", await manager.get_async())
        assert disabled.plugin_enable is False
        assert cast("DynamicConfigSchema", manager.get()).plugin_enable is False
    finally:
        await _delete_roulette_config(harness)


@PG_REQUIRED
async def test_real_config_manager_live_flip_denies_business_and_keeps_maintenance(
    harness: Tsk279Harness,
) -> None:
    """A persisted config flip is read live; recovery keeps running meanwhile."""

    api = _runtime_api()
    from komari_bot.plugins.config_manager.manager import ConfigManager
    from komari_bot.plugins.komari_roulette.config_schema import DynamicConfigSchema

    await _delete_roulette_config(harness)
    manager = ConfigManager("komari_roulette", DynamicConfigSchema)
    try:
        await manager.initialize_async()
        await manager.update_field_async("plugin_enable", value=True)
        async with harness.scope("dyn-flip") as current:
            await seed_binding(harness.binding_manager, current, 1)
            service = service_for(harness, random_source=CountingRandom())
            await create_waiting(service, current)
            before = await current_game_row(harness.session_factory, current)
            assert before is not None
            dispatched: list[str] = []

            class RealRecovery:
                async def run_recovery_tick(self) -> object:
                    dispatched.append("tick")
                    return await service.advance_expired(current.group)

                async def close(self) -> None:
                    return None

            runtime = api["RouletteRuntime"](
                config_manager=manager,
                recovery=RealRecovery(),
                admission=_admission_by_group({1}),
            )
            await runtime.start()
            assert runtime.authorize(scope="business", group_ids=[1]).allowed is True

            # Flip the *persisted* config through the real manager: authority must
            # follow immediately, with no restart and no recovery tick needed.
            await manager.update_field_async("plugin_enable", value=False)
            denied = runtime.authorize(scope="business", group_ids=[1])
            assert denied.allowed is False
            assert denied.reason_code == "plugin_disabled"
            assert runtime.authorize(scope="send", group_ids=[1]).allowed is False

            # Disabling business neither stops the absolute recovery sweep nor
            # extends an existing deadline.
            await runtime.run_recovery_tick()
            assert dispatched == ["tick"]
            after = await current_game_row(harness.session_factory, current)
            assert after is not None
            assert after["lifecycle"] == "waiting"
            assert after["waiting_expires_at"] == before["waiting_expires_at"]
            assert after["state_revision"] == before["state_revision"]
            await runtime.close()
    finally:
        await _delete_roulette_config(harness)


# ---------------------------------------------------------------------------
# Delivery boundary: cancelled send keeps PENDING + wins, unsent is rejected
# ---------------------------------------------------------------------------


@PG_REQUIRED
async def test_delivery_cancel_keeps_pending_and_wins_but_unsent_is_rejected(
    harness: Tsk279Harness,
) -> None:
    from komari_bot.plugins.komari_roulette.qq.delivery import (
        DeliveryOutcome,
        RouletteDelivery,
    )

    service = service_for(harness, random_source=CountingRandom())

    class CancellingSender:
        def __init__(self) -> None:
            self.calls = 0

        async def send_to_group(
            self,
            group_openid: str,
            message: Any,
            *,
            msg_id: str | None = None,
            msg_seq: int | None = None,
        ) -> Any:
            del message, msg_id, msg_seq
            self.calls += 1
            assert group_openid
            raise asyncio.CancelledError

    async with harness.scope("cancel-pending") as current:
        members = await seed_players(harness.binding_manager, current, 2)
        await create_waiting(service, current)
        await join_player(service, current, members[1], "cp-join")
        await start_game(service, current, members[0], "cp-start")
        row = await current_game_row(harness.session_factory, current)
        assert row is not None
        terminal = await service.execute_group_command(
            request(
                current,
                "cp-forfeit",
                command_factory("forfeit"),
                member_openid=members[0],
            ),
            observation=observation(
                game_id=str(row["game_id"]),
                state_revision=int(row["state_revision"]),
                turn_seq=int(row["turn_seq"]),
            ),
        )
        assert terminal.result_code == "forfeited"

        sender = CancellingSender()
        delivery = RouletteDelivery(
            service,
            payload_builder=lambda command_receipt: command_receipt.reply.body,
        )
        with pytest.raises(asyncio.CancelledError):
            await delivery.deliver(terminal, sender)
        assert sender.calls == 1

        async with harness.session_factory() as session:
            state = await session.scalar(
                text(
                    "SELECT state FROM komari_roulette_fulfillments "
                    "WHERE receipt_id = :receipt_id"
                ),
                {"receipt_id": terminal.receipt_id},
            )
            wins = await session.scalar(
                text(
                    "SELECT wins FROM komari_roulette_leaderboard "
                    "WHERE app_id = :app_id AND group_openid = :group_openid"
                ),
                {
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                },
            )
        # A send that already started but was cancelled must stay PENDING and the
        # committed win must not be rolled back.
        assert state == "PENDING_CONFIRMATION"
        assert int(wins or 0) == 1

    class RecordingSender:
        def __init__(self) -> None:
            self.calls = 0

        async def send_to_group(
            self,
            group_openid: str,
            message: Any,
            *,
            msg_id: str | None = None,
            msg_seq: int | None = None,
        ) -> Any:
            del group_openid, message, msg_id, msg_seq
            self.calls += 1
            return SimpleNamespace(id="never-sent")

    # A receipt that never reached the network is a pre-send rejection: zero
    # network calls and the claim converges to NOT_DELIVERED.
    async with harness.scope("reject-unsent") as current:
        await seed_binding(harness.binding_manager, current, 1)
        pending = await create_waiting(service, current, message_id="unsent-1")
        sender = RecordingSender()
        delivery = RouletteDelivery(
            service,
            runtime_check=lambda _receipt: False,
            payload_builder=lambda command_receipt: command_receipt.reply.body,
        )
        outcome = await delivery.deliver(pending, sender)
        assert outcome is DeliveryOutcome.NOT_DELIVERED
        assert sender.calls == 0
        async with harness.session_factory() as session:
            state = await session.scalar(
                text(
                    "SELECT state FROM komari_roulette_fulfillments "
                    "WHERE receipt_id = :receipt_id"
                ),
                {"receipt_id": pending.receipt_id},
            )
        assert state == "NOT_DELIVERED"
