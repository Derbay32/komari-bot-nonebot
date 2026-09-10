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
import inspect
import json
from contextlib import suppress
from typing import TYPE_CHECKING, Any
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
    join_player,
    seed_players,
    service_for,
    start_game,
)
from .tsk279_lifecycle_support import (
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
