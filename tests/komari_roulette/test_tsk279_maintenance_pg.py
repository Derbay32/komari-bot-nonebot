"""TSK-279 Stage-B: recovery scan, retention cleanup and PG-lock evidence.

The first four cases are **GREEN real-implementation probes**: they drive the
existing deep entry ``RouletteCommandService.advance_expired`` plus the real
storage/ORM to prove

* one missed 15-minute window eliminates the old current player exactly once
  and grants the *new* current player a full 15-minute window from PG ``now``;
* a completed terminal projects its wins once and recovery never re-settles;
* the PG scope advisory lock is observable through ``backend_pid`` /
  ``wait_for_blocked``;
* a retention fixture built from real terminal lifecycles (completed /
  cancelled / expired / failed) is schema- and FK-valid.

The remaining cases are **RED** for the still-missing
``komari_bot.plugins.komari_roulette.maintenance`` seam (recovery pagination,
retention boundaries, job registration, no-Redis dependency).  They fail with
``ModuleNotFoundError`` — never with a wrong-fixture assertion.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import AsyncExitStack
from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from sqlalchemy import text

from .command_support import (
    PG_REQUIRED,
    Scope,
    backend_pid,
    command_factory,
    hold_group_lock,
    observation,
    request,
    seed_binding,
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
from .tsk279_support import (
    MAINTENANCE_MODULE,
    RUNTIME_MODULE,
    Tsk279Harness,
    age_game,
    harness_fixture_body,
    insert_waiting_game,
    load_module,
    load_symbol,
    scope_counts,
    seed_aged_receipt,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

pytestmark = [pytest.mark.asyncio]

_TURN = timedelta(minutes=15)
_DAY = timedelta(days=1)


def _maintenance_api() -> dict[str, Any]:
    return {
        name: load_symbol(MAINTENANCE_MODULE, name)
        for name in (
            "CLEANUP_JOB_ID",
            "RECOVERY_JOB_ID",
            "RecoveryTickResult",
            "RouletteMaintenance",
            "register_maintenance_jobs",
            "unregister_maintenance_jobs",
        )
    }


def _allow_only(allowed: set[tuple[str, str]]) -> Any:
    """A group-level admission gate (no member identity involved)."""

    def gate(app_id: str, group_openid: str) -> bool:
        return (app_id, group_openid) in allowed

    return gate


async def _game_observation(harness: Tsk279Harness, current: Any) -> Any:
    """The real observe_current snapshot the active-write guard requires."""

    from .command_support import observation

    row = await current_game_row(harness.session_factory, current)
    assert row is not None
    return observation(
        game_id=str(row["game_id"]),
        state_revision=int(row["state_revision"]),
        turn_seq=int(row["turn_seq"]),
    )


@pytest.fixture
async def harness() -> AsyncIterator[Tsk279Harness]:
    async for current in harness_fixture_body():
        yield current


# ---------------------------------------------------------------------------
# GREEN probes: the real deep entry and its data
# ---------------------------------------------------------------------------


@PG_REQUIRED
async def test_advance_expired_eliminates_once_and_grants_full_window(
    harness: Tsk279Harness,
) -> None:
    async with harness.scope("advance-once") as current:
        members = await seed_players(harness.binding_manager, current, 3)
        service = service_for(harness, random_source=CountingRandom())
        await create_waiting(service, current)
        await join_player(service, current, members[1], "ao-join-2")
        await join_player(service, current, members[2], "ao-join-3")
        await start_game(service, current, members[0], "ao-start")
        row = await current_game_row(harness.session_factory, current)
        assert row is not None
        old_current = int(row["current_player_seq"])
        before = await scope_counts(harness.session_factory, current)
        async with harness.session_factory() as session:
            # Simulate a long stop: two whole missed windows in the past.
            await session.execute(
                text(
                    "UPDATE komari_roulette_games SET turn_deadline_at = "
                    "clock_timestamp() - interval '2 hours' "
                    "WHERE game_id = :game_id"
                ),
                {"game_id": str(row["game_id"])},
            )
            await session.commit()

        advance = await service.advance_expired(current.group)

        assert advance.changed is True
        assert advance.receipt_id is None
        assert advance.result_code == "turn_expired"
        async with harness.session_factory() as session:
            window = await session.scalar(
                text(
                    "SELECT turn_deadline_at - clock_timestamp() "
                    "FROM komari_roulette_games WHERE game_id = :game_id"
                ),
                {"game_id": str(row["game_id"])},
            )
            eliminated_rows = (
                await session.execute(
                    text(
                        "SELECT join_seq FROM komari_roulette_players "
                        "WHERE game_id = :game_id AND eliminated_at IS NOT NULL"
                    ),
                    {"game_id": str(row["game_id"])},
                )
            ).scalars().all()
        assert [int(seq) for seq in eliminated_rows] == [old_current]
        assert _TURN - timedelta(seconds=90) <= window <= _TURN + timedelta(
            seconds=90
        )
        # The background advance is not a command: no receipt, no fulfillment,
        # no platform send (there is no legal inbound msg_id to answer).
        after = await scope_counts(harness.session_factory, current)
        assert (
            after["komari_roulette_command_receipts"]
            == before["komari_roulette_command_receipts"]
        )
        assert after["fulfillments"] == before["fulfillments"]


@PG_REQUIRED
async def test_completed_wins_project_once_and_recovery_does_not_resettle(
    harness: Tsk279Harness,
) -> None:
    async with harness.scope("wins-once") as current:
        members = await seed_players(harness.binding_manager, current, 2)
        service = service_for(harness, random_source=CountingRandom())
        await create_waiting(service, current)
        await join_player(service, current, members[1], "wo-join-2")
        await start_game(service, current, members[0], "wo-start")
        ended = await service.execute_group_command(
            request(
                current,
                "wo-forfeit",
                command_factory("forfeit"),
                member_openid=members[0],
            ),
            observation=await _game_observation(harness, current),
        )
        assert ended.result_code == "forfeited"
        params = {"app_id": current.app_id, "grp": current.group_openid}
        async with harness.session_factory() as session:
            wins = await session.scalar(
                text(
                    "SELECT wins FROM komari_roulette_leaderboard "
                    "WHERE app_id = :app_id AND group_openid = :grp"
                ),
                params,
            )
            results = await session.scalar(
                text(
                    "SELECT count(*) FROM komari_roulette_results "
                    "WHERE app_id = :app_id AND group_openid = :grp "
                    "AND lifecycle = 'completed'"
                ),
                params,
            )
        assert int(wins or 0) == 1
        assert int(results or 0) == 1

        # Recovery after terminal finds no current game and must not touch wins.
        again = await service.advance_expired(current.group)
        assert again.changed is False
        assert again.result_code == "no_active_game"
        async with harness.session_factory() as session:
            wins_after = await session.scalar(
                text(
                    "SELECT wins FROM komari_roulette_leaderboard "
                    "WHERE app_id = :app_id AND group_openid = :grp"
                ),
                params,
            )
        assert int(wins_after or 0) == 1


@PG_REQUIRED
async def test_scope_lock_observation_blocks_a_second_writer(
    harness: Tsk279Harness,
) -> None:
    async with harness.scope("lock-observe") as current:
        await seed_binding(harness.binding_manager, current, 1)
        service = service_for(harness, random_source=CountingRandom())
        await create_waiting(service, current)
        async with harness.session_factory() as blocker:
            await blocker.begin()
            blocker_pid = await backend_pid(blocker)
            await hold_group_lock(blocker, current)
            waiter = asyncio.create_task(service.advance_expired(current.group))
            try:
                await wait_for_blocked(harness.session_factory, blocker_pid)
            finally:
                await blocker.commit()
            async with asyncio.timeout(5):
                result = await waiter
        assert result.result_code in {"not_expired", "no_active_game"}


@PG_REQUIRED
async def test_retention_fixture_has_real_terminal_lifecycles(
    harness: Tsk279Harness,
) -> None:
    async with AsyncExitStack() as stack:
        service = service_for(harness, random_source=CountingRandom())
        terminals: dict[str, str] = {}
        for kind in ("completed", "cancelled", "expired", "failed"):
            current = await stack.enter_async_context(harness.scope(f"term-{kind}"))
            members = await seed_players(harness.binding_manager, current, 2)
            terminals[kind] = await _seed_terminal(
                harness, service, current, kind, members
            )
            await age_game(
                harness.session_factory,
                game_id=terminals[kind],
                age_seconds=int((_DAY * 31).total_seconds()),
            )
        async with harness.session_factory() as session:
            for kind, game_id in terminals.items():
                lifecycle = await session.scalar(
                    text(
                        "SELECT lifecycle FROM komari_roulette_games "
                        "WHERE game_id = :game_id"
                    ),
                    {"game_id": game_id},
                )
                result_lifecycle = await session.scalar(
                    text(
                        "SELECT lifecycle FROM komari_roulette_results "
                        "WHERE game_id = :game_id"
                    ),
                    {"game_id": game_id},
                )
                result_players = await session.scalar(
                    text(
                        "SELECT count(*) FROM komari_roulette_result_players "
                        "WHERE game_id = :game_id"
                    ),
                    {"game_id": game_id},
                )
                runtime_players = await session.scalar(
                    text(
                        "SELECT count(*) FROM komari_roulette_players "
                        "WHERE game_id = :game_id"
                    ),
                    {"game_id": game_id},
                )
                assert lifecycle == kind
                assert result_lifecycle == kind
                assert int(result_players or 0) >= 2
                assert int(runtime_players or 0) == 0


@PG_REQUIRED
async def test_sql_seeded_waiting_game_is_really_advanced(
    harness: Tsk279Harness,
) -> None:
    """The raw-SQL waiting fixture is schema- and aggregate-valid."""

    async with harness.scope("insert-probe") as current:
        await insert_waiting_game(
            harness.session_factory,
            game_id=str(uuid4()),
            app_id=current.app_id,
            group_openid=current.group_openid,
            member_openid=current.member_openid,
            deadline_age_seconds=60,
        )
        service = service_for(harness, random_source=CountingRandom())
        advance = await service.advance_expired(current.group)
        assert advance.changed is True
        assert advance.result_code == "waiting_game_expired"
        assert advance.receipt_id is None
        row = await current_game_row(harness.session_factory, current)
        assert row is not None
        assert row["lifecycle"] == "expired"


async def _seed_terminal(
    harness: Tsk279Harness,
    service: Any,
    current: Any,
    kind: str,
    members: Sequence[str],
) -> str:
    await create_waiting(service, current, message_id=f"{kind}-create")
    await join_player(service, current, members[1], f"{kind}-join-2")
    if kind == "cancelled":
        await service.execute_group_command(
            request(
                current,
                f"{kind}-cancel",
                command_factory("cancel"),
                member_openid=members[0],
            )
        )
    elif kind == "completed":
        await start_game(service, current, members[0], f"{kind}-start")
        await service.execute_group_command(
            request(
                current,
                f"{kind}-forfeit",
                command_factory("forfeit"),
                member_openid=members[0],
            ),
            observation=await _game_observation(harness, current),
        )
    elif kind == "expired":
        row = await current_game_row(harness.session_factory, current)
        assert row is not None
        async with harness.session_factory() as session:
            await session.execute(
                text(
                    "UPDATE komari_roulette_games SET waiting_expires_at = "
                    "clock_timestamp() - interval '1 minute' "
                    "WHERE game_id = :game_id"
                ),
                {"game_id": str(row["game_id"])},
            )
            await session.commit()
        await service.advance_expired(current.group)
    elif kind == "failed":
        await start_game(service, current, members[0], f"{kind}-start")
        row = await current_game_row(harness.session_factory, current)
        assert row is not None
        async with harness.session_factory() as session:
            # Keep the seats but make the aggregate state invalid, so the
            # confirmed-corrupt path projects a failed result with its players.
            await session.execute(
                text(
                    "UPDATE komari_roulette_games SET phase = NULL "
                    "WHERE game_id = :game_id"
                ),
                {"game_id": str(row["game_id"])},
            )
            await session.commit()
        await service.advance_expired(current.group)
    else:  # pragma: no cover - guard for a wrong fixture
        message = f"unknown terminal kind {kind!r}"
        raise AssertionError(message)
    row = await current_game_row(harness.session_factory, current)
    assert row is not None
    return str(row["game_id"])


# ---------------------------------------------------------------------------
# RED: recovery scan (restricted groups, pagination, no background send)
# ---------------------------------------------------------------------------


async def test_maintenance_seam_exposes_recovery_and_cleanup() -> None:
    api = _maintenance_api()
    assert api["RECOVERY_JOB_ID"] != api["CLEANUP_JOB_ID"]


@PG_REQUIRED
async def test_recovery_skips_restricted_groups_and_creates_no_send(
    harness: Tsk279Harness,
) -> None:
    api = _maintenance_api()
    async with harness.app("recovery-gate") as app_id:
        service = service_for(harness, random_source=CountingRandom())
        restricted = f"grp-restricted-{uuid4().hex}"
        allowed = f"grp-allowed-{uuid4().hex}"
        await insert_waiting_game(
            harness.session_factory,
            game_id=str(uuid4()),
            app_id=app_id,
            group_openid=restricted,
            member_openid=f"m-{restricted}",
        )
        await insert_waiting_game(
            harness.session_factory,
            game_id=str(uuid4()),
            app_id=app_id,
            group_openid=allowed,
            member_openid=f"m-{allowed}",
        )
        maintenance = api["RouletteMaintenance"](
            session_factory=harness.session_factory,
            service=service,
            admission=_allow_only({(app_id, allowed)}),
        )
        async with harness.session_factory() as session:
            receipts_before = int(
                await session.scalar(
                    text(
                        "SELECT count(*) FROM komari_roulette_command_receipts "
                        "WHERE app_id = :app_id"
                    ),
                    {"app_id": app_id},
                )
                or 0
            )
        tick = await maintenance.advance_due(batch_size=100)
        assert tick.skipped_restricted >= 1
        async with harness.session_factory() as session:
            states = {
                str(row[0]): str(row[1])
                for row in (
                    await session.execute(
                        text(
                            "SELECT group_openid, lifecycle "
                            "FROM komari_roulette_games WHERE app_id = :app_id"
                        ),
                        {"app_id": app_id},
                    )
                ).all()
            }
            receipts_after = int(
                await session.scalar(
                    text(
                        "SELECT count(*) FROM komari_roulette_command_receipts "
                        "WHERE app_id = :app_id"
                    ),
                    {"app_id": app_id},
                )
                or 0
            )
        assert states[allowed] == "expired"
        assert states[restricted] == "waiting"
        assert receipts_after == receipts_before


@PG_REQUIRED
async def test_recovery_paginates_past_a_restricted_batch(
    harness: Tsk279Harness,
) -> None:
    api = _maintenance_api()
    async with harness.app("recovery-page") as app_id:
        service = service_for(harness, random_source=CountingRandom())
        allowed = f"grp-page-allowed-{uuid4().hex}"
        for index in range(100):
            group_openid = f"grp-page-restricted-{index}-{uuid4().hex}"
            await insert_waiting_game(
                harness.session_factory,
                game_id=str(uuid4()),
                app_id=app_id,
                group_openid=group_openid,
                member_openid=f"m-{group_openid}",
                deadline_age_seconds=3600,
            )
        await insert_waiting_game(
            harness.session_factory,
            game_id=str(uuid4()),
            app_id=app_id,
            group_openid=allowed,
            member_openid=f"m-{allowed}",
            deadline_age_seconds=60,
        )
        maintenance = api["RouletteMaintenance"](
            session_factory=harness.session_factory,
            service=service,
            admission=_allow_only({(app_id, allowed)}),
        )
        lifecycle: object = None
        ticks = 0
        for _ in range(3):
            ticks += 1
            await maintenance.advance_due(batch_size=100)
            async with harness.session_factory() as session:
                lifecycle = await session.scalar(
                    text(
                        "SELECT lifecycle FROM komari_roulette_games "
                        "WHERE app_id = :app_id AND group_openid = :group_openid"
                    ),
                    {"app_id": app_id, "group_openid": allowed},
                )
            if lifecycle == "expired":
                break
        assert lifecycle == "expired", (
            "bounded pagination must reach an allowed group behind a full "
            "batch of restricted groups within a few ticks"
        )
        assert ticks <= 3


# ---------------------------------------------------------------------------
# RED: retention cleanup (7d receipts, 30d non-win terminals, permanent wins)
# ---------------------------------------------------------------------------


@PG_REQUIRED
async def test_cleanup_deletes_aged_receipts_and_keeps_recent(
    harness: Tsk279Harness,
) -> None:
    api = _maintenance_api()
    async with harness.scope("cleanup-receipts") as current:
        aged = await seed_aged_receipt(
            harness.session_factory,
            current,
            inbound_msg_id="aged-old",
            age_seconds=int((_DAY * 7).total_seconds()) + 60,
        )
        recent = await seed_aged_receipt(
            harness.session_factory,
            current,
            inbound_msg_id="aged-recent",
            age_seconds=int((_DAY * 6).total_seconds()),
        )
        maintenance = api["RouletteMaintenance"](
            session_factory=harness.session_factory,
            service=service_for(harness, random_source=CountingRandom()),
            admission=_allow_only(set()),
        )
        await maintenance.cleanup_retention(batch_size=100)
        async with harness.session_factory() as session:
            remaining = set(
                (
                    await session.execute(
                        text(
                            "SELECT receipt_id FROM "
                            "komari_roulette_command_receipts "
                            "WHERE app_id = :app_id AND group_openid = :grp"
                        ),
                        {"app_id": current.app_id, "grp": current.group_openid},
                    )
                ).scalars().all()
            )
        assert aged not in remaining
        assert recent in remaining


@PG_REQUIRED
async def test_cleanup_deletes_nonwin_terminals_keeps_completed_and_wins(
    harness: Tsk279Harness,
) -> None:
    api = _maintenance_api()
    async with harness.scope("cleanup-terminals") as current:
        members = await seed_players(harness.binding_manager, current, 2)
        service = service_for(harness, random_source=CountingRandom())
        completed = await _seed_terminal(
            harness, service, current, "completed", members
        )
        cancelled = await _seed_terminal(
            harness, service, current, "cancelled", members
        )
        expired = await _seed_terminal(harness, service, current, "expired", members)
        await age_game(
            harness.session_factory,
            game_id=completed,
            age_seconds=int((_DAY * 40).total_seconds()),
        )
        for game_id in (cancelled, expired):
            await age_game(
                harness.session_factory,
                game_id=game_id,
                age_seconds=int((_DAY * 31).total_seconds()),
            )
        maintenance = api["RouletteMaintenance"](
            session_factory=harness.session_factory,
            service=service,
            admission=_allow_only(set()),
        )
        await maintenance.cleanup_retention(batch_size=100)
        params = {"app_id": current.app_id, "grp": current.group_openid}
        async with harness.session_factory() as session:
            games = set(
                (
                    await session.execute(
                        text(
                            "SELECT game_id FROM komari_roulette_games "
                            "WHERE app_id = :app_id AND group_openid = :grp"
                        ),
                        params,
                    )
                ).scalars().all()
            )
            wins = await session.scalar(
                text(
                    "SELECT wins FROM komari_roulette_leaderboard "
                    "WHERE app_id = :app_id AND group_openid = :grp"
                ),
                params,
            )
        assert completed in games
        assert cancelled not in games
        assert expired not in games
        assert int(wins or 0) == 1


@PG_REQUIRED
async def test_cleanup_is_batch_bounded_and_reentrant(
    harness: Tsk279Harness,
) -> None:
    api = _maintenance_api()
    async with harness.scope("cleanup-batch") as current:
        for index in range(3):
            await seed_aged_receipt(
                harness.session_factory,
                current,
                inbound_msg_id=f"batch-{index}",
                age_seconds=int((_DAY * 8).total_seconds()),
            )
        maintenance = api["RouletteMaintenance"](
            session_factory=harness.session_factory,
            service=service_for(harness, random_source=CountingRandom()),
            admission=_allow_only(set()),
        )
        first = await maintenance.cleanup_retention(batch_size=2)
        assert isinstance(first.more_pending, bool)
        for _ in range(3):
            await maintenance.cleanup_retention(batch_size=2)
        async with harness.session_factory() as session:
            remaining = int(
                await session.scalar(
                    text(
                        "SELECT count(*) FROM komari_roulette_command_receipts "
                        "WHERE app_id = :app_id AND group_openid = :grp"
                    ),
                    {"app_id": current.app_id, "grp": current.group_openid},
                )
                or 0
            )
        assert remaining == 0


@PG_REQUIRED
async def test_cleanup_does_not_starve_eligible_behind_protected(
    harness: Tsk279Harness,
) -> None:
    api = _maintenance_api()
    async with AsyncExitStack() as stack:
        current = await stack.enter_async_context(harness.scope("starve"))
        app_id = await stack.enter_async_context(harness.app("starve-games"))
        aged = await seed_aged_receipt(
            harness.session_factory,
            current,
            inbound_msg_id="starve-old",
            age_seconds=int((_DAY * 8).total_seconds()),
        )
        for index in range(120):
            group_openid = f"grp-starve-{index}-{uuid4().hex}"
            await insert_waiting_game(
                harness.session_factory,
                game_id=str(uuid4()),
                app_id=app_id,
                group_openid=group_openid,
                member_openid=f"m-{group_openid}",
                deadline_age_seconds=60,
            )
        maintenance = api["RouletteMaintenance"](
            session_factory=harness.session_factory,
            service=service_for(harness, random_source=CountingRandom()),
            admission=_allow_only(set()),
        )
        await maintenance.cleanup_retention(batch_size=10)
        async with harness.session_factory() as session:
            still_there = await session.scalar(
                text(
                    "SELECT count(*) FROM komari_roulette_command_receipts "
                    "WHERE receipt_id = :receipt_id"
                ),
                {"receipt_id": aged},
            )
        assert int(still_there or 0) == 0


# ---------------------------------------------------------------------------
# RED: scheduler wiring (fixed ids, throttle only) and no-Redis dependency
# ---------------------------------------------------------------------------


async def test_maintenance_jobs_registered_with_throttle_and_deploy_timezone() -> None:
    api = _maintenance_api()
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    # The deployment timezone is trusted from the scheduler config, never by
    # mutating the host clock/timezone.
    deploy_timezone = "Asia/Shanghai"
    scheduler = AsyncIOScheduler(timezone=deploy_timezone)

    class _MaintenanceStub:
        async def advance_due(self, **_kwargs: Any) -> None:
            return None

        async def cleanup_retention(self, **_kwargs: Any) -> None:
            return None

    api["register_maintenance_jobs"](scheduler, _MaintenanceStub())
    recovery = scheduler.get_job(api["RECOVERY_JOB_ID"])
    cleanup = scheduler.get_job(api["CLEANUP_JOB_ID"])
    assert recovery is not None
    assert cleanup is not None
    assert recovery.trigger.interval == timedelta(seconds=60)
    assert recovery.coalesce is True
    assert recovery.max_instances == 1
    assert str(cleanup.trigger.fields[5]) == "hour='4'"
    assert str(cleanup.trigger.timezone) == deploy_timezone
    assert cleanup.coalesce is True
    assert cleanup.max_instances == 1
    api["unregister_maintenance_jobs"](scheduler)
    assert scheduler.get_job(api["RECOVERY_JOB_ID"]) is None
    assert scheduler.get_job(api["CLEANUP_JOB_ID"]) is None


async def test_maintenance_does_not_require_redis() -> None:
    before = set(sys.modules)
    module = load_module(MAINTENANCE_MODULE)
    assert module is not None
    new_modules = set(sys.modules) - before
    assert not any(name.split(".")[0] == "redis" for name in new_modules)


# ---------------------------------------------------------------------------
# Worker concurrency on one real group lock (requirement 4)
# ---------------------------------------------------------------------------


@PG_REQUIRED
async def test_two_maintenance_instances_advance_one_turn_exactly_once(
    harness: Tsk279Harness,
) -> None:
    api = _maintenance_api()
    async with harness.scope("maint-race-turn") as current:
        members = await seed_players(harness.binding_manager, current, 3)
        service = service_for(harness, random_source=CountingRandom())
        await create_waiting(service, current)
        await join_player(service, current, members[1], "mrt-join-2")
        await join_player(service, current, members[2], "mrt-join-3")
        await start_game(service, current, members[0], "mrt-start")
        row = await current_game_row(harness.session_factory, current)
        assert row is not None
        old_current = int(row["current_player_seq"])
        async with harness.session_factory() as session:
            await session.execute(
                text(
                    "UPDATE komari_roulette_games SET turn_deadline_at = "
                    "clock_timestamp() - interval '2 hours' WHERE game_id = :gid"
                ),
                {"gid": str(row["game_id"])},
            )
            await session.commit()

        allowed = {(current.app_id, current.group_openid)}
        first = api["RouletteMaintenance"](
            session_factory=harness.session_factory,
            service=service,
            admission=_allow_only(allowed),
        )
        second = api["RouletteMaintenance"](
            session_factory=harness.session_factory,
            service=service,
            admission=_allow_only(allowed),
        )
        barrier = asyncio.Barrier(2)

        async def run(maintenance: Any) -> Any:
            await barrier.wait()
            return await maintenance.advance_due(batch_size=100)

        ticks = await asyncio.gather(run(first), run(second))
        advanced = sum(int(getattr(tick, "advanced", 0)) for tick in ticks)
        assert advanced == 1

        async with harness.session_factory() as session:
            window = await session.scalar(
                text(
                    "SELECT turn_deadline_at - clock_timestamp() "
                    "FROM komari_roulette_games WHERE game_id = :gid"
                ),
                {"gid": str(row["game_id"])},
            )
            eliminated = (
                (
                    await session.execute(
                        text(
                            "SELECT join_seq FROM komari_roulette_players "
                            "WHERE game_id = :gid AND eliminated_at IS NOT NULL"
                        ),
                        {"gid": str(row["game_id"])},
                    )
                )
                .scalars()
                .all()
            )
        assert [int(seq) for seq in eliminated] == [old_current]
        assert _TURN - timedelta(seconds=120) <= window <= _TURN + timedelta(seconds=120)


@PG_REQUIRED
async def test_two_maintenance_instances_settle_terminal_wins_once(
    harness: Tsk279Harness,
) -> None:
    api = _maintenance_api()
    async with harness.scope("maint-race-wins") as current:
        members = await seed_players(harness.binding_manager, current, 2)
        service = service_for(harness, random_source=CountingRandom())
        await create_waiting(service, current)
        await join_player(service, current, members[1], "mrw-join-2")
        await start_game(service, current, members[0], "mrw-start")
        row = await current_game_row(harness.session_factory, current)
        assert row is not None
        async with harness.session_factory() as session:
            await session.execute(
                text(
                    "UPDATE komari_roulette_games SET turn_deadline_at = "
                    "clock_timestamp() - interval '2 hours' WHERE game_id = :gid"
                ),
                {"gid": str(row["game_id"])},
            )
            await session.commit()

        allowed = {(current.app_id, current.group_openid)}
        first = api["RouletteMaintenance"](
            session_factory=harness.session_factory,
            service=service,
            admission=_allow_only(allowed),
        )
        second = api["RouletteMaintenance"](
            session_factory=harness.session_factory,
            service=service,
            admission=_allow_only(allowed),
        )
        barrier = asyncio.Barrier(2)

        async def run(maintenance: Any) -> Any:
            await barrier.wait()
            return await maintenance.advance_due(batch_size=100)

        await asyncio.gather(run(first), run(second))

        async with harness.session_factory() as session:
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
            results = await session.scalar(
                text(
                    "SELECT count(*) FROM komari_roulette_results "
                    "WHERE app_id = :app_id AND group_openid = :group_openid"
                ),
                {
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                },
            )
            result_players = await session.scalar(
                text(
                    "SELECT count(*) FROM komari_roulette_result_players rp "
                    "JOIN komari_roulette_results r ON r.result_id = rp.result_id "
                    "WHERE r.app_id = :app_id AND r.group_openid = :group_openid"
                ),
                {
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                },
            )
        assert int(wins or 0) == 1
        assert int(results or 0) == 1
        assert int(result_players or 0) == 2


@PG_REQUIRED
async def test_command_action_and_maintenance_locked_race_commits_one_effect(
    harness: Tsk279Harness,
) -> None:
    api = _maintenance_api()
    async with harness.scope("maint-race-action") as current:
        members = await seed_players(harness.binding_manager, current, 2)
        service = service_for(harness, random_source=CountingRandom())
        await create_waiting(service, current)
        await join_player(service, current, members[1], "mra-join-2")
        await start_game(service, current, members[0], "mra-start")
        row = await current_game_row(harness.session_factory, current)
        assert row is not None
        actor = members[int(row["current_player_seq"]) - 1]
        observed = observation(
            game_id=str(row["game_id"]),
            state_revision=int(row["state_revision"]),
            turn_seq=int(row["turn_seq"]),
        )
        async with harness.session_factory() as session:
            await session.execute(
                text(
                    "UPDATE komari_roulette_games SET turn_deadline_at = "
                    "clock_timestamp() - interval '2 hours' WHERE game_id = :gid"
                ),
                {"gid": str(row["game_id"])},
            )
            await session.commit()

        maintenance = api["RouletteMaintenance"](
            session_factory=harness.session_factory,
            service=service,
            admission=_allow_only({(current.app_id, current.group_openid)}),
        )
        barrier = asyncio.Barrier(2)

        async def run_action() -> Any:
            await barrier.wait()
            return await service.execute_group_command(
                request(
                    current,
                    "mra-forfeit",
                    command_factory("forfeit"),
                    member_openid=actor,
                ),
                observation=observed,
            )

        async def run_maintenance() -> Any:
            await barrier.wait()
            return await maintenance.advance_due(batch_size=100)

        receipt, tick = await asyncio.gather(run_action(), run_maintenance())
        del tick
        async with harness.session_factory() as session:
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
            completed = await session.scalar(
                text(
                    "SELECT count(*) FROM komari_roulette_results "
                    "WHERE app_id = :app_id AND group_openid = :group_openid "
                    "AND completion_reason IS NOT NULL"
                ),
                {
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                },
            )
            result_players = await session.scalar(
                text(
                    "SELECT count(*) FROM komari_roulette_result_players rp "
                    "JOIN komari_roulette_results r ON r.result_id = rp.result_id "
                    "WHERE r.app_id = :app_id AND r.group_openid = :group_openid"
                ),
                {
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                },
            )
        # Whichever worker took the group lock first, exactly one terminal effect
        # and exactly one win commits; the loser observes a clean conflict/no-op.
        assert receipt.result_code in {"forfeited", "state_conflict"}
        assert int(wins or 0) == 1
        assert int(completed or 0) == 1
        assert int(result_players or 0) == 2


# ---------------------------------------------------------------------------
# Admission / runtime change while waiting on the group lock (requirement 5)
# ---------------------------------------------------------------------------


def _admit_everything() -> Any:
    """A real ``AdmissionResult`` lookup for a runtime that only starts/closes."""

    from komari_bot.plugins.group_admission.contracts import (
        AdmissionIntent,
        AdmissionQualification,
        AdmissionResult,
    )

    def lookup(
        associated_group_ids: Sequence[str],
        *,
        intent: AdmissionIntent,
    ) -> AdmissionResult:
        del associated_group_ids
        assert intent is AdmissionIntent.BUSINESS
        return AdmissionResult(
            qualification=AdmissionQualification.BUSINESS,
            effective_revision=1,
            reason_code="policy_admitted",
        )

    return lookup


class _EnabledConfig:
    """Minimal read API mirroring ``ConfigManager`` for runtime start/close."""

    def get(self) -> Any:
        return SimpleNamespace(plugin_enable=True)

    async def get_async(self) -> Any:
        return SimpleNamespace(plugin_enable=True)

    async def initialize_async(self) -> Any:
        return SimpleNamespace(plugin_enable=True)


class _NoopRecovery:
    async def run_recovery_tick(self) -> Any:
        return SimpleNamespace(advanced=0)

    async def close(self) -> None:
        return None


@PG_REQUIRED
async def test_maintenance_rechecks_admission_after_group_lock_wait(
    harness: Tsk279Harness,
) -> None:
    api = _maintenance_api()
    from nonebot import require

    require("nonebot_plugin_orm")
    import nonebot_plugin_orm as orm_module

    probe = orm_module.get_session()
    await probe.close()
    shared_factory = orm_module.get_session

    async with harness.app("post-lock-admission") as app_id:
        service = service_for(harness, random_source=CountingRandom())
        group_openid = f"grp-postlock-{uuid4().hex}"
        await insert_waiting_game(
            harness.session_factory,
            game_id=str(uuid4()),
            app_id=app_id,
            group_openid=group_openid,
            member_openid=f"m-{group_openid}",
            deadline_age_seconds=60,
        )
        current = Scope(
            app_id=app_id,
            group_openid=group_openid,
            member_openid=f"m-{group_openid}",
        )
        gate = {"allowed": True}

        def admission(_app_id: str, _group_openid: str) -> bool:
            return gate["allowed"]

        maintenance = api["RouletteMaintenance"](
            session_factory=shared_factory,
            service=service,
            admission=admission,
        )
        async with harness.session_factory() as blocker:
            await blocker.begin()
            blocker_pid = await backend_pid(blocker)
            await hold_group_lock(blocker, current)
            tick_task = asyncio.create_task(maintenance.advance_due(batch_size=100))
            try:
                await wait_for_blocked(harness.session_factory, blocker_pid)
                # The group turns restricted while the worker waits on the lock.
                gate["allowed"] = False
            finally:
                await blocker.commit()
            with asyncio.timeout(10):
                tick = await tick_task
        async with harness.session_factory() as session:
            lifecycle = await session.scalar(
                text(
                    "SELECT lifecycle FROM komari_roulette_games "
                    "WHERE app_id = :app_id AND group_openid = :group_openid"
                ),
                {"app_id": app_id, "group_openid": group_openid},
            )
        assert lifecycle == "waiting"
        assert int(getattr(tick, "advanced", 0)) == 0


@PG_REQUIRED
async def test_maintenance_rechecks_closed_runtime_after_group_lock_wait(
    harness: Tsk279Harness,
) -> None:
    api = _maintenance_api()
    runtime_cls = load_symbol(RUNTIME_MODULE, "RouletteRuntime")
    from nonebot import require

    require("nonebot_plugin_orm")
    import nonebot_plugin_orm as orm_module

    probe = orm_module.get_session()
    await probe.close()
    shared_factory = orm_module.get_session

    async with harness.app("post-lock-runtime") as app_id:
        service = service_for(harness, random_source=CountingRandom())
        group_openid = f"grp-postlock-rt-{uuid4().hex}"
        await insert_waiting_game(
            harness.session_factory,
            game_id=str(uuid4()),
            app_id=app_id,
            group_openid=group_openid,
            member_openid=f"m-{group_openid}",
            deadline_age_seconds=60,
        )
        current = Scope(
            app_id=app_id,
            group_openid=group_openid,
            member_openid=f"m-{group_openid}",
        )
        runtime = runtime_cls(
            config_manager=_EnabledConfig(),
            recovery=_NoopRecovery(),
            admission=_admit_everything(),
        )
        await runtime.start()
        assert runtime.accepting is True

        def admission(candidate_app: str, candidate_group: str) -> bool:
            return runtime.accepting and (candidate_app, candidate_group) == (
                app_id,
                group_openid,
            )

        maintenance = api["RouletteMaintenance"](
            session_factory=shared_factory,
            service=service,
            admission=admission,
        )
        async with harness.session_factory() as blocker:
            await blocker.begin()
            blocker_pid = await backend_pid(blocker)
            await hold_group_lock(blocker, current)
            tick_task = asyncio.create_task(maintenance.advance_due(batch_size=100))
            try:
                await wait_for_blocked(harness.session_factory, blocker_pid)
                # The real runtime closes while the worker waits on the lock.
                await runtime.close()
                assert runtime.accepting is False
            finally:
                await blocker.commit()
            with asyncio.timeout(10):
                tick = await tick_task
        async with harness.session_factory() as session:
            lifecycle = await session.scalar(
                text(
                    "SELECT lifecycle FROM komari_roulette_games "
                    "WHERE app_id = :app_id AND group_openid = :group_openid"
                ),
                {"app_id": app_id, "group_openid": group_openid},
            )
        assert lifecycle == "waiting"
        assert int(getattr(tick, "advanced", 0)) == 0


@PG_REQUIRED
async def test_two_player_expiry_settles_one_win(harness: Tsk279Harness) -> None:
    """Fixture probe: an expired two-player turn really settles as a win."""

    async with harness.scope("expire-win-probe") as current:
        members = await seed_players(harness.binding_manager, current, 2)
        service = service_for(harness, random_source=CountingRandom())
        await create_waiting(service, current)
        await join_player(service, current, members[1], "ewp-join-2")
        await start_game(service, current, members[0], "ewp-start")
        row = await current_game_row(harness.session_factory, current)
        assert row is not None
        async with harness.session_factory() as session:
            await session.execute(
                text(
                    "UPDATE komari_roulette_games SET turn_deadline_at = "
                    "clock_timestamp() - interval '2 hours' WHERE game_id = :gid"
                ),
                {"gid": str(row["game_id"])},
            )
            await session.commit()

        advance = await service.advance_expired(current.group)
        assert advance.changed is True
        assert advance.result_code == "turn_expired"
        async with harness.session_factory() as session:
            lifecycle = await session.scalar(
                text(
                    "SELECT lifecycle FROM komari_roulette_games WHERE game_id = :gid"
                ),
                {"gid": str(row["game_id"])},
            )
            wins = await session.scalar(
                text(
                    "SELECT wins FROM komari_roulette_leaderboard "
                    "WHERE app_id = :app_id AND group_openid = :group_openid"
                ),
                {"app_id": current.app_id, "group_openid": current.group_openid},
            )
        assert lifecycle == "completed"
        assert int(wins or 0) == 1
