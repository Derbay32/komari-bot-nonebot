"""TSK-275 PostgreSQL-local constraints and relation shape."""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from komari_bot.plugins.komari_roulette.mapper import game_state_to_snapshot
from komari_bot.plugins.komari_roulette.storage import PostgresRouletteStorage
from tests.komari_roulette.storage_support import (
    POSTGRES_URL,
    SQLALCHEMY_URL,
    clear_scope,
    group_for,
    open_session,
    reset_shared_orm_engine,
    same_database,
    scope,
    waiting_state,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from komari_bot.plugins.komari_roulette.domain import GroupRef


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
    app_id, group_openid = scope("constraints")
    group = group_for(app_id, group_openid)
    await reset_shared_orm_engine()
    try:
        yield app_id, group_openid, group
    finally:
        with suppress(Exception):
            await clear_scope(app_id, group_openid)
        await reset_shared_orm_engine()


async def _metadata(table: str) -> dict[str, dict[str, Any]]:
    session = open_session()
    try:
        rows = await session.execute(
            text(
                "SELECT column_name, data_type, udt_name, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_name = :table ORDER BY ordinal_position"
            ),
            {"table": table},
        )
        return {str(row.column_name): dict(row._mapping) for row in rows}
    finally:
        await session.close()


async def _constraints(table: str) -> list[str]:
    session = open_session()
    try:
        rows = await session.execute(
            text(
                "SELECT pg_get_constraintdef(pg_constraint.oid) AS definition "
                "FROM pg_constraint "
                "JOIN pg_class ON pg_class.oid = pg_constraint.conrelid "
                "WHERE pg_class.relname = :table"
            ),
            {"table": table},
        )
        return [str(row.definition) for row in rows]
    finally:
        await session.close()


async def test_relationized_schema_has_closed_core_tables_and_types(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    _ = db_scope
    expected = {
        "komari_roulette_games",
        "komari_roulette_players",
        "komari_roulette_results",
        "komari_roulette_result_players",
        "komari_roulette_leaderboard",
    }
    session = open_session()
    try:
        rows = await session.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name LIKE 'komari_roulette_%'"
            )
        )
        actual = {str(row.table_name) for row in rows}
    finally:
        await session.close()

    assert expected <= actual
    game_columns = await _metadata("komari_roulette_games")
    player_columns = await _metadata("komari_roulette_players")
    assert {
        "game_id",
        "app_id",
        "group_openid",
        "lifecycle",
        "state_revision",
        "chamber_revision",
        "ordered_chamber",
        "pending_rewards",
        "item_weights",
        "next_join_seq",
    } <= set(game_columns)
    assert {
        "game_id",
        "join_seq",
        "member_openid",
        "display_name",
        "alive",
        "magnifier_count",
        "beer_count",
        "burst_count",
        "lock_count",
    } <= set(player_columns)
    assert game_columns["ordered_chamber"]["udt_name"] == "_text"
    assert game_columns["pending_rewards"]["udt_name"] == "_text"
    assert game_columns["ordered_chamber"]["data_type"] == "ARRAY"
    assert game_columns["pending_rewards"]["data_type"] == "ARRAY"


async def test_schema_constraints_cover_lifecycle_arrays_inventory_and_fk(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    _ = db_scope
    game_constraints = await _constraints("komari_roulette_games")
    player_constraints = await _constraints("komari_roulette_players")
    result_constraints = await _constraints("komari_roulette_results")
    result_player_constraints = await _constraints("komari_roulette_result_players")
    leaderboard_constraints = await _constraints("komari_roulette_leaderboard")

    all_constraints = game_constraints + player_constraints + result_constraints
    all_constraints += result_player_constraints + leaderboard_constraints
    joined = " ".join(all_constraints).lower()
    assert "lifecycle" in joined
    assert "waiting" in joined and "active" in joined
    assert "ordered_chamber" in joined
    assert "pending_rewards" in joined
    assert "magnifier_count" in joined
    assert "beer_count" in joined
    assert "burst_count" in joined
    assert "lock_count" in joined
    assert "foreign key" in joined
    assert "game_id" in " ".join(result_constraints).lower()
    assert "unique" in " ".join(result_constraints).lower()
    assert "wins" in " ".join(leaderboard_constraints).lower()
    # The current binding relation is intentionally absent: repair may delete
    # it while historical game/result snapshots remain durable.
    assert "character_binding" not in joined

    session = open_session()
    try:
        indexes = await session.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' "
                "AND tablename = 'komari_roulette_games'"
            )
        )
        index_defs = [str(row.indexdef).lower() for row in indexes]
    finally:
        await session.close()
    assert any(
        "unique" in definition
        and "app_id" in definition
        and "group_openid" in definition
        and "waiting" in definition
        and "active" in definition
        for definition in index_defs
    )


async def test_database_rejects_invalid_closed_arrays_and_inventory(
    db_scope: tuple[str, str, GroupRef],
) -> None:
    _app_id, _group_openid, group = db_scope
    session = open_session()
    try:
        storage = PostgresRouletteStorage(session)
        snapshot = game_state_to_snapshot(waiting_state(group), game_id=str(uuid4()))
        await storage.create_waiting(snapshot)
        await session.commit()
        game_id = snapshot.game_id
        baseline = await storage.load_current(group)
        assert baseline is not None

        invalid_updates = (
            "UPDATE komari_roulette_games SET lifecycle = 'unknown' WHERE game_id = :game_id",
            "UPDATE komari_roulette_games SET ordered_chamber = ARRAY['bogus']::text[] WHERE game_id = :game_id",
            "UPDATE komari_roulette_games SET ordered_chamber = ARRAY['live','live','live','live','live','live','live']::text[] WHERE game_id = :game_id",
            "UPDATE komari_roulette_games SET pending_rewards = ARRAY['bogus']::text[] WHERE game_id = :game_id",
            "UPDATE komari_roulette_games SET pending_rewards = ARRAY['beer','beer','beer','beer','beer']::text[] WHERE game_id = :game_id",
            "UPDATE komari_roulette_players SET magnifier_count = -1 WHERE game_id = :game_id",
            "UPDATE komari_roulette_players SET magnifier_count = 2, beer_count = 1, burst_count = 1, lock_count = 1 WHERE game_id = :game_id",
        )
        for statement in invalid_updates:
            with pytest.raises(IntegrityError):
                await session.execute(text(statement), {"game_id": game_id})
            await session.rollback()
            restored = await storage.load_current(group)
            assert restored is not None
            assert restored.game_id == baseline.game_id
            assert restored.state_revision == baseline.state_revision
            assert restored.players == baseline.players
    finally:
        await session.close()
