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

The remaining cases cover the real ``komari_bot.plugins.komari_roulette.maintenance``
seam (recovery pagination, retention boundaries, job registration, no-Redis
dependency) plus the two runtime-facing post-lock rechecks, which stay RED
until ``komari_bot.plugins.komari_roulette.runtime`` is implemented and fail
with ``ModuleNotFoundError`` — never with a wrong-fixture assertion.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class _RecoveryDrive:
    """Aggregated, bounded recovery walk toward this case's own candidate."""

    scanned: int
    advanced: int
    skipped_restricted: int
    failed: int
    pages: int
    settled: bool


@dataclass(frozen=True, slots=True)
class _ExpiredTurn:
    """A real two-player active game whose turn deadline is two hours past."""

    service: Any
    actor: str
    observation: Any


async def _group_lifecycle(
    harness: Tsk279Harness,
    *,
    app_id: str,
    group_openid: str,
) -> str | None:
    async with harness.session_factory() as session:
        value = await session.scalar(
            text(
                "SELECT lifecycle FROM komari_roulette_games "
                "WHERE app_id = :app_id AND group_openid = :group_openid "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"app_id": app_id, "group_openid": group_openid},
        )
    return None if value is None else str(value)


async def _recovery_page_budget(
    harness: Tsk279Harness,
    *,
    batch_size: int,
    margin: int = 4,
) -> int:
    """Bound the keyset walk by the *actual* number of due rows.

    Other suites leave due rows behind, so this case's candidate can sit behind
    several full pages.  The bound is derived from the real global due-row count
    (the production query is never narrowed to this case's scope), so the walk
    is bounded without deleting foreign rows or faking a private scan space.
    """

    async with harness.session_factory() as session:
        total = await session.scalar(
            text(
                "SELECT count(*) FROM komari_roulette_games "
                "WHERE lifecycle IN ('waiting', 'active') "
                "AND COALESCE(waiting_expires_at, turn_deadline_at) "
                "<= clock_timestamp()"
            )
        )
    pages = (int(total or 0) + batch_size - 1) // batch_size
    return pages + margin


async def _drive_recovery_until_settled(
    maintenance: Any,
    harness: Tsk279Harness,
    *,
    app_id: str,
    group_openid: str,
    batch_size: int = 100,
) -> _RecoveryDrive:
    """Walk the real keyset scan until this case's group settles (or wraps).

    The loop is driven by the *production* ``cursor``/``scanned`` values, so a
    page full of foreign or restricted groups is walked past instead of being
    deleted or filtered out.
    """

    budget = await _recovery_page_budget(harness, batch_size=batch_size)
    scanned = advanced = skipped = failed = 0
    pages = 0
    settled = False
    for _ in range(budget):
        tick = await maintenance.advance_due(batch_size=batch_size)
        pages += 1
        scanned += int(getattr(tick, "scanned", 0))
        advanced += int(getattr(tick, "advanced", 0))
        skipped += int(getattr(tick, "skipped_restricted", 0))
        failed += int(getattr(tick, "failed", 0))
        lifecycle = await _group_lifecycle(
            harness, app_id=app_id, group_openid=group_openid
        )
        if lifecycle not in {"waiting", "active"}:
            settled = True
            break
        if getattr(tick, "cursor", None) is None:
            # A partial page means the scan reached the end; the next call would
            # restart from the same first rows, so stop instead of looping.
            break
    return _RecoveryDrive(scanned, advanced, skipped, failed, pages, settled)


