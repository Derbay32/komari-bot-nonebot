"""TSK-279 Stage-C1 RED: real composition root against real PG/Redis seams.

Stage-B pinned ``runtime`` / ``maintenance`` against a hand-built service.  C1
pins the **assembly** the production ``lifecycle.py`` will build:

* the config resource is the real ``ConfigManager`` from the top-level
  ``get_config_manager`` registry (never a re-constructed manager);
* the installed service reads the **live** real config (item weights + copy
  pool) and still freezes per-game facts at ``waiting -> active``;
* the installed scheduler cron callback drains a multi-batch retention backlog
  in one run;
* maintenance admission translates the canonical ``(app_id, group_openid)`` to
  the numeric group and consults ``group_admission`` - it never gates on the
  business ``plugin_enable`` switch;
* ``maintenance.close()`` is bounded but does not return while an in-flight
  cleanup round is still running.

All cases are `PG_REQUIRED`; production ``komari_roulette.lifecycle`` does not
exist yet, so the RED is ``ModuleNotFoundError`` / ``AttributeError`` on the
missing seam, never a fixture fabrication.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import text

from .command_support import (
    PG_REQUIRED,
    backend_pid,
    hold_group_lock,
    wait_for_blocked,
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
from .test_tsk279_effect_recheck_pg import (
    _mint_business_token,
    _real_binding_resolvers,
)
from .tsk279_lifecycle_support import (
    LIFECYCLE_MODULE,
    application_api,
    delete_roulette_config,
    invoke_hook,
    lifecycle_context,
    require_single_startup_hook,
)
from .tsk279_support import (
    Tsk279Harness,
    harness_fixture_body,
    insert_waiting_game,
    scope_counts,
    seed_aged_receipt,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = [pytest.mark.asyncio]

_DAY_SECONDS = 24 * 60 * 60


async def _maybe_await(value: object) -> Any:
    if inspect.isawaitable(value):
        return await value  # type: ignore[misc]
    return value


@pytest.fixture
async def harness() -> AsyncIterator[Tsk279Harness]:
    async for current in harness_fixture_body():
        yield current


async def _persisted_item_weights(
    harness: Tsk279Harness,
    current: Any,
) -> dict[str, int]:
    async with harness.session_factory() as session:
        raw = await session.scalar(
            text(
                "SELECT item_weights::text FROM komari_roulette_games "
                "WHERE app_id = :app_id AND group_openid = :group_openid "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"app_id": current.app_id, "group_openid": current.group_openid},
        )
    assert raw is not None, "no persisted roulette game row"
    weights = json.loads(raw) if isinstance(raw, str) else dict(raw)
    return {str(key): int(value) for key, value in weights.items()}


async def _stored_reply_body(
    harness: Tsk279Harness,
    receipt_id: str,
) -> str:
    async with harness.session_factory() as session:
        raw = await session.scalar(
            text(
                "SELECT reply_projection::text "
                "FROM komari_roulette_command_receipts "
                "WHERE receipt_id = :receipt_id"
            ),
            {"receipt_id": receipt_id},
        )
    assert raw is not None
    projection = json.loads(raw) if isinstance(raw, str) else dict(raw)
    return str(projection.get("body", ""))


async def _scope_receipts(harness: Tsk279Harness, current: Any) -> int:
    async with harness.session_factory() as session:
        return int(
            await session.scalar(
                text(
                    "SELECT count(*) FROM komari_roulette_command_receipts "
                    "WHERE app_id = :app_id AND group_openid = :group_openid"
                ),
                {"app_id": current.app_id, "group_openid": current.group_openid},
            )
            or 0
        )


async def _scope_latest_lifecycle(harness: Tsk279Harness, current: Any) -> str | None:
    async with harness.session_factory() as session:
        value = await session.scalar(
            text(
                "SELECT lifecycle FROM komari_roulette_games "
                "WHERE app_id = :app_id AND group_openid = :group_openid "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"app_id": current.app_id, "group_openid": current.group_openid},
        )
    return None if value is None else str(value)


async def _drive_installed_recovery(
    maintenance: Any,
    harness: Tsk279Harness,
    current: Any,
    *,
    batch_size: int = 100,
    max_pages: int = 60,
) -> Any:
    """Drive the real installed worker until this scope's latest game settles."""

    tick: Any = None
    for _ in range(max_pages):
        tick = await maintenance.advance_due(batch_size=batch_size)
        lifecycle = await _scope_latest_lifecycle(harness, current)
        if lifecycle not in {"waiting", "active"}:
            return tick
        if getattr(tick, "cursor", None) is None:
            return tick
    return tick


def _application() -> Any:
    app = application_api()["get_roulette_application"]()
    assert app is not None, "startup hook must install the application"
    return app


# ---------------------------------------------------------------------------
# Real ConfigManager registry + live config into the installed service
# ---------------------------------------------------------------------------


