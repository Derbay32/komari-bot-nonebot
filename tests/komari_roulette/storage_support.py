"""Shared real-PostgreSQL fixtures for the TSK-275 storage contract."""

from __future__ import annotations

import os
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from uuid import uuid4

from komari_bot.plugins.komari_roulette.domain import (
    Action,
    ChamberKind,
    GameState,
    GroupRef,
    ItemType,
    PlayerRef,
    apply_action,
    initial_state,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")
SQLALCHEMY_URL = os.getenv("SQLALCHEMY_DATABASE_URL", "")
REDIS_URL = os.getenv("KOMARI_TEST_REDIS_URL", "")

START = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
TURN = timedelta(minutes=15)


def same_database(left: str, right: str) -> bool:
    """Compare the database target while ignoring driver scheme differences."""

    left_parsed = urlparse(left.replace("postgresql+asyncpg://", "postgresql://"))
    right_parsed = urlparse(right.replace("postgresql+asyncpg://", "postgresql://"))
    return (
        left_parsed.hostname == right_parsed.hostname
        and (left_parsed.port or 5432) == (right_parsed.port or 5432)
        and left_parsed.path == right_parsed.path
    )


def scope(tag: str = "game") -> tuple[str, str]:
    """Return an app/group key unique to this test process."""

    suffix = f"{tag}-{uuid4().hex}"
    return f"test-tsk275-app-{suffix}", f"test-tsk275-group-{suffix}"


def group_for(app_id: str, group_openid: str) -> GroupRef:
    return GroupRef(app_id=app_id, group_openid=group_openid)


def player_for(
    group: GroupRef,
    number: int,
    *,
    name: str | None = None,
    member_openid: str | None = None,
) -> PlayerRef:
    return PlayerRef(
        app_id=group.app_id,
        group_openid=group.group_openid,
        member_openid=member_openid or f"member-{number}-{uuid4().hex}",
        display_name=name or f"Player {number}",
    )


class DeterministicRandom:
    """Random boundary used to build trusted states without global entropy."""

    def __init__(
        self,
        chambers: Iterable[Sequence[ChamberKind]] = (),
        items: Iterable[ItemType] = (),
    ) -> None:
        self.chambers = [tuple(order) for order in chambers]
        self.items = list(items)

    def chamber_order(
        self,
        live_count: int,
        blank_count: int,
    ) -> tuple[ChamberKind, ...]:
        if not self.chambers:
            raise AssertionError
        order = self.chambers.pop(0)
        assert len(order) == live_count + blank_count
        return order

    def weighted_item(self, weights: Mapping[ItemType, int]) -> ItemType:
        if not self.items:
            raise AssertionError
        item = self.items.pop(0)
        assert weights[item] > 0
        return item


def waiting_state(
    group: GroupRef,
    *,
    player_count: int = 1,
    names: Sequence[str] | None = None,
    member_openids: Sequence[str] | None = None,
) -> GameState:
    """Construct a waiting state through the public domain seam."""

    first = player_for(
        group,
        1,
        name=names[0] if names else None,
        member_openid=member_openids[0] if member_openids else None,
    )
    created = apply_action(
        initial_state(group),
        Action.create(first),
        now=START,
        random_source=DeterministicRandom(),
    )
    assert created.ok and created.code == "created"
    state = created.state
    for number in range(2, player_count + 1):
        joined = apply_action(
            state,
            Action.join(
                player_for(
                    group,
                    number,
                    name=names[number - 1] if names else None,
                    member_openid=(
                        member_openids[number - 1] if member_openids else None
                    ),
                )
            ),
            now=START,
            random_source=DeterministicRandom(),
        )
        assert joined.ok and joined.code == "joined"
        state = joined.state
    return state


def active_state(
    group: GroupRef,
    *,
    player_count: int = 2,
    names: Sequence[str] | None = None,
    member_openids: Sequence[str] | None = None,
    chamber: Sequence[ChamberKind] | None = None,
    pending_rewards: Sequence[ItemType] = (),
    pending_locks: Sequence[int] = (),
) -> GameState:
    """Construct a deterministic active state for mapper/storage tests."""

    state = waiting_state(
        group,
        player_count=player_count,
        names=names,
        member_openids=member_openids,
    )
    entropy = DeterministicRandom(
        chambers=(
            chamber
            or (
                ChamberKind.BLANK,
                ChamberKind.LIVE,
                ChamberKind.BLANK,
                ChamberKind.BLANK,
                ChamberKind.LIVE,
                ChamberKind.BLANK,
            ),
        )
    )
    started = apply_action(
        state,
        Action.start(
            state.players[0].player,
            item_weights={
                ItemType.MAGNIFIER: 4,
                ItemType.BEER: 3,
                ItemType.BURST: 2,
                ItemType.LOCK: 1,
            },
        ),
        now=START,
        random_source=entropy,
    )
    assert started.ok and started.code == "started"
    runtime_players = list(started.state.players)
    if pending_rewards:
        runtime_players[0] = replace(
            runtime_players[0],
            inventory={
                ItemType.MAGNIFIER: 1,
                ItemType.BEER: 1,
                ItemType.BURST: 1,
                ItemType.LOCK: 1,
            },
        )
    candidate = GameState.from_trusted_snapshot(
        {
            "group": group,
            "lifecycle": started.state.lifecycle,
            "phase": "item_choice" if pending_rewards else started.state.phase,
            "state_revision": started.state.state_revision,
            "chamber_revision": started.state.chamber_revision,
            "turn_seq": started.state.turn_seq,
            "current_player_seq": started.state.current_player_seq,
            "deadline": started.state.deadline,
            "host_seq": started.state.host_seq,
            "players": tuple(runtime_players),
            "ordered_chamber": started.state.ordered_chamber,
            "pending_rewards": tuple(pending_rewards),
            "pending_burst": False,
            "pending_locks": tuple(pending_locks),
            "item_weights": dict(started.state.item_weights),
            "next_join_seq": started.state.next_join_seq,
        }
    )
    probe = apply_action(
        candidate,
        (
            Action.choose_item(candidate.players[0].player, "discard")
            if pending_rewards
            else Action.open_item_panel(candidate.players[0].player)
        ),
        now=START,
        random_source=DeterministicRandom(),
    )
    assert probe.code not in {"invalid_game_state", "not_participant"}
    return candidate


def player_member_ids(state: GameState) -> tuple[str, ...]:
    return tuple(seat.member_openid for seat in state.players)


def active_state_with_join_gap(group: GroupRef) -> GameState:
    """Build a legal active roster whose next join sequence is greater than 6."""

    state = waiting_state(group, player_count=6)
    returning_player = state.players[1].player
    for seat in state.players[1:5]:
        left = apply_action(
            state,
            Action.leave(seat.player),
            now=START,
            random_source=DeterministicRandom(),
        )
        assert left.ok and left.code == "left"
        state = left.state
    rejoined = apply_action(
        state,
        Action.join(returning_player),
        now=START,
        random_source=DeterministicRandom(),
    )
    assert rejoined.ok and rejoined.code == "joined"
    state = rejoined.state
    assert [seat.join_seq for seat in state.players] == [1, 6, 7]
    started = apply_action(
        state,
        Action.start(
            state.players[0].player,
            item_weights={
                ItemType.MAGNIFIER: 4,
                ItemType.BEER: 3,
                ItemType.BURST: 2,
                ItemType.LOCK: 1,
            },
        ),
        now=START,
        random_source=DeterministicRandom(
            chambers=(
                (
                    ChamberKind.BLANK,
                    ChamberKind.LIVE,
                    ChamberKind.BLANK,
                    ChamberKind.BLANK,
                    ChamberKind.LIVE,
                    ChamberKind.BLANK,
                ),
            )
        ),
    )
    assert started.ok and started.code == "started"
    return started.state


async def reset_shared_orm_engine() -> None:
    """Dispose nonebot-plugin-orm pools between pytest event loops."""

    from nonebot import require

    require("nonebot_plugin_orm")
    import nonebot_plugin_orm as orm_module

    engines = getattr(orm_module, "_engines", None)
    if not engines:
        return
    for engine in list(engines.values()):
        with suppress(Exception):
            await engine.dispose()


def open_session() -> "AsyncSession":
    from nonebot_plugin_orm import get_session

    return get_session(expire_on_commit=False)


async def clear_scope(app_id: str, group_openid: str) -> None:
    """Delete only this test scope in FK order using the shared ORM session."""

    from sqlalchemy import text

    session = open_session()
    try:
        scope_params = {"app_id": app_id, "group_openid": group_openid}
        await session.execute(
            text(
                "DELETE FROM komari_roulette_result_players "
                "WHERE game_id IN (SELECT game_id FROM komari_roulette_games "
                "WHERE app_id = :app_id AND group_openid = :group_openid)"
            ),
            scope_params,
        )
        await session.execute(
            text(
                "DELETE FROM komari_roulette_results "
                "WHERE game_id IN (SELECT game_id FROM komari_roulette_games "
                "WHERE app_id = :app_id AND group_openid = :group_openid)"
            ),
            scope_params,
        )
        for table in ("komari_roulette_leaderboard",):
            await session.execute(
                text(
                    f"DELETE FROM {table} "
                    "WHERE app_id = :app_id AND group_openid = :group_openid"
                ),
                scope_params,
            )
        await session.execute(
            text(
                "DELETE FROM komari_roulette_players "
                "WHERE game_id IN (SELECT game_id FROM komari_roulette_games "
                "WHERE app_id = :app_id AND group_openid = :group_openid)"
            ),
            scope_params,
        )
        await session.execute(
            text(
                "DELETE FROM komari_roulette_games "
                "WHERE app_id = :app_id AND group_openid = :group_openid"
            ),
            scope_params,
        )
        await session.commit()
    finally:
        await session.close()


async def count_scope_rows(app_id: str, group_openid: str) -> dict[str, int]:
    from sqlalchemy import text

    session = open_session()
    try:
        counts: dict[str, int] = {}
        for table in ("komari_roulette_games", "komari_roulette_leaderboard"):
            result = await session.execute(
                text(
                    f"SELECT count(*) FROM {table} "
                    "WHERE app_id = :app_id AND group_openid = :group_openid"
                ),
                {"app_id": app_id, "group_openid": group_openid},
            )
            counts[table] = int(result.scalar_one())
        result = await session.execute(
            text(
                "SELECT count(*) FROM komari_roulette_results "
                "WHERE game_id IN (SELECT game_id FROM komari_roulette_games "
                "WHERE app_id = :app_id AND group_openid = :group_openid)"
            ),
            {"app_id": app_id, "group_openid": group_openid},
        )
        counts["komari_roulette_results"] = int(result.scalar_one())
        result = await session.execute(
            text(
                "SELECT count(*) FROM komari_roulette_players "
                "WHERE game_id IN (SELECT game_id FROM komari_roulette_games "
                "WHERE app_id = :app_id AND group_openid = :group_openid)"
            ),
            {"app_id": app_id, "group_openid": group_openid},
        )
        counts["komari_roulette_players"] = int(result.scalar_one())
        result = await session.execute(
            text(
                "SELECT count(*) FROM komari_roulette_result_players "
                "WHERE game_id IN (SELECT game_id FROM komari_roulette_games "
                "WHERE app_id = :app_id AND group_openid = :group_openid)"
            ),
            {"app_id": app_id, "group_openid": group_openid},
        )
        counts["komari_roulette_result_players"] = int(result.scalar_one())
        return counts
    finally:
        await session.close()


async def fetch_raw_scope(
    app_id: str,
    group_openid: str,
    *,
    game_id: str | None = None,
) -> dict[str, list[dict[str, object]]]:
    """Read storage rows for invariant/cleanup assertions only."""

    from sqlalchemy import text

    session = open_session()
    try:
        rows: dict[str, list[dict[str, object]]] = {}
        where = "app_id = :app_id AND group_openid = :group_openid"
        params: dict[str, object] = {
            "app_id": app_id,
            "group_openid": group_openid,
        }
        if game_id is not None:
            where += " AND game_id = :game_id"
            params["game_id"] = game_id
        for table in (
            "komari_roulette_games",
            "komari_roulette_leaderboard",
        ):
            result = await session.execute(
                text(f"SELECT * FROM {table} WHERE {where}"), params
            )
            rows[table] = [dict(row._mapping) for row in result.fetchall()]
        result = await session.execute(
            text(
                "SELECT * FROM komari_roulette_results "
                "WHERE game_id IN (SELECT game_id FROM komari_roulette_games "
                f"WHERE {where})"
            ),
            params,
        )
        rows["komari_roulette_results"] = [
            dict(row._mapping) for row in result.fetchall()
        ]
        player_where = (
            f"game_id IN (SELECT game_id FROM komari_roulette_games WHERE {where})"
        )
        result = await session.execute(
            text(f"SELECT * FROM komari_roulette_players WHERE {player_where}"),
            params,
        )
        rows["komari_roulette_players"] = [
            dict(row._mapping) for row in result.fetchall()
        ]
        result = await session.execute(
            text(
                "SELECT * FROM komari_roulette_result_players "
                "WHERE game_id IN (SELECT game_id FROM komari_roulette_games "
                f"WHERE {where})"
            ),
            params,
        )
        rows["komari_roulette_result_players"] = [
            dict(row._mapping) for row in result.fetchall()
        ]
        return rows
    finally:
        await session.close()