async def _cleanup_round_budget(
    harness: Tsk279Harness,
    *,
    batch_size: int,
    margin: int = 4,
) -> int:
    """Bound cleanup rounds by the *actual* aged-group count (no scope filter)."""

    async with harness.session_factory() as session:
        receipt_groups = int(
            await session.scalar(
                text(
                    "SELECT count(*) FROM ("
                    "SELECT app_id, group_openid FROM "
                    "komari_roulette_command_receipts "
                    "WHERE created_at < clock_timestamp() "
                    "- make_interval(days => 7) "
                    "GROUP BY app_id, group_openid) AS aged_receipt_groups"
                )
            )
            or 0
        )
        terminal_groups = int(
            await session.scalar(
                text(
                    "SELECT count(*) FROM ("
                    "SELECT app_id, group_openid FROM komari_roulette_games "
                    "WHERE lifecycle IN ('cancelled', 'expired', 'failed') "
                    "AND ended_at IS NOT NULL "
                    "AND ended_at < clock_timestamp() - make_interval(days => 30) "
                    "GROUP BY app_id, group_openid) AS aged_terminal_groups"
                )
            )
            or 0
        )
    groups = max(receipt_groups, terminal_groups)
    return (groups + batch_size - 1) // batch_size + margin


async def _drive_cleanup_until(
    maintenance: Any,
    harness: Tsk279Harness,
    *,
    done: Any,
    batch_size: int = 100,
) -> int:
    """Run bounded, reentrant cleanup rounds until ``done()`` holds."""

    budget = await _cleanup_round_budget(harness, batch_size=batch_size)
    for round_index in range(1, budget + 1):
        await maintenance.cleanup_retention(batch_size=batch_size)
        if await done():
            return round_index
    message = "bounded cleanup rounds did not reach this case's rows"
    raise AssertionError(message)


