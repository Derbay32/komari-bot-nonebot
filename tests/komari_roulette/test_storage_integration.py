"""TSK-275 real PostgreSQL aggregate/storage acceptance tests."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from komari_bot.plugins.komari_roulette.domain import (
    Action,
    ChamberKind,
    apply_action,
)
from komari_bot.plugins.komari_roulette.mapper import (
    GameSnapshot,
    TerminalProjection,
    game_state_from_snapshot,
    game_state_to_snapshot,
    transition_from_action_result,
)
from komari_bot.plugins.komari_roulette.orm_models import (
    RouletteGameRow,
    RoulettePlayerRow,
)
from komari_bot.plugins.komari_roulette.storage import (
    AggregateCorruptError,
    PostgresRouletteStorage,
    RevisionConflictError,
    StorageUnavailableError,
    TerminalProjectionRejectedError,
)
from tests.komari_roulette.storage_support import (
    POSTGRES_URL,
    SQLALCHEMY_URL,
    START,
    DeterministicRandom,
    active_state,
    clear_scope,
    count_scope_rows,
    group_for,
    open_session,
    player_for,
    reset_shared_orm_engine,
    same_database,
    scope,
    waiting_state,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession

    from komari_bot.plugins.komari_roulette.domain import GameState, GroupRef


pytestmark = [
    pytest.mark.skipif(
        not POSTGRES_URL,
        reason="未设置 KOMARI_TEST_POSTGRES_URL，不能执行真实 PostgreSQL 验收",
    ),
    pytest.mark.asyncio,
]


@pytest.fixture
async def db_scope() -> AsyncIterator[tuple[str, str, GroupRef]]:
    if not same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")
    app_id, group_openid = scope("storage")
    group = group_for(app_id, group_openid)
    await reset_shared_orm_engine()
    try:
        yield app_id, group_openid, group
    finally:
        with suppress(Exception):
            await clear_scope(app_id, group_openid)
        await reset_shared_orm_engine()


def _snapshot(state: GameState, game_id: str | None = None) -> GameSnapshot:
    return game_state_to_snapshot(state, game_id=game_id or str(uuid4()))


async def _pg_now(session: AsyncSession) -> datetime:
    value = (await session.execute(text("SELECT CURRENT_TIMESTAMP"))).scalar_one()
    assert isinstance(value, datetime)
    return value


async def _backend_pid(session: AsyncSession) -> int:
    value = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()
    assert isinstance(value, int)
    return value


async def _blocking_pids(session: AsyncSession, pid: int) -> tuple[int, ...]:
    value = (
        await session.execute(
            text("SELECT pg_blocking_pids(:pid)"),
            {"pid": pid},
        )
    ).scalar_one()
    assert isinstance(value, list)
    return tuple(int(blocker) for blocker in value)


async def _wait_until_blocked(
    observer: AsyncSession,
    pid: int,
    *,
    wait_seconds: float = 5.0,
) -> tuple[int, ...]:
    deadline = asyncio.get_running_loop().time() + wait_seconds
    while asyncio.get_running_loop().time() < deadline:
        blockers = await _blocking_pids(observer, pid)
        if blockers:
            return blockers
        await asyncio.sleep(0.02)
    pytest.fail(f"backend {pid} did not become blocked")


async def _create_waiting(
    group: GroupRef,
    *,
    game_id: str | None = None,
) -> GameSnapshot:
    session = open_session()
    try:
        storage = PostgresRouletteStorage(session)
        snapshot = _snapshot(waiting_state(group), game_id)
        stored = await storage.create_waiting(snapshot)
        await session.commit()
        assert stored.game_id == snapshot.game_id
        return stored
    finally:
        await session.close()


async def _persist_waiting_players(
    session: AsyncSession,
    group: GroupRef,
    *,
    names: tuple[str, ...] = ("玩家一", "玩家二"),
    member_openids: tuple[str, ...] | None = None,
    game_id: str | None = None,
) -> GameSnapshot:
    """Persist create + joins as separate caller-owned transactions."""

    storage = PostgresRouletteStorage(session)
    game_id = game_id or str(uuid4())
    initial = _snapshot(
        waiting_state(
            group,
            names=(names[0],),
            member_openids=member_openids[:1] if member_openids else None,
        ),
        game_id,
    )
    current_snapshot = await storage.create_waiting(initial)
    await session.commit()
    current = game_state_from_snapshot(current_snapshot)
    for number in range(2, len(names) + 1):
        actor = player_for(
            group,
            number,
            name=names[number - 1],
            member_openid=(member_openids[number - 1] if member_openids else None),
        )
        result = apply_action(
            current,
            Action.join(actor),
            now=START,
            random_source=DeterministicRandom(),
        )
        assert result.ok and result.code == "joined"
        transition = transition_from_action_result(
            current_snapshot,
            result,
            action_kind="join",
            occurred_at=await _pg_now(session),
        )
        current_snapshot = await storage.save_transition(
            transition,
            expected_revision=current.state_revision,
        )
        await session.commit()
        current = game_state_from_snapshot(current_snapshot)
    return current_snapshot


async def _persist_active(
    session: AsyncSession,
    group: GroupRef,
    *,
    names: tuple[str, ...] = ("玩家一", "玩家二"),
    member_openids: tuple[str, ...] | None = None,
    chamber: tuple[ChamberKind, ...] = (
        ChamberKind.LIVE,
        ChamberKind.LIVE,
        ChamberKind.BLANK,
        ChamberKind.BLANK,
        ChamberKind.BLANK,
        ChamberKind.BLANK,
    ),
    game_id: str | None = None,
) -> GameSnapshot:
    current_snapshot = await _persist_waiting_players(
        session,
        group,
        names=names,
        member_openids=member_openids,
        game_id=game_id,
    )
    current = game_state_from_snapshot(current_snapshot)
    result = apply_action(
        current,
        Action.start(current.players[0].player),
        now=START,
        random_source=DeterministicRandom(chambers=(chamber,)),
    )
    assert result.ok and result.code == "started"
    transition = transition_from_action_result(
        current_snapshot,
        result,
        action_kind="start",
        occurred_at=await _pg_now(session),
    )
    storage = PostgresRouletteStorage(session)
    active_snapshot = await storage.save_transition(
        transition,
        expected_revision=current.state_revision,
    )
    await session.commit()
    return active_snapshot


async def _persist_completed(
    session: AsyncSession,
    group: GroupRef,
    *,
    names: tuple[str, str, str] = ("玩家一", "玩家二", "玩家三"),
    member_openids: tuple[str, str, str] | None = None,
    project: bool = True,
) -> tuple[GameSnapshot, TerminalProjection, GameSnapshot]:
    """Persist one shot and one forfeit elimination, then project the winner."""

    current_snapshot = await _persist_active(
        session,
        group,
        names=names,
        member_openids=member_openids,
        chamber=(
            ChamberKind.LIVE,
            ChamberKind.LIVE,
            ChamberKind.BLANK,
            ChamberKind.BLANK,
            ChamberKind.BLANK,
            ChamberKind.BLANK,
        ),
    )
    storage = PostgresRouletteStorage(session)
    current = game_state_from_snapshot(current_snapshot)
    active_before_terminal: GameSnapshot | None = None
    for index, action_kind in enumerate(("shoot", "forfeit")):
        actor = next(
            seat.player
            for seat in current.players
            if seat.join_seq == current.current_player_seq
        )
        result = apply_action(
            current,
            Action.shoot(actor) if action_kind == "shoot" else Action.forfeit(actor),
            now=START + timedelta(minutes=index + 1),
            random_source=DeterministicRandom(),
        )
        assert result.ok
        transition = transition_from_action_result(
            current_snapshot,
            result,
            action_kind=action_kind,
            occurred_at=await _pg_now(session),
        )
        current_snapshot = await storage.save_transition(
            transition,
            expected_revision=current.state_revision,
        )
        if action_kind == "shoot":
            await session.commit()
            reload_session = open_session()
            try:
                reloaded = await PostgresRouletteStorage(reload_session).load_current(
                    group
                )
                assert reloaded is not None
                current_snapshot = reloaded
                active_before_terminal = reloaded
            finally:
                await reload_session.close()
        current = game_state_from_snapshot(current_snapshot)
    assert current.lifecycle == "completed"
    assert active_before_terminal is not None
    projection = TerminalProjection.from_state(
        current_snapshot,
        lifecycle="completed",
        reason="forfeit",
        ended_at=await _pg_now(session),
        winner_seq=3,
    )
    if project:
        await storage.project_terminal(projection)
        await session.commit()
    return current_snapshot, projection, active_before_terminal


async def test_create_and_load_use_postgres_as_durable_source(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    _app_id, _group_openid, group = db_scope
    created = await _create_waiting(group)

    session = open_session()
    try:
        loaded = await PostgresRouletteStorage(session).load_current(group)
        assert loaded is not None
        assert loaded.game_id == created.game_id
        restored = game_state_from_snapshot(loaded)
        assert restored.lifecycle == "waiting"
        assert restored.state_revision == 1
        assert [seat.join_seq for seat in restored.players] == [1]
    finally:
        await session.close()


async def test_redis_loss_does_not_change_postgres_game_fact(
    db_scope: tuple[str, str, GroupRef],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app_id, _group_openid, group = db_scope
    from redis import asyncio as redis_asyncio

    redis_calls = 0

    async def unavailable_execute_command(*_args: object, **_kwargs: object) -> None:
        nonlocal redis_calls
        redis_calls += 1
        raise ConnectionError

    monkeypatch.setattr(
        redis_asyncio.Redis,
        "execute_command",
        unavailable_execute_command,
    )
    created = await _create_waiting(group)
    session = open_session()
    try:
        loaded = await PostgresRouletteStorage(session).load_current(group)
        assert loaded is not None
        assert loaded.game_id == created.game_id
        assert game_state_from_snapshot(loaded).lifecycle == "waiting"
        assert redis_calls == 0
    finally:
        await session.close()


async def test_storage_methods_leave_commit_to_the_calling_unit_of_work(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    app_id, group_openid, group = db_scope
    session = open_session()
    try:
        storage = PostgresRouletteStorage(session)
        await storage.create_waiting(_snapshot(waiting_state(group)))
        assert session.in_transaction()
        await session.rollback()
    finally:
        await session.close()

    assert all(
        value == 0 for value in (await count_scope_rows(app_id, group_openid)).values()
    )


async def test_database_outage_is_explicit_storage_unavailable() -> None:
    engine = create_async_engine(
        "postgresql+asyncpg://komari_test@127.0.0.1:1/does_not_exist",
        pool_pre_ping=True,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            with pytest.raises(StorageUnavailableError):
                await PostgresRouletteStorage(session).load_current(
                    group_for("outage-app", "outage-group")
                )
    finally:
        await engine.dispose()


async def test_successfully_read_but_cross_row_corrupt_aggregate_is_not_no_game(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    _app_id, _group_openid, group = db_scope
    created = await _create_waiting(group)
    corrupt_session = open_session()
    try:
        await corrupt_session.execute(
            text(
                "UPDATE komari_roulette_games SET next_join_seq = 1 "
                "WHERE game_id = :game_id"
            ),
            {"game_id": created.game_id},
        )
        await corrupt_session.commit()
    finally:
        await corrupt_session.close()

    read_session = open_session()
    try:
        with pytest.raises(AggregateCorruptError):
            await PostgresRouletteStorage(read_session).load_current(group)
    finally:
        await read_session.close()


async def test_cross_row_stage_invariant_is_checked_when_active_roster_is_too_small(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    _app_id, _group_openid, group = db_scope
    session = open_session()
    try:
        active = await _persist_active(
            session,
            group,
            names=("合法玩家一", "合法玩家二"),
        )
        await session.execute(
            text(
                "DELETE FROM komari_roulette_players "
                "WHERE game_id = :game_id AND join_seq = 2"
            ),
            {"game_id": active.game_id},
        )
        await session.commit()
        with pytest.raises(AggregateCorruptError):
            await PostgresRouletteStorage(session).load_current(group)
    finally:
        await session.close()


@pytest.mark.parametrize(
    "malformed_weight",
    (True, 1.5, "3"),
    ids=("bool", "float", "numeric-string"),
)
async def test_json_weight_types_are_not_silently_coerced(
    db_scope: tuple[str, str, GroupRef],
    malformed_weight: object,
) -> None:
    _app_id, _group_openid, group = db_scope
    session = open_session()
    try:
        created = await _create_waiting(group)
        baseline = await PostgresRouletteStorage(session).load_current(group)
        assert baseline is not None
        weights: dict[str, object] = {
            item.value: value for item, value in baseline.item_weights.items()
        }
        weights["beer"] = malformed_weight
        await session.execute(
            text(
                "UPDATE komari_roulette_games "
                "SET item_weights = CAST(:weights AS json) "
                "WHERE game_id = :game_id"
            ),
            {"weights": json.dumps(weights), "game_id": created.game_id},
        )
        await session.commit()
    finally:
        await session.close()

    read_session = open_session()
    try:
        with pytest.raises(AggregateCorruptError):
            await PostgresRouletteStorage(read_session).load_current(group)
        assert (
            await PostgresRouletteStorage(read_session).get_result(
                group, created.game_id
            )
            is None
        )
    finally:
        await read_session.close()


async def test_persisted_zero_revision_is_not_a_valid_current_game(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    _app_id, _group_openid, group = db_scope
    session = open_session()
    try:
        created = await _create_waiting(group)
        await session.execute(
            text(
                "UPDATE komari_roulette_games SET state_revision = 0 "
                "WHERE game_id = :game_id"
            ),
            {"game_id": created.game_id},
        )
        await session.commit()
    finally:
        await session.close()

    read_session = open_session()
    try:
        with pytest.raises(AggregateCorruptError):
            await PostgresRouletteStorage(read_session).load_current(group)
    finally:
        await read_session.close()


@pytest.mark.parametrize(
    "corruption",
    (
        "empty_chamber",
        "chamber_without_live",
        "zero_chamber_revision",
        "zero_turn_seq",
        "pending_reward_wrong_phase",
        "item_choice_not_full",
        "duplicate_pending_locks",
        "duplicate_frozen_name",
        "lock_targets_eliminated_player",
    ),
)
async def test_active_row_and_phase_invariants_fail_closed(
    db_scope: tuple[str, str, GroupRef],
    corruption: str,
) -> None:
    _app_id, _group_openid, group = db_scope
    session = open_session()
    try:
        names = (
            ("合法一", "合法二", "合法三")
            if corruption == "lock_targets_eliminated_player"
            else ("合法一", "合法二")
        )
        active = await _persist_active(session, group, names=names)
        baseline = await PostgresRouletteStorage(session).load_current(group)
        assert baseline is not None
        game_id = active.game_id
        if corruption == "duplicate_frozen_name":
            await session.execute(
                text(
                    "UPDATE komari_roulette_players AS duplicate "
                    "SET display_name = original.display_name "
                    "FROM komari_roulette_players AS original "
                    "WHERE duplicate.game_id = :game_id "
                    "AND original.game_id = :game_id "
                    "AND duplicate.join_seq = 2 "
                    "AND original.join_seq = 1"
                ),
                {"game_id": game_id},
            )
        elif corruption == "lock_targets_eliminated_player":
            await session.execute(
                text(
                    "UPDATE komari_roulette_players SET alive = false, "
                    "eliminated_order = 1, eliminated_reason = 'shot', "
                    "eliminated_at = CURRENT_TIMESTAMP "
                    "WHERE game_id = :game_id AND join_seq = 1"
                ),
                {"game_id": game_id},
            )
            await session.execute(
                text(
                    "UPDATE komari_roulette_games SET current_player_seq = 2, "
                    "phase = 'locked_turn', "
                    "pending_locks = ARRAY[1]::integer[] "
                    "WHERE game_id = :game_id"
                ),
                {"game_id": game_id},
            )
        else:
            statements = {
                "empty_chamber": (
                    "UPDATE komari_roulette_games SET ordered_chamber = "
                    "ARRAY[]::text[] WHERE game_id = :game_id"
                ),
                "chamber_without_live": (
                    "UPDATE komari_roulette_games SET ordered_chamber = "
                    "ARRAY['blank','blank','blank','blank','blank','blank']::text[] "
                    "WHERE game_id = :game_id"
                ),
                "zero_chamber_revision": (
                    "UPDATE komari_roulette_games SET chamber_revision = 0 "
                    "WHERE game_id = :game_id"
                ),
                "zero_turn_seq": (
                    "UPDATE komari_roulette_games SET turn_seq = 0 "
                    "WHERE game_id = :game_id"
                ),
                "pending_reward_wrong_phase": (
                    "UPDATE komari_roulette_games SET pending_rewards = "
                    "ARRAY['beer']::text[] WHERE game_id = :game_id"
                ),
                "item_choice_not_full": (
                    "UPDATE komari_roulette_games SET phase = 'item_choice', "
                    "pending_rewards = ARRAY['beer']::text[] "
                    "WHERE game_id = :game_id"
                ),
                "duplicate_pending_locks": (
                    "UPDATE komari_roulette_games SET pending_locks = "
                    "ARRAY[2,2]::integer[] WHERE game_id = :game_id"
                ),
            }
            await session.execute(text(statements[corruption]), {"game_id": game_id})
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            restored = await PostgresRouletteStorage(session).load_current(group)
            assert restored is not None
            assert restored.state_revision == baseline.state_revision
            assert restored.players == baseline.players
            return
    finally:
        await session.close()

    read_session = open_session()
    try:
        with pytest.raises(AggregateCorruptError):
            await PostgresRouletteStorage(read_session).load_current(group)
    finally:
        await read_session.close()


async def test_controlled_failed_projection_after_corruption_has_no_winner(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    _app_id, _group_openid, group = db_scope
    created = await _create_waiting(group)
    corrupt_session = open_session()
    try:
        storage = PostgresRouletteStorage(corrupt_session)
        await corrupt_session.execute(
            text(
                "UPDATE komari_roulette_games SET next_join_seq = 1 "
                "WHERE game_id = :game_id"
            ),
            {"game_id": created.game_id},
        )
        await corrupt_session.commit()
        with pytest.raises(AggregateCorruptError):
            await storage.load_current(group)
        projection = TerminalProjection.from_state(
            created,
            lifecycle="failed",
            reason="aggregate_corrupt",
            ended_at=await _pg_now(corrupt_session),
            winner_seq=None,
        )
        await storage.project_terminal(projection)
        await corrupt_session.commit()
        result = await storage.get_result(group, created.game_id)
        assert result is not None
        assert result.lifecycle == "failed"
        assert result.winner_member_openid is None
        assert await storage.list_leaderboard(group) == ()
        assert result.reason == "aggregate_corrupt"
        assert result.terminal_revision == created.state_revision + 1
        root = (
            await corrupt_session.execute(
                text(
                    "SELECT lifecycle, state_revision "
                    "FROM komari_roulette_games WHERE game_id = :game_id"
                ),
                {"game_id": created.game_id},
            )
        ).mappings().one()
        assert root["lifecycle"] == "failed"
        assert root["state_revision"] == result.terminal_revision
    finally:
        await corrupt_session.close()


async def test_fail_corrupt_current_projects_safe_history_and_releases_slot(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    _app_id, _group_openid, group = db_scope
    setup = open_session()
    try:
        active = await _persist_active(
            setup,
            group,
            names=("原始甲", "原始乙"),
        )
    finally:
        await setup.close()

    corrupt = open_session()
    try:
        await corrupt.execute(
            text(
                "UPDATE komari_roulette_players AS duplicate "
                "SET display_name = original.display_name "
                "FROM komari_roulette_players AS original "
                "WHERE duplicate.game_id = :game_id "
                "AND original.game_id = :game_id "
                "AND duplicate.join_seq = 2 "
                "AND original.join_seq = 1"
            ),
            {"game_id": active.game_id},
        )
        await corrupt.commit()
    finally:
        await corrupt.close()

    read_session = open_session()
    try:
        with pytest.raises(AggregateCorruptError):
            await PostgresRouletteStorage(read_session).load_current(group)
    finally:
        await read_session.close()

    failed_session = open_session()
    try:
        storage = PostgresRouletteStorage(failed_session)
        failed = await storage.fail_corrupt_current(
            group,
            ended_at=await _pg_now(failed_session),
        )
        assert failed is not None
        assert failed.lifecycle == "failed"
        assert failed.reason == "aggregate_corrupt"
        assert failed.winner_seq is None
        assert failed.winner_member_openid is None
        assert failed.players
        assert failed.players[0].join_seq == 1
        await failed_session.commit()

        stored = await storage.get_result(group, active.game_id)
        assert stored is not None
        assert stored.lifecycle == "failed"
        assert stored.winner_seq is None
        assert stored.players
        assert stored.players[0].member_openid == active.players[0].member_openid
        assert stored.terminal_revision == active.state_revision + 1
        root = (
            await failed_session.execute(
                text(
                    "SELECT lifecycle, state_revision, ordered_chamber, "
                    "pending_rewards, pending_burst, pending_locks "
                    "FROM komari_roulette_games WHERE game_id = :game_id"
                ),
                {"game_id": active.game_id},
            )
        ).mappings().one()
        assert root["lifecycle"] == "failed"
        assert root["state_revision"] == stored.terminal_revision
        assert list(root["ordered_chamber"] or ()) == []
        assert list(root["pending_rewards"] or ()) == []
        assert root["pending_burst"] is False
        assert list(root["pending_locks"] or ()) == []
    finally:
        await failed_session.close()

    assert (await count_scope_rows(group.app_id, group.group_openid))["komari_roulette_players"] == 0
    replacement = await _create_waiting(group)
    assert replacement.lifecycle == "waiting"


async def test_fail_corrupt_current_allows_empty_failed_history(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    _app_id, _group_openid, group = db_scope
    setup = open_session()
    try:
        active = await _persist_active(setup, group)
    finally:
        await setup.close()

    corrupt = open_session()
    try:
        await corrupt.execute(
            text(
                "DELETE FROM komari_roulette_players "
                "WHERE game_id = :game_id"
            ),
            {"game_id": active.game_id},
        )
        await corrupt.commit()
    finally:
        await corrupt.close()

    read_session = open_session()
    try:
        with pytest.raises(AggregateCorruptError):
            await PostgresRouletteStorage(read_session).load_current(group)
    finally:
        await read_session.close()

    failed_session = open_session()
    try:
        storage = PostgresRouletteStorage(failed_session)
        failed = await storage.fail_corrupt_current(
            group,
            ended_at=await _pg_now(failed_session),
        )
        assert failed is not None
        assert failed.lifecycle == "failed"
        assert failed.players == ()
        await failed_session.commit()
        stored = await storage.get_result(group, active.game_id)
        assert stored is not None
        assert stored.lifecycle == "failed"
        assert stored.players == ()
        assert stored.winner_member_openid is None
        assert await storage.list_leaderboard(group) == ()
    finally:
        await failed_session.close()

    counts = await count_scope_rows(group.app_id, group.group_openid)
    assert counts["komari_roulette_players"] == 0
    assert counts["komari_roulette_results"] == 1
    assert counts["komari_roulette_result_players"] == 0


async def test_fail_corrupt_current_rejects_a_valid_active_game_without_changes(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    _app_id, _group_openid, group = db_scope
    setup = open_session()
    try:
        active = await _persist_active(setup, group)
    finally:
        await setup.close()

    session = open_session()
    try:
        storage = PostgresRouletteStorage(session)
        before = await storage.load_current(group)
        assert before is not None
        with pytest.raises(TerminalProjectionRejectedError):
            await storage.fail_corrupt_current(
                group,
                ended_at=await _pg_now(session),
            )
        await session.rollback()
        restored = await storage.load_current(group)
        assert restored == before
        assert await storage.get_result(group, active.game_id) is None
        assert await storage.list_leaderboard(group) == ()
    finally:
        await session.close()


async def test_infrastructure_failure_never_creates_a_failed_result(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    _app_id, _group_openid, group = db_scope
    setup = open_session()
    try:
        active = await _persist_active(setup, group)
    finally:
        await setup.close()

    broken_engine = create_async_engine(
        "postgresql+asyncpg://komari_test@127.0.0.1:1/does_not_exist",
        pool_pre_ping=True,
    )
    broken_factory = async_sessionmaker(broken_engine, expire_on_commit=False)
    try:
        async with broken_factory() as broken_session:
            with pytest.raises(StorageUnavailableError):
                await PostgresRouletteStorage(broken_session).fail_corrupt_current(
                    group,
                    ended_at=START + timedelta(minutes=15),
                )
    finally:
        await broken_engine.dispose()

    verify = open_session()
    try:
        storage = PostgresRouletteStorage(verify)
        current = await storage.load_current(group)
        assert current is not None
        assert current.game_id == active.game_id
        assert current.lifecycle == "active"
        assert await storage.get_result(group, active.game_id) is None
        assert await storage.list_leaderboard(group) == ()
    finally:
        await verify.close()


async def test_terminal_projection_rejects_an_absent_game(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    _app_id, _group_openid, group = db_scope
    session = open_session()
    try:
        storage = PostgresRouletteStorage(session)
        projection = TerminalProjection.from_state(
            _snapshot(active_state(group, player_count=2)),
            lifecycle="completed",
            reason="forfeit",
            ended_at=await _pg_now(session),
            winner_seq=2,
        )
        with pytest.raises(TerminalProjectionRejectedError):
            await storage.project_terminal(projection)
        await session.rollback()
        assert await storage.load_current(group) is None
        assert await storage.list_leaderboard(group) == ()
    finally:
        await session.close()


async def test_terminal_projection_rejects_multiple_live_players_as_winner(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    _app_id, _group_openid, group = db_scope
    session = open_session()
    try:
        storage = PostgresRouletteStorage(session)
        current = await _persist_active(
            session,
            group,
            names=("仍存活一", "仍存活二", "仍存活三"),
        )
        projection = TerminalProjection.from_state(
            current,
            lifecycle="completed",
            reason="forfeit",
            ended_at=await _pg_now(session),
            winner_seq=1,
        )
        with pytest.raises(TerminalProjectionRejectedError):
            await storage.project_terminal(projection)
        await session.rollback()
    finally:
        await session.close()

    verify_session = open_session()
    try:
        restored_snapshot = await PostgresRouletteStorage(verify_session).load_current(
            group
        )
        assert restored_snapshot is not None
        restored = game_state_from_snapshot(restored_snapshot)
        assert restored.lifecycle == "active"
        assert all(seat.alive for seat in restored.players)
    finally:
        await verify_session.close()


async def test_same_group_concurrent_create_has_one_winner_without_in_process_lock(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    _app_id, _group_openid, group = db_scope
    barrier = asyncio.Barrier(2)

    async def create_once() -> str:
        session = open_session()
        try:
            storage = PostgresRouletteStorage(session)
            await barrier.wait()
            await storage.create_waiting(_snapshot(waiting_state(group)))
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return "conflict"
        else:
            return "created"
        finally:
            await session.close()

    outcomes = await asyncio.gather(create_once(), create_once())
    assert sorted(outcomes) == ["conflict", "created"]

    session = open_session()
    try:
        current = await PostgresRouletteStorage(session).load_current(group)
        assert current is not None
    finally:
        await session.close()


async def test_nonlocking_load_returns_one_complete_snapshot_during_join_commit(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    _app_id, _group_openid, group = db_scope
    created = await _create_waiting(group)
    reader = open_session()
    writer = open_session()
    observer = open_session()
    writer_task: asyncio.Task[None] | None = None
    writer_was_blocked = False
    try:
        reader_storage = PostgresRouletteStorage(reader)
        writer_storage = PostgresRouletteStorage(writer)
        writer_pid = await _backend_pid(writer)
        original_execute = reader.execute
        hook_used = False

        async def join_and_commit() -> None:
            before = await writer_storage.load_current(group)
            assert before is not None
            before_state = game_state_from_snapshot(before)
            actor = player_for(
                group,
                2,
                name="钩子加入者",
                member_openid="snapshot-hook-member",
            )
            joined = apply_action(
                before_state,
                Action.join(actor),
                now=START,
                random_source=DeterministicRandom(),
            )
            assert joined.ok and joined.code == "joined"
            transition = transition_from_action_result(
                before,
                joined,
                action_kind="join",
                occurred_at=await _pg_now(writer),
            )
            await writer_storage.save_transition(
                transition,
                expected_revision=before.state_revision,
            )
            await writer.commit()

        async def execute_with_join_hook(
            statement: Any,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            nonlocal hook_used, writer_task, writer_was_blocked
            result = await original_execute(statement, *args, **kwargs)
            if not hook_used:
                hook_used = True
                writer_task = asyncio.create_task(join_and_commit())
                try:
                    await asyncio.wait_for(asyncio.shield(writer_task), timeout=1)
                except TimeoutError:
                    blockers = await _blocking_pids(observer, writer_pid)
                    if blockers:
                        writer_was_blocked = True
                    else:
                        await asyncio.wait_for(writer_task, timeout=5)
            return result

        object.__setattr__(reader, "execute", execute_with_join_hook)
        loaded = await reader_storage.load_current(group, for_update=False)
        assert loaded is not None
        assert hook_used
        # The reader may observe the statement's old snapshot or a fully new
        # one, but it must never combine root revision 1 with two player rows.
        assert (loaded.state_revision, len(loaded.players)) in {(1, 1), (2, 2)}
        assert not (
            loaded.state_revision == created.state_revision and len(loaded.players) == 2
        )

        await reader.rollback()
        if writer_task is not None and not writer_task.done():
            await asyncio.wait_for(writer_task, timeout=5)
        latest = await reader_storage.load_current(group, for_update=False)
        assert latest is not None
        assert latest.state_revision == created.state_revision + 1
        assert len(latest.players) == 2
    finally:
        if writer_task is not None and not writer_task.done():
            writer_task.cancel()
            with suppress(asyncio.CancelledError):
                await writer_task
        with suppress(Exception):
            await reader.rollback()
        with suppress(Exception):
            await writer.rollback()
        await observer.close()
        await reader.close()
        await writer.close()


async def test_terminal_save_and_projection_share_one_scope_lock_order(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    _app_id, _group_openid, group = db_scope
    saver = open_session()
    projector = open_session()
    observer = open_session()
    projector_task: asyncio.Task[None] | None = None
    try:
        saver_pid = await _backend_pid(saver)
        _final_snapshot, projection, _active_before_terminal = await _persist_completed(
            saver,
            group,
            project=False,
        )
        projector_pid = await _backend_pid(projector)
        projector_task = asyncio.create_task(
            PostgresRouletteStorage(projector).project_terminal(projection)
        )
        blockers = await _wait_until_blocked(observer, projector_pid)
        assert saver_pid in blockers

        await asyncio.wait_for(
            PostgresRouletteStorage(saver).project_terminal(projection),
            timeout=5,
        )
        await asyncio.wait_for(saver.commit(), timeout=5)
        await asyncio.wait_for(projector_task, timeout=5)
        await projector.commit()
    finally:
        if projector_task is not None and not projector_task.done():
            projector_task.cancel()
            with suppress(asyncio.CancelledError):
                await projector_task
        with suppress(Exception):
            await saver.rollback()
        with suppress(Exception):
            await projector.rollback()
        await observer.close()
        await saver.close()
        await projector.close()

    verify = open_session()
    try:
        storage = PostgresRouletteStorage(verify)
        result = await storage.get_result(group, projection.game_id)
        assert result is not None
        assert result.lifecycle == "completed"
        assert result.winner_seq == 3
        rows = await storage.list_leaderboard(group)
        assert len(rows) == 1
        assert rows[0].wins == 1
    finally:
        await verify.close()


async def test_blocked_transition_preserves_domain_deadline(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    _app_id, _group_openid, group = db_scope
    setup = open_session()
    blocker = open_session()
    writer = open_session()
    observer = open_session()
    writer_task: asyncio.Task[GameSnapshot] | None = None
    try:
        await _persist_active(
            setup,
            group,
            chamber=(
                ChamberKind.BLANK,
                ChamberKind.LIVE,
                ChamberKind.LIVE,
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.BLANK,
            ),
        )
        await setup.close()

        before = await PostgresRouletteStorage(writer).load_current(group)
        assert before is not None
        await writer.rollback()
        state = game_state_from_snapshot(before)
        actor = next(
            seat.player
            for seat in state.players
            if seat.join_seq == state.current_player_seq
        )
        changed = apply_action(
            state,
            Action.shoot(actor),
            now=START + timedelta(minutes=10),
            random_source=DeterministicRandom(),
        )
        assert changed.ok
        transition = transition_from_action_result(
            before,
            changed,
            action_kind="shoot",
            occurred_at=await _pg_now(writer),
        )
        expected_deadline = transition.after.deadline
        assert expected_deadline is not None

        locked = await PostgresRouletteStorage(blocker).load_current(
            group,
            for_update=True,
        )
        assert locked is not None
        writer_pid = await _backend_pid(writer)
        writer_task = asyncio.create_task(
            PostgresRouletteStorage(writer).save_transition(
                transition,
                expected_revision=before.state_revision,
            )
        )
        await _wait_until_blocked(observer, writer_pid)
        await blocker.rollback()
        saved = await asyncio.wait_for(writer_task, timeout=5)
        await writer.commit()
        assert saved.deadline == expected_deadline
    finally:
        if writer_task is not None and not writer_task.done():
            writer_task.cancel()
            with suppress(asyncio.CancelledError):
                await writer_task
        with suppress(Exception):
            await blocker.rollback()
        with suppress(Exception):
            await writer.rollback()
        with suppress(Exception):
            await setup.close()
        await observer.close()
        await blocker.close()
        await writer.close()

    verify = open_session()
    try:
        restored = await PostgresRouletteStorage(verify).load_current(group)
        assert restored is not None
        assert restored.deadline == expected_deadline
    finally:
        await verify.close()


async def test_public_load_refreshes_a_caller_session_identity_map_and_cas(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    _app_id, _group_openid, group = db_scope
    created = await _create_waiting(group)
    reader = open_session()
    writer = open_session()
    try:
        reader_storage = PostgresRouletteStorage(reader)
        writer_storage = PostgresRouletteStorage(writer)
        old = await reader_storage.load_current(group)
        assert old is not None
        held_root = (
            await reader.execute(
                select(RouletteGameRow).where(
                    RouletteGameRow.__table__.c.game_id == old.game_id
                )
            )
        ).scalar_one()
        held_players = list(
            (
                await reader.execute(
                    select(RoulettePlayerRow)
                    .where(RoulettePlayerRow.__table__.c.game_id == old.game_id)
                    .order_by(RoulettePlayerRow.__table__.c.join_seq)
                )
            ).scalars()
        )
        assert held_root.state_revision == old.state_revision
        assert [row.join_seq for row in held_players] == [1]
        old_state = game_state_from_snapshot(old)
        actor = player_for(
            group,
            2,
            name="外部新版本",
            member_openid="identity-map-fresh-member",
        )
        stale_result = apply_action(
            old_state,
            Action.join(actor),
            now=START,
            random_source=DeterministicRandom(),
        )
        assert stale_result.ok and stale_result.code == "joined"
        stale_transition = transition_from_action_result(
            old,
            stale_result,
            action_kind="join",
            occurred_at=await _pg_now(reader),
        )

        current = await writer_storage.load_current(group)
        assert current is not None
        joined = apply_action(
            game_state_from_snapshot(current),
            Action.join(actor),
            now=START,
            random_source=DeterministicRandom(),
        )
        assert joined.ok and joined.code == "joined"
        external_transition = transition_from_action_result(
            current,
            joined,
            action_kind="join",
            occurred_at=await _pg_now(writer),
        )
        await writer_storage.save_transition(
            external_transition,
            expected_revision=current.state_revision,
        )
        await writer.commit()

        refreshed = await reader_storage.load_current(group)
        assert refreshed is not None
        assert refreshed.state_revision == created.state_revision + 1
        assert [seat.join_seq for seat in refreshed.players] == [1, 2]
        assert held_root.state_revision == refreshed.state_revision
        assert held_players[0].join_seq == 1
        with pytest.raises(RevisionConflictError):
            await reader_storage.save_transition(
                stale_transition,
                expected_revision=old.state_revision,
            )
        await reader.rollback()
        latest = await reader_storage.load_current(group)
        assert latest is not None
        assert latest.state_revision == refreshed.state_revision
        assert [seat.join_seq for seat in latest.players] == [1, 2]
    finally:
        await reader.close()
        await writer.close()


async def test_activity_slot_isolated_by_app_and_group(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    _app_id, _group_openid, group = db_scope
    other_app = group_for(f"{group.app_id}-other", group.group_openid)
    other_group = group_for(group.app_id, f"{group.group_openid}-other")
    sessions = [open_session(), open_session(), open_session()]
    try:
        storages = [PostgresRouletteStorage(session) for session in sessions]
        await asyncio.gather(
            storages[0].create_waiting(_snapshot(waiting_state(group))),
            storages[1].create_waiting(_snapshot(waiting_state(other_app))),
            storages[2].create_waiting(_snapshot(waiting_state(other_group))),
        )
        await asyncio.gather(*(session.commit() for session in sessions))
    finally:
        await asyncio.gather(*(session.close() for session in sessions))

    for isolated_group in (group, other_app, other_group):
        session = open_session()
        try:
            assert await PostgresRouletteStorage(session).load_current(isolated_group)
        finally:
            await session.close()
    # The fixture owns only its base scope; clean the two additional scopes here.
    await clear_scope(other_app.app_id, other_app.group_openid)
    await clear_scope(other_group.app_id, other_group.group_openid)


async def test_save_transition_rejects_stale_revision_and_keeps_rows_unchanged(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    _app_id, _group_openid, group = db_scope
    created = await _create_waiting(group)
    before = game_state_from_snapshot(created)

    first_session = open_session()
    try:
        first_storage = PostgresRouletteStorage(first_session)
        actor = player_for(group, 2, name="新加入者")
        joined = apply_action(
            before,
            Action.join(actor),
            now=START,
            random_source=DeterministicRandom(),
        )
        assert joined.ok and joined.code == "joined"
        first_transition = transition_from_action_result(
            created,
            joined,
            action_kind="join",
            occurred_at=await _pg_now(first_session),
        )
        saved = await first_storage.save_transition(
            first_transition,
            expected_revision=before.state_revision,
        )
        await first_session.commit()
        assert game_state_from_snapshot(saved).state_revision == 2
    finally:
        await first_session.close()

    stale_session = open_session()
    try:
        with pytest.raises(RevisionConflictError):
            await PostgresRouletteStorage(stale_session).save_transition(
                first_transition,
                expected_revision=before.state_revision,
            )
        await stale_session.rollback()
    finally:
        await stale_session.close()

    verify_session = open_session()
    try:
        latest = await PostgresRouletteStorage(verify_session).load_current(group)
        assert latest is not None
        restored = game_state_from_snapshot(latest)
        assert restored.state_revision == 2
        assert len(restored.players) == 2
    finally:
        await verify_session.close()


@pytest.mark.parametrize(
    ("lifecycle", "reason"),
    (
        ("cancelled", "host_cancelled"),
        ("expired", "waiting_timeout"),
    ),
)
async def test_non_completed_terminal_projection_has_no_winner_or_win(
    db_scope: tuple[str, str, GroupRef],
    lifecycle: str,
    reason: str,
) -> None:
    _app_id, _group_openid, group = db_scope
    session = open_session()
    try:
        snapshot = await _persist_waiting_players(session, group)
        state = game_state_from_snapshot(snapshot)
        if lifecycle == "cancelled":
            result = apply_action(
                state,
                Action.cancel(state.players[0].player),
                now=START,
                random_source=DeterministicRandom(),
            )
        else:
            result = apply_action(
                state,
                Action.expire(),
                now=START + timedelta(minutes=16),
                random_source=DeterministicRandom(),
            )
        assert result.state.lifecycle == lifecycle
        transition = transition_from_action_result(
            snapshot,
            result,
            action_kind="cancel" if lifecycle == "cancelled" else "expire",
            occurred_at=await _pg_now(session),
        )
        storage = PostgresRouletteStorage(session)
        terminal_snapshot = await storage.save_transition(
            transition,
            expected_revision=state.state_revision,
        )
        projection = TerminalProjection.from_state(
            terminal_snapshot,
            lifecycle=lifecycle,
            reason=reason,
            ended_at=await _pg_now(session),
            winner_seq=None,
        )
        await storage.project_terminal(projection)
        await session.commit()
        result_record = await storage.get_result(group, snapshot.game_id)
        leaderboard = await storage.list_leaderboard(group)
        assert result_record.lifecycle == lifecycle
        assert result_record.winner_member_openid is None
        assert leaderboard == ()
    finally:
        await session.close()

    counts = await count_scope_rows(group.app_id, group.group_openid)
    assert counts["komari_roulette_players"] == 0
    assert counts["komari_roulette_results"] == 1
    assert counts["komari_roulette_result_players"] == 2
    assert counts["komari_roulette_leaderboard"] == 0


async def test_completed_projection_cleans_runtime_and_adds_one_rebuildable_win(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    _app_id, _group_openid, group = db_scope
    session = open_session()
    try:
        active_snapshot, projection, _active_before_terminal = await _persist_completed(
            session,
            group,
            names=("冻结甲", "冻结乙", "冻结胜者"),
        )
    finally:
        await session.close()

    # Re-read the completed proof and leaderboard in an independent transaction.
    read_session = open_session()
    try:
        storage = PostgresRouletteStorage(read_session)
        result = await storage.get_result(group, active_snapshot.game_id)
        assert (
            result.winner_member_openid
            == game_state_from_snapshot(active_snapshot).players[2].member_openid
        )
        assert result.winner_display_name == "冻结胜者"
        assert result.created_at is not None
        assert result.started_at is not None
        assert result.ended_at is not None
        assert result.created_at <= result.started_at <= result.ended_at
        assert result.ended_at >= result.started_at
        result_players = {row.join_seq: row for row in result.players}
        assert [result_players[index].display_name for index in (1, 2, 3)] == [
            "冻结甲",
            "冻结乙",
            "冻结胜者",
        ]
        assert result_players[1].eliminated_order == 1
        assert result_players[1].eliminated_reason == "shot"
        assert result_players[1].eliminated_at is not None
        assert result_players[2].eliminated_order == 2
        assert result_players[2].eliminated_reason == "forfeit"
        assert result_players[2].eliminated_at is not None
        assert (
            result_players[1].eliminated_at
            <= result_players[2].eliminated_at
            <= result.ended_at
        )
        assert result_players[3].eliminated_order is None

        rows = await storage.list_leaderboard(group)
        assert len(rows) == 1
        assert rows[0].wins == 1
        assert rows[0].display_name == "冻结胜者"
    finally:
        await read_session.close()

    counts = await count_scope_rows(group.app_id, group.group_openid)
    assert counts["komari_roulette_players"] == 0
    assert counts["komari_roulette_results"] == 1
    assert counts["komari_roulette_result_players"] == 3

    cleanup_session = open_session()
    try:
        cleanup_row = (
            await cleanup_session.execute(
                text(
                    "SELECT ordered_chamber, pending_rewards, pending_burst "
                    "FROM komari_roulette_games WHERE game_id = :game_id"
                ),
                {"game_id": active_snapshot.game_id},
            )
        ).mappings().one()
        assert list(cleanup_row["ordered_chamber"] or ()) == []
        assert list(cleanup_row["pending_rewards"] or ()) == []
        assert cleanup_row["pending_burst"] is False
    finally:
        await cleanup_session.close()

    # Replaying a terminal projection is a no-op read of the immutable result.
    replay_session = open_session()
    try:
        replay_storage = PostgresRouletteStorage(replay_session)
        await replay_storage.project_terminal(projection)
        await replay_session.commit()
        replay_rows = await replay_storage.list_leaderboard(group)
        assert len(replay_rows) == 1
        assert replay_rows[0].wins == 1
    finally:
        await replay_session.close()


async def test_terminal_and_leaderboard_roll_back_together_on_later_failure(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    """A caller transaction cannot leave result rows without their win."""

    app_id, group_openid, group = db_scope
    session = open_session()
    try:
        storage = PostgresRouletteStorage(session)
        _final_snapshot, projection, active_before_terminal = await _persist_completed(
            session,
            group,
            project=False,
        )
        await storage.project_terminal(projection)
        # A later SQL failure in the same UoW must erase the first projection
        # and its win.  The division-by-zero is deliberately independent of
        # implementation column names and models a failed downstream write.
        with pytest.raises(SQLAlchemyError):
            await session.execute(text("SELECT 1 / 0"))
        await session.rollback()
    finally:
        await session.close()

    counts = await count_scope_rows(app_id, group_openid)
    assert counts["komari_roulette_games"] == 1
    assert counts["komari_roulette_players"] == 3
    assert counts["komari_roulette_results"] == 0
    assert counts["komari_roulette_result_players"] == 0
    assert counts["komari_roulette_leaderboard"] == 0

    verify_session = open_session()
    try:
        restored_snapshot = await PostgresRouletteStorage(verify_session).load_current(
            group
        )
        assert restored_snapshot is not None
        restored = game_state_from_snapshot(restored_snapshot)
        expected = game_state_from_snapshot(active_before_terminal)
        assert restored.lifecycle == "active"
        assert restored.state_revision == expected.state_revision
        assert restored.chamber_revision == expected.chamber_revision
        assert restored.turn_seq == expected.turn_seq
        assert restored.current_player_seq == expected.current_player_seq
        assert restored.ordered_chamber == expected.ordered_chamber
        assert restored.pending_rewards == expected.pending_rewards
        assert restored.pending_locks == expected.pending_locks
        assert [seat.alive for seat in restored.players] == [False, True, True]
        assert [
            (seat.join_seq, seat.alive, dict(seat.inventory))
            for seat in restored.players
        ] == [
            (seat.join_seq, seat.alive, dict(seat.inventory))
            for seat in expected.players
        ]
    finally:
        await verify_session.close()


async def test_result_projection_is_immutable_and_name_is_a_frozen_snapshot(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    _app_id, _group_openid, group = db_scope
    session = open_session()
    try:
        storage = PostgresRouletteStorage(session)
        (
            completed_snapshot,
            _projection,
            _active_before_terminal,
        ) = await _persist_completed(
            session,
            group,
            names=("首次名字", "另一名字", "首次胜者"),
        )
        result_before = await storage.get_result(group, completed_snapshot.game_id)
        changed = TerminalProjection.from_state(
            _snapshot(
                active_state(
                    group,
                    player_count=3,
                    names=("篡改名字", "另一名字", "篡改胜者"),
                    member_openids=(
                        result_before.players[0].member_openid,
                        result_before.players[1].member_openid,
                        result_before.winner_member_openid,
                    ),
                ),
                completed_snapshot.game_id,
            ),
            lifecycle="completed",
            reason="other_reason",
            ended_at=await _pg_now(session),
            winner_seq=3,
        )
        await storage.project_terminal(changed)
        await session.commit()
        result_after = await storage.get_result(group, completed_snapshot.game_id)
        assert result_after.winner_display_name == result_before.winner_display_name
        assert result_after.reason == result_before.reason
    finally:
        await session.close()


async def test_leaderboard_rebuild_uses_results_and_orders_without_exposing_openids(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    _app_id, _group_openid, group = db_scope
    member_a = "member-a"
    member_b = "member-b"
    session = open_session()
    try:
        storage = PostgresRouletteStorage(session)
        await _persist_completed(
            session,
            group,
            names=("败者A1", "败者A2", "A旧"),
            member_openids=("loser-a-1", "loser-a-2", member_a),
        )
        await _persist_completed(
            session,
            group,
            names=("败者B1", "败者B2", "B旧"),
            member_openids=("loser-b-1", "loser-b-2", member_b),
        )
        await _persist_completed(
            session,
            group,
            names=("败者B3", "败者B4", "B新"),
            member_openids=("loser-b-3", "loser-b-4", member_b),
        )
        await _persist_completed(
            session,
            group,
            names=("败者A3", "败者A4", "A新"),
            member_openids=("loser-a-3", "loser-a-4", member_a),
        )
        # Simulate a lost/stale projection; rebuilding must derive only from
        # immutable completed results and preserve frozen names.
        await storage.rebuild_leaderboard(group)
        await session.commit()
        rows = await storage.list_leaderboard(group)
        assert [row.wins for row in rows] == [2, 2]
        assert [row.display_name for row in rows] == ["B新", "A新"]
        assert rows[0].last_won_at <= rows[1].last_won_at
        assert all("member" not in row.display_name for row in rows)
    finally:
        await session.close()