@PG_REQUIRED
async def test_installed_service_reads_live_real_item_weights_and_freezes_game(
    harness: Tsk279Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from komari_bot.plugins.config_manager import ConfigManager

    await delete_roulette_config(harness.engine)
    try:
        async with lifecycle_context(monkeypatch) as ctx:
            await invoke_hook(require_single_startup_hook(ctx))
            app = _application()
            manager = ctx.config_managers["komari_roulette"]
            assert isinstance(manager, ConfigManager), (
                "C1 must obtain the real ConfigManager from the top-level "
                "registry, not re-construct one"
            )
            assert app.config_manager is manager

            await manager.update_field_async("item_weight_magnifier", 3)
            await manager.update_field_async("item_weight_beer", 0)
            await manager.update_field_async("item_weight_burst", 0)
            await manager.update_field_async("item_weight_lock", 0)

            service = app.service
            async with harness.scope("live-weights") as current:
                members = await seed_players(harness.binding_manager, current, 2)
                await create_waiting(service, current)
                await join_player(service, current, members[1], "lw-join")
                await start_game(service, current, members[0], "lw-start")

                frozen = await _persisted_item_weights(harness, current)
                assert frozen == {
                    "magnifier": 3,
                    "beer": 0,
                    "burst": 0,
                    "lock": 0,
                }, (
                    "the installed service must read the live real config's "
                    f"item weights, got {frozen}"
                )

                # A persisted update must not rewrite the already-frozen game.
                await manager.update_field_async("item_weight_magnifier", 9)
                still_frozen = await _persisted_item_weights(harness, current)
                assert still_frozen == frozen, (
                    "an active game's frozen item weights must be immutable"
                )
    finally:
        await delete_roulette_config(harness.engine)


@PG_REQUIRED
async def test_installed_service_reads_live_real_copy_pool_and_freezes_receipt(
    harness: Tsk279Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from komari_bot.plugins.komari_roulette.config_schema import DynamicConfigSchema

    await delete_roulette_config(harness.engine)
    try:
        async with lifecycle_context(monkeypatch) as ctx:
            await invoke_hook(require_single_startup_hook(ctx))
            app = _application()
            manager = ctx.config_managers["komari_roulette"]

            pool = {
                key: list(values)
                for key, values in DynamicConfigSchema().action_copy_pool.items()
            }
            pool["created"] = ["C1注入文案已创建。"]
            await manager.update_field_async("action_copy_pool", pool)

            service = app.service
            async with harness.scope("live-copy") as current:
                await seed_players(harness.binding_manager, current, 1)
                receipt = await create_waiting(service, current)
                assert "C1注入文案已创建。" in receipt.reply.body, (
                    "the installed projector must read the live real config's "
                    f"copy pool, got {receipt.reply.body!r}"
                )

                pool_after = dict(pool)
                pool_after["created"] = ["替换后文案。"]
                await manager.update_field_async("action_copy_pool", pool_after)
                stored = await _stored_reply_body(harness, receipt.receipt_id)
                assert "C1注入文案已创建。" in stored, (
                    "a committed receipt's frozen copy must not be rewritten"
                )
    finally:
        await delete_roulette_config(harness.engine)


# ---------------------------------------------------------------------------
# Installed scheduler cron drains the multi-batch retention backlog
# ---------------------------------------------------------------------------


@PG_REQUIRED
async def test_installed_cron_callback_drains_multi_batch_backlog(
    harness: Tsk279Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from komari_bot.plugins.komari_roulette.maintenance import CLEANUP_JOB_ID

    await delete_roulette_config(harness.engine)
    try:
        async with lifecycle_context(monkeypatch) as ctx:
            await invoke_hook(require_single_startup_hook(ctx))
            job = ctx.scheduler.get_job(CLEANUP_JOB_ID)
            assert job is not None, "startup must register the cleanup job"
            callback = job["func"]

            async with harness.scope("cron-live-drain") as current:
                for index in range(201):
                    await seed_aged_receipt(
                        harness.session_factory,
                        current,
                        inbound_msg_id=f"cld-{index}",
                        age_seconds=_DAY_SECONDS * 8,
                    )
                await _maybe_await(callback())
                remaining = await _scope_receipts(harness, current)
                assert remaining == 0, (
                    "one installed 04:00 cron run must drain the multi-batch "
                    f"retention backlog, {remaining} aged receipts remain"
                )
    finally:
        await delete_roulette_config(harness.engine)


# ---------------------------------------------------------------------------
# Maintenance admission: canonical numeric group + admission, not the switch
# ---------------------------------------------------------------------------


@PG_REQUIRED
async def test_maintenance_admission_is_canonical_and_ignores_business_switch(
    harness: Tsk279Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The installed worker resolves the canonical group and consults admission.

    The real composition root is exercised end to end through the public
    ``advance_due`` worker (never ``app.maintenance._admission``): the business
    switch is off by default yet a canonically-bound, currently-admitted group
    is still advanced, and the same group is left untouched once the real
    admission policy revokes it.
    """

    from komari_bot.plugins import group_admission
    from tests.group_admission.management_support import prepare_control_plane
    from tests.group_admission.runtime_support import (
        AdmissionStorageFake,
        stored_policy,
    )

    numeric_group = 770031
    storage = AdmissionStorageFake(
        stored_policy(1, {"mode": "blacklist", "group_ids": []})
    )
    await prepare_control_plane(monkeypatch, storage)

    await delete_roulette_config(harness.engine)
    try:
        async with lifecycle_context(monkeypatch) as ctx:
            await invoke_hook(require_single_startup_hook(ctx))
            app = _application()
            assert app.runtime.get_state().plugin_enable is False
            assert app.runtime.accepting is False

            async with harness.scope("maintenance-admission") as current:
                await harness.binding_manager.bind_group_member(
                    app_id=current.app_id,
                    group_id=str(numeric_group),
                    group_openid=current.group_openid,
                    member_qq="7700310001",
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
                    deadline_age_seconds=315_360_000,
                )
                await _drive_installed_recovery(
                    app.maintenance, harness, current
                )
                assert await _scope_latest_lifecycle(harness, current) == "expired", (
                    "maintenance must advance the canonically-bound group via "
                    "admission even while the business switch is off"
                )

                # The same canonical group is revoked by the real policy.
                storage.deliver(
                    stored_policy(
                        2,
                        {"mode": "blacklist", "group_ids": [numeric_group]},
                    )
                )
                await insert_waiting_game(
                    harness.session_factory,
                    game_id=str(uuid4()),
                    app_id=current.app_id,
                    group_openid=current.group_openid,
                    member_openid=current.member_openid,
                    deadline_age_seconds=315_360_000,
                )
                await _drive_installed_recovery(
                    app.maintenance, harness, current
                )
                assert await _scope_latest_lifecycle(harness, current) == "waiting", (
                    "a policy-revoked group must not advance"
                )
    finally:
        group_admission.register_qq_group_resolver(None)
        await delete_roulette_config(harness.engine)


# ---------------------------------------------------------------------------
# Shutdown: bounded close of an in-flight cleanup round
# ---------------------------------------------------------------------------


@PG_REQUIRED
async def test_maintenance_close_waits_for_the_in_flight_cleanup_round(
    harness: Tsk279Harness,
) -> None:
    """``close`` must not return while a bounded cleanup round is still running.

    The maintenance docstring promises "in-flight rounds finish their bounded
    page"; returning immediately after flipping ``_stopped`` lets a round keep
    deleting after shutdown returned.  This is a real production-behaviour gap
    (``RouletteMaintenance`` already exists), not a missing-seam RED.
    """

    from komari_bot.plugins.komari_roulette.maintenance import RouletteMaintenance

    maintenance = RouletteMaintenance(
        session_factory=harness.session_factory,
        service=service_for(harness, random_source=CountingRandom()),
        admission=lambda _app, _group: True,
    )
    async with harness.scope("close-drain") as current:
        await seed_aged_receipt(
            harness.session_factory,
            current,
            inbound_msg_id="close-drain-1",
            age_seconds=_DAY_SECONDS * 8,
        )
        async with harness.session_factory() as blocker:
            await blocker.begin()
            blocker_pid = await backend_pid(blocker)
            await hold_group_lock(blocker, current)
            round_task = asyncio.create_task(
                maintenance.cleanup_retention(batch_size=100)
            )
            close_task: asyncio.Task[None] | None = None
            try:
                await wait_for_blocked(harness.session_factory, blocker_pid)
                close_task = asyncio.create_task(maintenance.close())
                await asyncio.sleep(0.2)
                assert not close_task.done(), (
                    "maintenance.close() must wait for the in-flight cleanup "
                    "round instead of returning immediately"
                )
            finally:
                await blocker.commit()
                async with asyncio.timeout(10):
                    if close_task is not None:
                        with suppress(Exception):
                            await close_task
                    with suppress(Exception):
                        await round_task
        assert await _scope_receipts(harness, current) == 0


@PG_REQUIRED
async def test_stop_after_start_leaves_no_roulette_jobs(
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
            await invoke_hook(require_single_startup_hook(ctx))
            assert ctx.scheduler.get_job(RECOVERY_JOB_ID) is not None
            assert ctx.scheduler.get_job(CLEANUP_JOB_ID) is not None
            api = application_api()
            with suppress(Exception):
                await _maybe_await(api["stop_roulette_application"]())
            assert ctx.scheduler.get_job(RECOVERY_JOB_ID) is None
            assert ctx.scheduler.get_job(CLEANUP_JOB_ID) is None
    finally:
        await delete_roulette_config(harness.engine)


# ---------------------------------------------------------------------------
# Lifecycle → QQ business wiring: the *real* composition root, not a manual
# ``install_roulette_qq_runtime``.
#
# C1 review gap: every persisted GREEN "installed" probe builds its own runtime
# with ``install_roulette_qq_runtime`` and test-owned gates, so it cannot show
# that the *real* lifecycle installs gates that read the real authority. These
# cases never install a runtime and never replace its three gates: they drive the
# real driver startup hook and then the globally installed ``handle_roulette_qq``
# over real ``AdmissionRuntime`` / ``BindingTransaction`` / ``UserBanService``.
# They are RED until ``komari_roulette.lifecycle`` exists.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _InstalledQQAuthority:
    """Handles of one real lifecycle install plus real authority wiring."""

    app: Any
    manager: Any
    current: Any
    storage: Any
    ban_service: Any
    business_gate: Any
    numeric_group: int
    member_qq: int


@asynccontextmanager
async def _installed_lifecycle_qq_authority(
    harness: Tsk279Harness,
    monkeypatch: pytest.MonkeyPatch,
    *,
    plugin_enable: bool,
    numeric_group: int,
) -> AsyncIterator[_InstalledQQAuthority]:
    """Install the real lifecycle and real authority; never install a QQ runtime.

    Ordering is deliberately *dependency-first*, mirroring production fail-closed
    startup: (1) the real admission control plane reaches READY with its fake
    policy store, (2) the canonical binding/member/ban callbacks are registered,
    (3) the real driver startup hook builds the composition root, (4) the live
    switch is written through that root's real ``ConfigManager``, and only then
    (5) the globally installed ``handle_roulette_qq`` is driven.  A startup that
    ran before its authority dependencies were ready would have to ignore their
    readiness, so this fixture never forces that shape.

    ``prepare_control_plane`` monkeypatches the module-global
    ``manager.get_config_storage`` factory; the fixture restores the real factory
    immediately afterwards so every later ``ConfigManager`` (the roulette one)
    reads real PostgreSQL.  The already-started admission runtime keeps working
    because its ``ConfigManager`` captured the fake store's watcher callback
    during ``start``.
    """

    from komari_bot.plugins import group_admission
    from komari_bot.plugins.config_manager import manager as manager_module
    from komari_bot.plugins.user_ban.service import UserBanService
    from tests.group_admission.management_support import prepare_control_plane
    from tests.group_admission.runtime_support import (
        AdmissionStorageFake,
        stored_policy,
    )

    storage = AdmissionStorageFake(
        stored_policy(1, {"mode": "blacklist", "group_ids": []})
    )
    await delete_roulette_config(harness.engine)
    ban_service = UserBanService()
    original_get_config_storage = manager_module.get_config_storage
    # A fresh random member QQ per case: a ban left behind by an earlier run can
    # never match it, so there is no foreign record to pre-clean and the
    # ``finally`` unban only ever touches this case's own identity.
    member_qq = 8_500_000_000 + int(uuid4().int % 1_000_000_000)
    try:
        # 1. Real READY admission first: the runtime consumes the fake policy
        #    store and registers its watcher before the seam is restored.
        await prepare_control_plane(monkeypatch, storage)
        # 2. Restore the real storage factory before the composition root starts:
        #    the admission runtime keeps its captured fake watcher, while the
        #    roulette ``ConfigManager`` must read real PostgreSQL.
        manager_module.get_config_storage = original_get_config_storage

        # 3. Register the real canonical binding/member/ban callbacks before any
        #    installed gate can read them.
        resolve_group, resolve_member = await _real_binding_resolvers(harness)

        async def ban_checker(member_qq_value: int, scope: object) -> bool:
            return await ban_service.is_user_banned(
                str(member_qq_value), cast("Any", scope)
            )

        async with harness.scope("lifecycle-qq") as current:
            await harness.binding_manager.bind_group_member(
                app_id=current.app_id,
                group_id=str(numeric_group),
                group_openid=current.group_openid,
                member_qq=str(member_qq),
                member_openid=current.member_openid,
                character_name="Seat 1",
                bot_self_id="tsk279-test-bot",
            )
            group_admission.register_qq_group_resolver(
                resolve_group, member_resolver=resolve_member
            )
            group_admission.register_qq_ban_checker(ban_checker)
            try:
                async with lifecycle_context(monkeypatch) as ctx:
                    # Capture the real installed business gate without replacing
                    # it: the recording wrapper delegates to the production
                    # install, so the gate under test is exactly the one the
                    # composition root installs.
                    lifecycle_module = importlib.import_module(LIFECYCLE_MODULE)
                    real_install = lifecycle_module.install_roulette_qq_runtime
                    captured_gate: dict[str, Any] = {}

                    def _recording_install(**kwargs: Any) -> Any:
                        captured_gate["business_gate"] = kwargs["business_gate"]
                        return real_install(**kwargs)

                    monkeypatch.setattr(
                        lifecycle_module,
                        "install_roulette_qq_runtime",
                        _recording_install,
                    )
                    # 4. Real driver startup: the composition root reads the real
                    #    PostgreSQL config through the restored factory.
                    await invoke_hook(require_single_startup_hook(ctx))
                    app = _application()
                    manager = ctx.config_managers["komari_roulette"]
                    # 5. Legal live switch through the real manager (real PG).
                    await manager.update_field_async("plugin_enable", plugin_enable)
                    if plugin_enable:
                        await app.runtime.run_recovery_tick()
                    assert app.runtime.get_state().plugin_enable is plugin_enable, (
                        "the lifecycle config manager must carry the requested "
                        "live switch"
                    )
                    assert "business_gate" in captured_gate, (
                        "the composition root must install the QQ runtime through "
                        "the package install helper"
                    )
                    yield _InstalledQQAuthority(
                        app=app,
                        manager=manager,
                        current=current,
                        storage=storage,
                        ban_service=ban_service,
                        numeric_group=numeric_group,
                        member_qq=member_qq,
                        business_gate=captured_gate["business_gate"],
                    )
            finally:
                group_admission.register_qq_group_resolver(None)
                group_admission.register_qq_ban_checker(None)
    finally:
        manager_module.get_config_storage = original_get_config_storage
        with suppress(Exception):
            # Case-owned hygiene: never leave this case's ban behind for a
            # sibling parametrization (which may run later in the same session).
            await ban_service.unban_user(
                user_id=str(member_qq), target_scope="command"
            )
        with suppress(Exception):
            await ban_service.close()
        await delete_roulette_config(harness.engine)


async def _scope_latest_receipt_id(harness: Tsk279Harness, current: Any) -> str | None:
    async with harness.session_factory() as session:
        value = await session.scalar(
            text(
                "SELECT receipt_id FROM komari_roulette_command_receipts "
                "WHERE app_id = :app_id AND group_openid = :group_openid "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"app_id": current.app_id, "group_openid": current.group_openid},
        )
    return None if value is None else str(value)


async def _fulfillment_state(
    harness: Tsk279Harness, current: Any, inbound_msg_id: str
) -> str | None:
    async with harness.session_factory() as session:
        value = await session.scalar(
            text(
                "SELECT f.state FROM komari_roulette_fulfillments AS f "
                "JOIN komari_roulette_command_receipts AS r "
                "ON r.receipt_id = f.receipt_id "
                "WHERE r.app_id = :app_id AND r.group_openid = :group_openid "
                "AND r.inbound_msg_id = :inbound"
            ),
            {
                "app_id": current.app_id,
                "group_openid": current.group_openid,
                "inbound": inbound_msg_id,
            },
        )
    return None if value is None else str(value)


@PG_REQUIRED
async def test_restoring_config_storage_factory_keeps_admission_fake_live_and_roulette_real_pg(
    harness: Tsk279Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Old-API probe for the C1 fixture order: admission fake + real factory.

    ``prepare_control_plane`` monkeypatches the module-global
    ``manager.get_config_storage`` factory, so a ``ConfigManager`` built later
    would silently read the admission fake.  The lifecycle fixture restores the
    real factory *after* admission is READY and before the roulette composition
    root starts.  This probe proves the restore is correct both ways without
    depending on the still-missing ``lifecycle`` module:

    * the already-started ``AdmissionRuntime`` still receives the fake's watcher
      deliveries after the restore (a policy revocation really bites), because
      its manager captured the fake watcher at ``start``;
    * a real ``ConfigManager`` built after the restore reads and writes the real
      PostgreSQL ``komari_roulette_config`` table, never the admission fake.
    """

    from komari_bot.plugins import group_admission
    from komari_bot.plugins.config_manager import manager as manager_module
    from komari_bot.plugins.config_manager.manager import ConfigManager
    from komari_bot.plugins.komari_roulette.config_schema import DynamicConfigSchema
    from tests.group_admission.management_support import prepare_control_plane
    from tests.group_admission.runtime_support import (
        AdmissionStorageFake,
        stored_policy,
    )

    numeric_group = 279540
    storage = AdmissionStorageFake(
        stored_policy(1, {"mode": "blacklist", "group_ids": []})
    )
    original_get_config_storage = manager_module.get_config_storage
    await delete_roulette_config(harness.engine)
    try:
        await prepare_control_plane(monkeypatch, storage)
        admitted = group_admission.adjudicate([numeric_group])
        assert (
            admitted.qualification is group_admission.AdmissionQualification.BUSINESS
        ), "the real admission runtime must be READY before the factory restore"

        manager_module.get_config_storage = original_get_config_storage

        # The fake's captured watcher keeps the *started* runtime live.
        storage.deliver(
            stored_policy(2, {"mode": "blacklist", "group_ids": [numeric_group]})
        )
        revoked = group_admission.adjudicate([numeric_group])
        assert (
            revoked.qualification
            is group_admission.AdmissionQualification.REJECTED
        ), "a fake-delivered revocation must still reach the started runtime"
        assert revoked.reason_code == "policy_restricted"
        assert group_admission.get_runtime_state().effective_revision == 2
        storage.deliver(stored_policy(3, {"mode": "blacklist", "group_ids": []}))
        assert (
            group_admission.adjudicate([numeric_group]).qualification
            is group_admission.AdmissionQualification.BUSINESS
        ), "readmission through the same fake watcher must work"

        # A real manager built after the restore uses real PostgreSQL.
        roulette_manager = ConfigManager("komari_roulette", DynamicConfigSchema)
        await roulette_manager.initialize_async()
        await roulette_manager.update_field_async("plugin_enable", value=True)
        async with harness.session_factory() as session:
            stored = await session.scalar(
                text("SELECT plugin_enable FROM komari_roulette_config WHERE id = 1")
            )
        assert stored is True, (
            "a ConfigManager built after the factory restore must persist to the "
            f"real PostgreSQL roulette config, got {stored!r}"
        )
    finally:
        manager_module.get_config_storage = original_get_config_storage
        await delete_roulette_config(harness.engine)


@PG_REQUIRED
async def test_lifecycle_installed_qq_handler_commits_valid_token_and_captures_real_sdk_payload(
    harness: Tsk279Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .tsk278_support import admission_state
    from .tsk279_lifecycle_support import QQ_MODULE, RecordingQQBot

    async with _installed_lifecycle_qq_authority(
        harness,
        monkeypatch,
        plugin_enable=True,
        numeric_group=279501,
    ) as authority:
        qq = __import__(QQ_MODULE, fromlist=["handle_roulette_qq"])
        assert qq.get_roulette_qq_runtime() is not None, (
            "the real startup hook must install the QQ runtime globally"
        )
        token, _mint_bot, event = await _mint_business_token(
            authority.current, "lifecycle-allow-1"
        )
        bot = RecordingQQBot(authority.current.app_id)
        await qq.handle_roulette_qq(bot, event, admission_state(token=token))

        assert await _scope_receipts(harness, authority.current) == 1
        apis = [api for api, _data in bot.calls]
        assert apis == ["post_group_messages"], (
            "the lifecycle-installed handler must produce exactly one real SDK "
            f"payload, got {apis}"
        )
        payload = bot.calls[0][1]
        assert payload["msg_id"] == "lifecycle-allow-1"
        assert payload["msg_seq"] == 1
        receipt_id = await _scope_latest_receipt_id(harness, authority.current)
        assert receipt_id is not None
        assert payload["markdown"].content == await _stored_reply_body(
            harness, receipt_id
        ), "the SDK payload must carry the frozen committed body"


_REVOKE_CASES = ("plugin_disabled", "user_banned", "policy_restricted")


@PG_REQUIRED
@pytest.mark.parametrize("mutation", _REVOKE_CASES)
async def test_lifecycle_installed_qq_handler_rejects_revoked_authority_before_execution(
    harness: Tsk279Harness,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    from tests.group_admission.runtime_support import stored_policy

    from .tsk278_support import admission_state
    from .tsk279_lifecycle_support import QQ_MODULE, RecordingQQBot

    async with _installed_lifecycle_qq_authority(
        harness,
        monkeypatch,
        plugin_enable=mutation != "plugin_disabled",
        numeric_group=279510,
    ) as authority:
        qq = __import__(QQ_MODULE, fromlist=["handle_roulette_qq"])
        token, _mint_bot, event = await _mint_business_token(
            authority.current, f"lifecycle-reject-{mutation}"
        )
        match mutation:
            case "plugin_disabled":
                pass  # installed with the live switch already off
            case "user_banned":
                await authority.ban_service.ban_user(
                    user_id=str(authority.member_qq),
                    target_scope="command",
                    operator_id="tsk279-test",
                )
            case "policy_restricted":
                authority.storage.deliver(
                    stored_policy(
                        2,
                        {
                            "mode": "blacklist",
                            "group_ids": [authority.numeric_group],
                        },
                    )
                )
            case _:
                message = f"unknown revocation case: {mutation}"
                raise AssertionError(message)

        bot = RecordingQQBot(authority.current.app_id)
        await qq.handle_roulette_qq(bot, event, admission_state(token=token))

        assert bot.calls == [], (
            f"a {mutation} authority before execution must never send"
        )
        assert await _scope_receipts(harness, authority.current) == 0, (
            f"a {mutation} authority before execution must leave no receipt"
        )
        counts = await scope_counts(harness.session_factory, authority.current)
        assert all(count == 0 for count in counts.values()), (
            f"a {mutation} authority before execution must leave zero effects, "
            f"got {counts}"
        )


class _PostClaimClaimBarrier:
    """Pause the *real* ``app.service.claim_fulfillment`` after the claim commits.

    The committed ``PENDING_CONFIRMATION`` fulfillment row is the deterministic
    post-claim marker: it cannot be observed before the real claim returned, and
    the delivery only re-reads live authority *after* the claim.  Blocking inside
    the real service method therefore lets this case revoke authority before any
    final authority read begins, while still exercising the genuine claim and
    delivery path.  It never guesses whether the installed implementation calls
    a group resolver (a valid binding-session read path may not).

    Only this case's receipt id is gated; foreign calls pass straight through.
    ``RouletteCommandService`` is a plain (non-``slots``) class, so wrapping the
    bound method on the installed instance is enough and leaves other objects
    untouched.
    """

    def __init__(
        self,
        harness: Tsk279Harness,
        *,
        app_id: str,
        inbound_msg_id: str,
    ) -> None:
        self._harness = harness
        self._app_id = app_id
        self._inbound = inbound_msg_id
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self._blocked = False

    async def _this_case_receipt_id(self) -> str | None:
        async with self._harness.session_factory() as session:
            value = await session.scalar(
                text(
                    "SELECT receipt_id FROM komari_roulette_command_receipts "
                    "WHERE app_id = :app_id AND inbound_msg_id = :inbound "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"app_id": self._app_id, "inbound": self._inbound},
            )
        return None if value is None else str(value)

    async def _pending(self, receipt_id: str) -> bool:
        async with self._harness.session_factory() as session:
            state = await session.scalar(
                text(
                    "SELECT state FROM komari_roulette_fulfillments "
                    "WHERE receipt_id = :receipt_id"
                ),
                {"receipt_id": receipt_id},
            )
        return str(state) == "PENDING_CONFIRMATION"

    def install(self, service: Any) -> None:
        """Wrap the real bound ``claim_fulfillment`` on the installed service."""

        original = service.claim_fulfillment

        async def gated(receipt_id: str) -> Any:
            self.calls += 1
            claim = await original(receipt_id)
            if (
                not self._blocked
                and receipt_id == await self._this_case_receipt_id()
                and await self._pending(receipt_id)
            ):
                self._blocked = True
                self.entered.set()
                await self.release.wait()
            return claim

        service.claim_fulfillment = gated


_POST_CLAIM_CASES = ("user_banned", "canonical_remapped")


@PG_REQUIRED
@pytest.mark.parametrize("mutation", _POST_CLAIM_CASES)
async def test_lifecycle_installed_delivery_post_claim_revocation_blocks_network_but_keeps_committed_facts(
    harness: Tsk279Harness,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    from .tsk278_support import admission_state
    from .tsk279_lifecycle_support import QQ_MODULE, RecordingQQBot

    inbound = f"lifecycle-postclaim-{mutation}"
    async with _installed_lifecycle_qq_authority(
        harness,
        monkeypatch,
        plugin_enable=True,
        numeric_group=279520,
    ) as authority:
        qq = __import__(QQ_MODULE, fromlist=["handle_roulette_qq"])
        barrier = _PostClaimClaimBarrier(
            harness, app_id=authority.current.app_id, inbound_msg_id=inbound
        )
        barrier.install(authority.app.service)
        token, _mint_bot, event = await _mint_business_token(
            authority.current, inbound
        )
        bot = RecordingQQBot(authority.current.app_id)
        handler_task = asyncio.create_task(
            qq.handle_roulette_qq(bot, event, admission_state(token=token))
        )
        try:
            async with asyncio.timeout(10):
                await barrier.entered.wait()
            assert await _fulfillment_state(harness, authority.current, inbound) == (
                "PENDING_CONFIRMATION"
            ), "the real PG claim must be committed before authority is revoked"
            match mutation:
                case "user_banned":
                    await authority.ban_service.ban_user(
                        user_id=str(authority.member_qq),
                        target_scope="command",
                        operator_id="tsk279-test",
                    )
                case "canonical_remapped":
                    async with harness.engine.begin() as connection:
                        await connection.execute(
                            text(
                                "UPDATE komari_character_binding_groups "
                                "SET group_id = :moved "
                                "WHERE app_id = :app_id AND group_openid = :group"
                            ),
                            {
                                "moved": str(authority.numeric_group + 1),
                                "app_id": authority.current.app_id,
                                "group": authority.current.group_openid,
                            },
                        )
                case _:
                    message = f"unknown post-claim case: {mutation}"
                    raise AssertionError(message)
        finally:
            barrier.release.set()
        async with asyncio.timeout(10):
            await handler_task

        assert bot.calls == [], (
            "an authority revoked after the claim must never reach the network"
        )
        assert await _fulfillment_state(harness, authority.current, inbound) == (
            "NOT_DELIVERED"
        )
        assert await _scope_receipts(harness, authority.current) == 1, (
            "the committed domain receipt must survive a post-claim rejection"
        )
        row = await current_game_row(harness.session_factory, authority.current)
        assert row is not None, (
            "the committed game fact must survive a post-claim rejection"
        )
        assert str(row["lifecycle"]) == "waiting"


@PG_REQUIRED
async def test_lifecycle_installed_qq_handler_group_lock_wait_then_plugin_false_leaves_zero_receipt_game(
    harness: Tsk279Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .tsk278_support import admission_state
    from .tsk279_lifecycle_support import QQ_MODULE, RecordingQQBot

    async with _installed_lifecycle_qq_authority(
        harness,
        monkeypatch,
        plugin_enable=True,
        numeric_group=279530,
    ) as authority:
        qq = __import__(QQ_MODULE, fromlist=["handle_roulette_qq"])
        token, _mint_bot, event = await _mint_business_token(
            authority.current, "lifecycle-lock-1"
        )
        bot = RecordingQQBot(authority.current.app_id)
        async with harness.session_factory() as blocker:
            await blocker.begin()
            blocker_pid = await backend_pid(blocker)
            await hold_group_lock(blocker, authority.current)
            handler_task = asyncio.create_task(
                qq.handle_roulette_qq(bot, event, admission_state(token=token))
            )
            try:
                await wait_for_blocked(harness.session_factory, blocker_pid)
                await authority.manager.update_field_async(
                    "plugin_enable", value=False
                )
            finally:
                await blocker.commit()
            async with asyncio.timeout(10):
                await handler_task

        assert bot.calls == [], (
            "a switch turned off while the command queued must never send"
        )
        assert await _scope_receipts(harness, authority.current) == 0, (
            "a switch turned off while the command queued must leave no receipt"
        )
        counts = await scope_counts(harness.session_factory, authority.current)
        assert all(count == 0 for count in counts.values()), (
            "a switch turned off while the command queued must leave zero "
            f"effects, got {counts}"
        )


# ---------------------------------------------------------------------------
# C1 review gap: the installed gate reads the *old* app state, so a stop that
# begins (and even returns) can leave that gate accepting, and the registered
# cleanup job is never cancelled.  These cases pin the local-state ordering.
# ---------------------------------------------------------------------------


@PG_REQUIRED
async def test_lifecycle_stop_rejects_the_old_installed_gate_before_maintenance_drains(
    harness: Tsk279Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop must close the old installed gate before maintenance drains.

    ``stop_roulette_application`` clears QQ dispatch and then bounded-closes
    maintenance *before* closing the runtime, so ``runtime.accepting`` only turns
    false after the (up to ``CLOSE_ROUND_TIMEOUT_SECONDS``) maintenance close.  A
    handler already holding the old installed gate must already be rejected when
    stop begins - not only after the drain - and the released stop must not
    resurrect any state.
    """

    from .tsk279_lifecycle_support import QQ_MODULE, RecordingQQBot

    async with _installed_lifecycle_qq_authority(
        harness,
        monkeypatch,
        plugin_enable=True,
        numeric_group=279550,
    ) as authority:
        token, _mint_bot, event = await _mint_business_token(
            authority.current, "lifecycle-stop-gate-1"
        )
        bot = RecordingQQBot(authority.current.app_id)

        drain_entered = asyncio.Event()
        drain_release = asyncio.Event()
        real_close = authority.app.maintenance.close

        async def gated_close() -> None:
            drain_entered.set()
            await drain_release.wait()
            await real_close()

        authority.app.maintenance.close = gated_close
        stop_application = application_api()["stop_roulette_application"]

        stop_task = asyncio.create_task(stop_application())
        try:
            async with asyncio.timeout(10):
                await drain_entered.wait()
            rejected = await authority.business_gate(bot, event, token)
            assert rejected is False, (
                "the old installed gate must reject as soon as stop begins, not "
                "only after maintenance drains"
            )
        finally:
            drain_release.set()
            async with asyncio.timeout(10):
                await stop_task

        assert application_api()["get_roulette_application"]() is None, (
            "a released stop must not resurrect the application"
        )
        qq = __import__(QQ_MODULE, fromlist=["get_roulette_qq_runtime"])
        assert qq.get_roulette_qq_runtime() is None, (
            "a released stop must not resurrect QQ dispatch"
        )


@PG_REQUIRED
async def test_stop_bounded_joins_the_registered_cleanup_blocked_on_the_group_lock(
    harness: Tsk279Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop must cancel/join the registered cleanup, not only time out.

    The real registered cleanup callback waits on the group advisory lock while a
    foreign transaction holds it past the shutdown bound.  ``maintenance.close``
    only waits ``CLOSE_ROUND_TIMEOUT_SECONDS`` for a round boundary and then
    returns, so a still-running job would resume and DELETE after shutdown
    returned.  Shutdown must boundedly cancel/join the owned job, and the
    released lock must not let the cleanup delete anything after stop returned.
    """

    from komari_bot.plugins.komari_roulette import maintenance as maintenance_module
    from komari_bot.plugins.komari_roulette.maintenance import CLEANUP_JOB_ID

    # Inject the real bound so the case does not wait the production five
    # seconds; the semantics under test (never DELETE after stop returned) are
    # unchanged, and a normal short round still drains (see the close test).
    monkeypatch.setattr(maintenance_module, "CLOSE_ROUND_TIMEOUT_SECONDS", 0.5)

    await delete_roulette_config(harness.engine)
    try:
        async with lifecycle_context(monkeypatch) as ctx:
            await invoke_hook(require_single_startup_hook(ctx))
            job = ctx.scheduler.get_job(CLEANUP_JOB_ID)
            assert job is not None, "startup must register the cleanup job"
            callback = job["func"]

            async with harness.scope("stop-cleanup-inflight") as current:
                await seed_aged_receipt(
                    harness.session_factory,
                    current,
                    inbound_msg_id="stop-cleanup-inflight-1",
                    age_seconds=_DAY_SECONDS * 8,
                )
                async with harness.session_factory() as blocker:
                    await blocker.begin()
                    blocker_pid = await backend_pid(blocker)
                    await hold_group_lock(blocker, current)
                    cleanup_task = asyncio.create_task(_maybe_await(callback()))
                    try:
                        await wait_for_blocked(
                            harness.session_factory, blocker_pid
                        )
                        stop_task = asyncio.create_task(
                            application_api()["stop_roulette_application"]()
                        )
                        async with asyncio.timeout(10):
                            await stop_task
                    finally:
                        await blocker.commit()
                        with suppress(asyncio.CancelledError, Exception):
                            await asyncio.wait_for(cleanup_task, timeout=10)

                assert await _scope_receipts(harness, current) == 1, (
                    "the registered cleanup must not DELETE after stop returned"
                )
    finally:
        await delete_roulette_config(harness.engine)


@PG_REQUIRED
async def test_lifecycle_business_gate_rechecks_local_switch_after_the_recheck(
    harness: Tsk279Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The installed gate must re-read the local live switch after the recheck.

    ``recheck_qq_effect`` is wrapped only to control timing (an ``Event``) and
    still delegates to the real helper, so no remote verdict is fabricated.  The
    live ``plugin_enable`` is flipped while the gate is suspended inside the
    await; the gate must not release the effect (or the send) afterwards.
    """

    from komari_bot.plugins import group_admission

    from .tsk278_support import admission_state
    from .tsk279_lifecycle_support import QQ_MODULE, RecordingQQBot

    async with _installed_lifecycle_qq_authority(
        harness,
        monkeypatch,
        plugin_enable=True,
        numeric_group=279560,
    ) as authority:
        qq = __import__(QQ_MODULE, fromlist=["handle_roulette_qq"])
        token, _mint_bot, event = await _mint_business_token(
            authority.current, "lifecycle-local-1"
        )

        recheck_entered = asyncio.Event()
        recheck_release = asyncio.Event()
        real_recheck = group_admission.recheck_qq_effect

        async def timing_recheck(checked: Any, *, effect: Any) -> Any:
            recheck_entered.set()
            await recheck_release.wait()
            return await real_recheck(checked, effect=effect)

        monkeypatch.setattr(
            group_admission, "recheck_qq_effect", timing_recheck
        )

        bot = RecordingQQBot(authority.current.app_id)
        handler_task = asyncio.create_task(
            qq.handle_roulette_qq(bot, event, admission_state(token=token))
        )
        try:
            async with asyncio.timeout(10):
                await recheck_entered.wait()
            # The live switch turns off while the gate waits on the authority.
            await authority.manager.update_field_async(
                "plugin_enable", value=False
            )
        finally:
            recheck_release.set()
        async with asyncio.timeout(10):
            await handler_task

        assert bot.calls == [], (
            "a live switch turned off during the recheck must block the send"
        )
        assert await _scope_receipts(harness, authority.current) == 0, (
            "a live switch turned off during the recheck must leave no receipt"
        )
        counts = await scope_counts(harness.session_factory, authority.current)
        assert all(count == 0 for count in counts.values()), (
            "a live switch turned off during the recheck must leave zero "
            f"effects, got {counts}"
        )