async def _terminal_projection_state(
    harness: Tsk279Harness,
    current: Any,
) -> dict[str, Any]:
    """Read the real terminal projection for one exact scope (read-only)."""

    params = {"app_id": current.app_id, "group_openid": current.group_openid}
    async with harness.session_factory() as session:
        results = (
            (
                await session.execute(
                    text(
                        "SELECT lifecycle, reason FROM komari_roulette_results "
                        "WHERE app_id = :app_id AND group_openid = :group_openid"
                    ),
                    params,
                )
            )
            .all()
        )
        game_lifecycle = await session.scalar(
            text(
                "SELECT lifecycle FROM komari_roulette_games "
                "WHERE app_id = :app_id AND group_openid = :group_openid "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            params,
        )
        wins = await session.scalar(
            text(
                "SELECT wins FROM komari_roulette_leaderboard "
                "WHERE app_id = :app_id AND group_openid = :group_openid"
            ),
            params,
        )
        result_players = await session.scalar(
            text(
                "SELECT count(*) FROM komari_roulette_result_players rp "
                "JOIN komari_roulette_results r ON r.game_id = rp.game_id "
                "WHERE r.app_id = :app_id AND r.group_openid = :group_openid"
            ),
            params,
        )
    return {
        "results": [(str(row[0]), str(row[1])) for row in results],
        "game_lifecycle": game_lifecycle,
        "wins": int(wins or 0),
        "result_players": int(result_players or 0),
    }


async def _assert_single_timeout_terminal(
    harness: Tsk279Harness,
    current: Any,
) -> None:
    """Exactly one completed result, reason ``timeout``, two seats, one win."""

    state = await _terminal_projection_state(harness, current)
    assert state["results"] == [("completed", "timeout")]
    assert state["game_lifecycle"] == "completed"
    assert state["wins"] == 1
    assert state["result_players"] == 2


async def _game_snapshot(harness: Tsk279Harness, current: Any) -> dict[str, Any]:
    """Full mutation-relevant snapshot of one exact scope's current game."""

    params = {"app_id": current.app_id, "group_openid": current.group_openid}
    async with harness.session_factory() as session:
        game = (
            (
                await session.execute(
                    text(
                        "SELECT game_id, lifecycle, state_revision, "
                        "chamber_revision, turn_seq, current_player_seq, "
                        "waiting_expires_at, turn_deadline_at, pending_rewards, "
                        "pending_burst, pending_locks, updated_at "
                        "FROM komari_roulette_games "
                        "WHERE app_id = :app_id AND group_openid = :group_openid "
                        "ORDER BY created_at DESC LIMIT 1"
                    ),
                    params,
                )
            )
            .mappings()
            .first()
        )
        assert game is not None
        eliminated = (
            (
                await session.execute(
                    text(
                        "SELECT join_seq FROM komari_roulette_players "
                        "WHERE game_id = :game_id AND eliminated_at IS NOT NULL"
                    ),
                    {"game_id": game["game_id"]},
                )
            )
            .scalars()
            .all()
        )
        receipts = int(
            await session.scalar(
                text(
                    "SELECT count(*) FROM komari_roulette_command_receipts "
                    "WHERE app_id = :app_id AND group_openid = :group_openid"
                ),
                params,
            )
            or 0
        )
        results = int(
            await session.scalar(
                text(
                    "SELECT count(*) FROM komari_roulette_results "
                    "WHERE app_id = :app_id AND group_openid = :group_openid"
                ),
                params,
            )
            or 0
        )
    return {
        "game": dict(game),
        "eliminated": [int(seq) for seq in eliminated],
        "receipts": receipts,
        "results": results,
    }


async def _seed_expired_active_turn(
    harness: Tsk279Harness,
    current: Any,
    *,
    message_prefix: str,
) -> _ExpiredTurn:
    """Seed a real two-player active game whose deadline already passed."""

    members = await seed_players(harness.binding_manager, current, 2)
    service = service_for(harness, random_source=CountingRandom())
    await create_waiting(service, current, message_id=f"{message_prefix}-create")
    await join_player(service, current, members[1], f"{message_prefix}-join-2")
    await start_game(service, current, members[0], f"{message_prefix}-start")
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
                "clock_timestamp() - interval '2 hours' WHERE game_id = :game_id"
            ),
            {"game_id": str(row["game_id"])},
        )
        await session.commit()
    return _ExpiredTurn(service=service, actor=actor, observation=observed)


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
        drive = await _drive_recovery_until_settled(
            maintenance,
            harness,
            app_id=app_id,
            group_openid=allowed,
        )
        assert drive.settled is True
        assert drive.skipped_restricted >= 1
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
        drive = await _drive_recovery_until_settled(
            maintenance,
            harness,
            app_id=app_id,
            group_openid=allowed,
        )
        assert drive.settled is True, (
            "bounded pagination must reach an allowed group behind a full "
            "batch of restricted groups"
        )
        assert drive.pages >= 2, (
            "the allowed group must sit behind a full first page so the keyset "
            "walk is what reaches it, not a private first-page assumption"
        )
        assert drive.skipped_restricted >= 100


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
        async def aged_gone() -> bool:
            async with harness.session_factory() as session:
                present = await session.scalar(
                    text(
                        "SELECT count(*) FROM komari_roulette_command_receipts "
                        "WHERE receipt_id = :receipt_id"
                    ),
                    {"receipt_id": aged},
                )
            return int(present or 0) == 0

        await _drive_cleanup_until(
            maintenance, harness, done=aged_gone, batch_size=100
        )
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
        async def nonwin_gone() -> bool:
            async with harness.session_factory() as session:
                present = await session.scalar(
                    text(
                        "SELECT count(*) FROM komari_roulette_games "
                        "WHERE game_id = :cancelled OR game_id = :expired"
                    ),
                    {"cancelled": cancelled, "expired": expired},
                )
            return int(present or 0) == 0

        await _drive_cleanup_until(
            maintenance, harness, done=nonwin_gone, batch_size=100
        )
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
        assert first.receipts_deleted <= 2

        async def ours_gone() -> bool:
            async with harness.session_factory() as session:
                present = await session.scalar(
                    text(
                        "SELECT count(*) FROM komari_roulette_command_receipts "
                        "WHERE app_id = :app_id AND group_openid = :grp"
                    ),
                    {"app_id": current.app_id, "grp": current.group_openid},
                )
            return int(present or 0) == 0

        await _drive_cleanup_until(
            maintenance, harness, done=ours_gone, batch_size=2
        )
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
        async def aged_gone() -> bool:
            async with harness.session_factory() as session:
                present = await session.scalar(
                    text(
                        "SELECT count(*) FROM komari_roulette_command_receipts "
                        "WHERE receipt_id = :receipt_id"
                    ),
                    {"receipt_id": aged},
                )
            return int(present or 0) == 0

        await _drive_cleanup_until(
            maintenance, harness, done=aged_gone, batch_size=10
        )
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
    # APScheduler renders the cron field as the bare value (``4``), not a
    # shell-like ``hour='4'`` repr.  Assert the 04:00 schedule directly on the
    # trigger's own timezone (the deployment timezone), so the host clock/TZ is
    # never involved.
    assert str(cleanup.trigger.fields[5]) == "4"
    assert str(cleanup.trigger.fields[6]) == "0"
    from datetime import datetime as _datetime

    at_noon = _datetime(2026, 1, 1, 12, 0, tzinfo=cleanup.trigger.timezone)
    next_fire = cleanup.trigger.get_next_fire_time(None, at_noon)
    assert next_fire is not None
    assert (next_fire.hour, next_fire.minute) == (4, 0)
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

        async def run(maintenance: Any) -> _RecoveryDrive:
            await barrier.wait()
            return await _drive_recovery_until_settled(
                maintenance,
                harness,
                app_id=current.app_id,
                group_openid=current.group_openid,
            )

        drives = await asyncio.gather(run(first), run(second))
        advanced = sum(drive.advanced for drive in drives)
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

        async def run(maintenance: Any) -> _RecoveryDrive:
            await barrier.wait()
            return await _drive_recovery_until_settled(
                maintenance,
                harness,
                app_id=current.app_id,
                group_openid=current.group_openid,
            )

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
                    "JOIN komari_roulette_results r ON r.game_id = rp.game_id "
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
async def test_expired_deadline_beats_forfeit_when_command_runs_first(
    harness: Tsk279Harness,
) -> None:
    """Controlled ordering #1: the real command holds the lock on a past deadline.

    The two-hours-past deadline is the terminal fact.  Even though the actor
    issues ``forfeit``, the real deep entry must settle ``turn_expired`` with
    reason ``timeout`` - never ``forfeited`` - and must not double-settle the
    win or the result seats.
    """

    async with harness.scope("deadline-command-first") as current:
        seeded = await _seed_expired_active_turn(
            harness, current, message_prefix="dcf"
        )
        receipt = await seeded.service.execute_group_command(
            request(
                current,
                "dcf-forfeit",
                command_factory("forfeit"),
                member_openid=seeded.actor,
            ),
            observation=seeded.observation,
        )
        assert receipt.result_code == "turn_expired"
        assert receipt.result_code != "forfeited"
        await _assert_single_timeout_terminal(harness, current)


@PG_REQUIRED
async def test_maintenance_first_settles_timeout_then_command_sees_no_active_game(
    harness: Tsk279Harness,
) -> None:
    """Controlled ordering #2: the worker settles first, the late command loses.

    With the same past deadline, the recovery worker takes the lock first and
    settles the timeout.  A command that arrives afterwards must observe
    ``no_active_game`` (or a clean ``state_conflict`` at the observation
    boundary) and must never overwrite the settled timeout with a forfeit.
    """

    api = _maintenance_api()
    async with harness.scope("deadline-maintenance-first") as current:
        seeded = await _seed_expired_active_turn(
            harness, current, message_prefix="dmf"
        )
        maintenance = api["RouletteMaintenance"](
            session_factory=harness.session_factory,
            service=seeded.service,
            admission=_allow_only({(current.app_id, current.group_openid)}),
        )
        drive = await _drive_recovery_until_settled(
            maintenance,
            harness,
            app_id=current.app_id,
            group_openid=current.group_openid,
        )
        assert drive.settled is True
        assert drive.advanced == 1
        await _assert_single_timeout_terminal(harness, current)

        late = await seeded.service.execute_group_command(
            request(
                current,
                "dmf-forfeit",
                command_factory("forfeit"),
                member_openid=seeded.actor,
            ),
            observation=seeded.observation,
        )
        assert late.result_code in {"no_active_game", "state_conflict"}
        assert late.result_code != "forfeited"
        await _assert_single_timeout_terminal(harness, current)


# ---------------------------------------------------------------------------
# Post-lock effect recheck on the real service (requirement 5 / TSK-279 10.2)
# ---------------------------------------------------------------------------


@PG_REQUIRED
async def test_advance_expired_rejects_sync_gate_with_zero_mutation(
    harness: Tsk279Harness,
) -> None:
    """A sync ``effect_check`` returning False is a no-effect refusal."""

    from komari_bot.plugins.komari_roulette.command_service import EFFECT_CHECK_REJECTED

    async with harness.scope("effect-reject-sync") as current:
        service = service_for(harness, random_source=CountingRandom())
        await insert_waiting_game(
            harness.session_factory,
            game_id=str(uuid4()),
            app_id=current.app_id,
            group_openid=current.group_openid,
            member_openid=current.member_openid,
            deadline_age_seconds=60,
        )
        before = await _game_snapshot(harness, current)
        advance = await service.advance_expired(
            current.group,
            effect_check=lambda: False,
        )
        assert advance.result_code == EFFECT_CHECK_REJECTED
        assert advance.changed is False
        assert advance.receipt_id is None
        after = await _game_snapshot(harness, current)
        assert after == before


@PG_REQUIRED
async def test_advance_expired_rejects_async_gate_with_zero_mutation(
    harness: Tsk279Harness,
) -> None:
    """An awaitable ``effect_check`` resolving False is a no-effect refusal."""

    from komari_bot.plugins.komari_roulette.command_service import EFFECT_CHECK_REJECTED

    async def gate() -> bool:
        return False

    async with harness.scope("effect-reject-async") as current:
        service = service_for(harness, random_source=CountingRandom())
        await insert_waiting_game(
            harness.session_factory,
            game_id=str(uuid4()),
            app_id=current.app_id,
            group_openid=current.group_openid,
            member_openid=current.member_openid,
            deadline_age_seconds=60,
        )
        before = await _game_snapshot(harness, current)
        advance = await service.advance_expired(
            current.group,
            effect_check=gate,
        )
        assert advance.result_code == EFFECT_CHECK_REJECTED
        assert advance.changed is False
        after = await _game_snapshot(harness, current)
        assert after == before


@PG_REQUIRED
async def test_advance_expired_gate_exception_fails_closed_zero_mutation(
    harness: Tsk279Harness,
) -> None:
    """A gate that raises must fail closed, never licence a write."""

    from komari_bot.plugins.komari_roulette.command_service import EFFECT_CHECK_REJECTED

    def gate() -> bool:
        message = "admission port exploded (TSK-279 test port)"
        raise RuntimeError(message)

    async with harness.scope("effect-reject-raise") as current:
        service = service_for(harness, random_source=CountingRandom())
        await insert_waiting_game(
            harness.session_factory,
            game_id=str(uuid4()),
            app_id=current.app_id,
            group_openid=current.group_openid,
            member_openid=current.member_openid,
            deadline_age_seconds=60,
        )
        before = await _game_snapshot(harness, current)
        advance = await service.advance_expired(
            current.group,
            effect_check=gate,
        )
        assert advance.result_code == EFFECT_CHECK_REJECTED
        assert advance.changed is False
        after = await _game_snapshot(harness, current)
        assert after == before


@PG_REQUIRED
async def test_advance_expired_accepts_true_gate_and_advances(
    harness: Tsk279Harness,
) -> None:
    """A gate resolving True lets the real expiry proceed exactly once."""

    gate_calls = 0

    def gate() -> bool:
        nonlocal gate_calls
        gate_calls += 1
        return True

    async with harness.scope("effect-accept") as current:
        service = service_for(harness, random_source=CountingRandom())
        await insert_waiting_game(
            harness.session_factory,
            game_id=str(uuid4()),
            app_id=current.app_id,
            group_openid=current.group_openid,
            member_openid=current.member_openid,
            deadline_age_seconds=60,
        )
        advance = await service.advance_expired(
            current.group,
            effect_check=gate,
        )
        assert gate_calls == 1
        assert advance.changed is True
        assert advance.result_code == "waiting_game_expired"
        lifecycle = await _group_lifecycle(
            harness,
            app_id=current.app_id,
            group_openid=current.group_openid,
        )
        assert lifecycle == "expired"


@PG_REQUIRED
async def test_advance_expired_runs_gate_only_after_group_lock_is_held(
    harness: Tsk279Harness,
) -> None:
    """The post-lock gate really runs after the group advisory lock is held."""

    gate_calls = 0

    async def gate() -> bool:
        nonlocal gate_calls
        gate_calls += 1
        return True

    async with harness.scope("effect-lock-order") as current:
        service = service_for(harness, random_source=CountingRandom())
        await insert_waiting_game(
            harness.session_factory,
            game_id=str(uuid4()),
            app_id=current.app_id,
            group_openid=current.group_openid,
            member_openid=current.member_openid,
            deadline_age_seconds=60,
        )
        async with harness.session_factory() as blocker:
            await blocker.begin()
            blocker_pid = await backend_pid(blocker)
            await hold_group_lock(blocker, current)
            advance_task = asyncio.create_task(
                service.advance_expired(current.group, effect_check=gate)
            )
            try:
                await wait_for_blocked(harness.session_factory, blocker_pid)
                # While the worker is queued on the group lock the gate has not
                # run yet: the recheck is genuinely *after* the lock, not before.
                assert gate_calls == 0
            finally:
                await blocker.commit()
            async with asyncio.timeout(10):
                advance = await advance_task
        assert gate_calls == 1
        assert advance.changed is True
        assert advance.result_code == "waiting_game_expired"


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
        result_cls = _maintenance_api()["RecoveryTickResult"]
        return result_cls(
            scanned=0,
            advanced=0,
            skipped_restricted=0,
            failed=0,
        )

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
            tick_task = asyncio.create_task(
                _drive_recovery_until_settled(
                    maintenance,
                    harness,
                    app_id=app_id,
                    group_openid=group_openid,
                )
            )
            try:
                await wait_for_blocked(harness.session_factory, blocker_pid)
                # The group turns restricted while the worker waits on the lock.
                gate["allowed"] = False
            finally:
                await blocker.commit()
            async with asyncio.timeout(10):
                drive = await tick_task
        async with harness.session_factory() as session:
            lifecycle = await session.scalar(
                text(
                    "SELECT lifecycle FROM komari_roulette_games "
                    "WHERE app_id = :app_id AND group_openid = :group_openid"
                ),
                {"app_id": app_id, "group_openid": group_openid},
            )
        assert lifecycle == "waiting"
        assert drive.advanced == 0


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

        # Test port for the True→close transition only: ``runtime.accepting``
        # stands in for "runtime still alive".  Production maintenance must NOT
        # gate on the business ``plugin_enable`` switch (see TSK-279-contract
        # §10.2): a disabled business switch still requires maintenance.  The
        # real shutdown / dependency-ready / group-admission combination is a
        # Stage-C concern.
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
            tick_task = asyncio.create_task(
                _drive_recovery_until_settled(
                    maintenance,
                    harness,
                    app_id=app_id,
                    group_openid=group_openid,
                )
            )
            try:
                await wait_for_blocked(harness.session_factory, blocker_pid)
                # The real runtime closes while the worker waits on the lock.
                await runtime.close()
                assert runtime.accepting is False
            finally:
                await blocker.commit()
            async with asyncio.timeout(10):
                drive = await tick_task
        async with harness.session_factory() as session:
            lifecycle = await session.scalar(
                text(
                    "SELECT lifecycle FROM komari_roulette_games "
                    "WHERE app_id = :app_id AND group_openid = :group_openid"
                ),
                {"app_id": app_id, "group_openid": group_openid},
            )
        assert lifecycle == "waiting"
        assert drive.advanced == 0


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
