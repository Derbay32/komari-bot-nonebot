"""TSK-276 command transaction, replay, and commit-boundary tests."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from komari_bot.plugins.character_binding.manager import CharacterBindingManager
from komari_bot.plugins.komari_roulette import (
    CommandReceipt,
    CommitOutcomeUnknownError,
    ExpiryAdvance,
    IdempotencyKeyConflictError,
    ReplyProjection,
    ReplyProjectionContext,
    RouletteCommandService,
    StorageUnavailableError,
)
from komari_bot.plugins.komari_roulette.domain import ChamberKind, ItemType

from .command_support import (
    PG_REQUIRED,
    Scope,
    backend_pid,
    command_factory,
    count_rows,
    create_engine_and_factory,
    delete_scope,
    hold_group_lock,
    observation,
    request,
    reset_shared_orm_engine,
    scope,
    seed_binding,
    wait_for_blocked,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

    from komari_bot.plugins.komari_roulette.domain import RandomSource


pytestmark = [pytest.mark.asyncio, PG_REQUIRED]


@dataclass(frozen=True, slots=True)
class Harness:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    binding_manager: CharacterBindingManager


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    async for engine, session_factory in create_engine_and_factory():
        await reset_shared_orm_engine()
        manager = CharacterBindingManager()
        await manager.initialize()
        current = scope("fixture")
        try:
            yield Harness(engine, session_factory, manager)
        finally:
            with suppress(Exception):
                await manager.close()
            await reset_shared_orm_engine()
            await delete_scope(engine, current)


class CountingProjector:
    """Deterministic projector that exposes only the public reply context."""

    def __init__(self, *, metadata: Mapping[str, str | int | bool] | None = None) -> None:
        self.calls = 0
        self.contexts: list[str] = []
        self.context_objects: list[ReplyProjectionContext] = []
        self.metadata = dict(metadata or {})

    def __call__(self, context: ReplyProjectionContext) -> ReplyProjection:
        self.calls += 1
        self.contexts.append(repr(context))
        self.context_objects.append(context)
        metadata = {"projector_call": self.calls, **self.metadata}
        return ReplyProjection(
            body="冻结安全回复",
            metadata=metadata,
        )


def service_for(
    harness: Harness,
    *,
    projector: CountingProjector | None = None,
    random_source: RandomSource | None = None,
) -> RouletteCommandService:
    reply_projector = projector or CountingProjector()
    if random_source is None:
        return RouletteCommandService(
            session_factory=harness.session_factory,
            reply_projector=reply_projector,
        )
    return RouletteCommandService(
        session_factory=harness.session_factory,
        reply_projector=reply_projector,
        random_source=random_source,
    )


class CountingRandom:
    def __init__(self) -> None:
        self.chamber_calls = 0
        self.item_calls = 0

    def chamber_order(
        self,
        live_count: int,
        blank_count: int,
    ) -> tuple[ChamberKind, ...]:
        self.chamber_calls += 1
        assert (live_count, blank_count) == (2, 4)
        return (
            ChamberKind.BLANK,
            ChamberKind.BLANK,
            ChamberKind.LIVE,
            ChamberKind.BLANK,
            ChamberKind.LIVE,
            ChamberKind.BLANK,
        )

    def weighted_item(self, weights: Mapping[ItemType, int]) -> ItemType:
        self.item_calls += 1
        del weights
        return ItemType.BEER



async def seed_players(
    manager: CharacterBindingManager,
    current: Scope,
    count: int,
) -> tuple[str, ...]:
    return tuple(
        [
            await seed_binding(manager, current, number)
            for number in range(1, count + 1)
        ]
    )


async def create_waiting(
    service: RouletteCommandService,
    current: Scope,
    *,
    message_id: str = "create-1",
    member_openid: str | None = None,
) -> CommandReceipt:
    return await service.execute_group_command(
        request(
            current,
            message_id,
            command_factory("create"),
            member_openid=member_openid,
        )
    )


async def join_player(
    service: RouletteCommandService,
    current: Scope,
    member_openid: str,
    message_id: str,
) -> CommandReceipt:
    return await service.execute_group_command(
        request(
            current,
            message_id,
            command_factory("join"),
            member_openid=member_openid,
        )
    )


async def start_game(
    service: RouletteCommandService,
    current: Scope,
    member_openid: str,
    message_id: str = "start-1",
) -> CommandReceipt:
    return await service.execute_group_command(
        request(
            current,
            message_id,
            command_factory("start"),
            member_openid=member_openid,
        )
    )


async def current_game_row(
    session_factory: async_sessionmaker[AsyncSession],
    current: Scope,
) -> Mapping[str, Any] | None:
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT game_id, lifecycle, state_revision, turn_seq, "
                    "chamber_revision, current_player_seq, waiting_expires_at, "
                    "turn_deadline_at, "
                    "ordered_chamber, pending_rewards "
                    "FROM komari_roulette_games "
                    "WHERE app_id = :app_id AND group_openid = :group_openid "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                },
            )
        ).mappings().first()
        return dict(row) if row is not None else None


async def test_concurrent_create_has_one_game_and_one_success_receipt(
    harness: Harness,
) -> None:
    current = scope("create-race")
    await seed_binding(harness.binding_manager, current, 1)
    service = service_for(harness)
    async with harness.session_factory() as blocker:
        await blocker.begin()
        blocker_pid = await backend_pid(blocker)
        await hold_group_lock(blocker, current)
        first_started = asyncio.Event()
        second_started = asyncio.Event()

        async def run_first() -> object:
            first_started.set()
            return await create_waiting(service, current, message_id="create-a")

        async def run_second() -> object:
            second_started.set()
            return await create_waiting(service, current, message_id="create-b")

        first_task = asyncio.create_task(run_first())
        second_task = asyncio.create_task(run_second())
        await first_started.wait()
        await second_started.wait()
        await wait_for_blocked(harness.session_factory, blocker_pid)
        await blocker.commit()
        results = await asyncio.gather(first_task, second_task, return_exceptions=True)

    exceptions = [result for result in results if isinstance(result, Exception)]
    receipts = [
        cast("CommandReceipt", result)
        for result in results
        if not isinstance(result, Exception)
    ]
    assert not exceptions
    assert sorted(receipt.result_code for receipt in receipts) == [
        "created",
        "game_already_exists",
    ]
    async with harness.session_factory() as session:
        assert await count_rows(session, "komari_roulette_games", current) == 1
        assert await count_rows(
            session, "komari_roulette_command_receipts", current
        ) == 2
    await delete_scope(harness.engine, current)


async def test_different_app_and_group_scopes_do_not_contend(
    harness: Harness,
) -> None:
    first = scope("isolated-a")
    second = Scope(
        app_id=f"{first.app_id}-other-app",
        group_openid=f"{first.group_openid}-other-group",
        member_openid=f"{first.member_openid}-other-member",
    )
    await seed_binding(harness.binding_manager, first, 1)
    await seed_binding(harness.binding_manager, second, 1)
    service = service_for(harness)

    receipts = await asyncio.gather(
        create_waiting(service, first, message_id="create-a"),
        create_waiting(service, second, message_id="create-b"),
    )

    assert [receipt.result_code for receipt in receipts] == [
        "created",
        "created",
    ]
    await delete_scope(harness.engine, first)
    await delete_scope(harness.engine, second)


async def test_waiting_join_race_assigns_last_seat_and_never_reuses_join_seq(
    harness: Harness,
) -> None:
    current = scope("waiting-race")
    member_openids = await seed_players(harness.binding_manager, current, 8)
    service = service_for(harness)
    created = await create_waiting(service, current)
    assert created.result_code == "created"

    async def blocked_join_race(
        members_to_join: tuple[tuple[str, str], ...],
    ) -> list[CommandReceipt]:
        async with harness.session_factory() as blocker:
            await blocker.begin()
            blocker_pid = await backend_pid(blocker)
            await hold_group_lock(blocker, current)
            started = [asyncio.Event() for _ in members_to_join]

            async def run_one(
                index: int,
                member_openid: str,
                message_id: str,
            ) -> CommandReceipt:
                started[index].set()
                return await join_player(
                    service, current, member_openid, message_id
                )

            tasks: list[asyncio.Task[CommandReceipt]] = []
            for index, (member, message) in enumerate(members_to_join):
                task = asyncio.create_task(run_one(index, member, message))
                tasks.append(task)
                await started[index].wait()
                await wait_for_blocked(harness.session_factory, blocker_pid)
            await blocker.commit()
            return list(await asyncio.gather(*tasks))

    joined = await blocked_join_race(
        tuple(
            (member_openids[number - 1], f"join-{number}")
            for number in range(2, 6)
        )
    )
    assert all(receipt.result_code == "joined" for receipt in joined)

    last_race = await blocked_join_race(
        (
            (member_openids[5], "join-6"),
            (member_openids[6], "join-7"),
        )
    )
    assert sorted(receipt.result_code for receipt in last_race) == [
        "game_full",
        "joined",
    ]

    async with harness.session_factory() as session:
        stale_target_seq = await session.scalar(
            text(
                "SELECT join_seq FROM komari_roulette_players "
                "WHERE game_id = (SELECT game_id FROM komari_roulette_games "
                "WHERE app_id = :app_id AND group_openid = :group_openid) "
                "AND member_openid = :member_openid"
            ),
            {
                "app_id": current.app_id,
                "group_openid": current.group_openid,
                "member_openid": member_openids[1],
            },
        )
    assert stale_target_seq is not None

    leave = await service.execute_group_command(
        request(
            current,
            "leave-2",
            command_factory("leave"),
            member_openid=member_openids[1],
        )
    )
    assert leave.result_code == "left"
    rejoined = await join_player(service, current, member_openids[1], "rejoin-2")
    assert rejoined.result_code == "joined"

    stale_target = await service.execute_group_command(
        request(
            current,
            "transfer-stale-target",
            command_factory("transfer", target_player_seq=int(stale_target_seq)),
            member_openid=member_openids[0],
        )
    )
    assert stale_target.result_code == "player_seq_not_found"

    async with harness.session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT join_seq, member_openid FROM komari_roulette_players "
                    "WHERE game_id = (SELECT game_id FROM komari_roulette_games "
                    "WHERE app_id = :app_id AND group_openid = :group_openid "
                    "AND lifecycle = 'waiting') ORDER BY join_seq"
                ),
                {
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                },
            )
        ).all()
    assert [row[0] for row in rows] == [1, 3, 4, 5, 6, 7]
    assert rows[-1][1] == member_openids[1]
    started = await start_game(service, current, member_openids[0], "start-after-rejoin")
    assert started.result_code == "started"
    await delete_scope(harness.engine, current)


async def test_waiting_start_cancel_race_commits_one_latest_decision(
    harness: Harness,
) -> None:
    current = scope("start-cancel-race")
    members = await seed_players(harness.binding_manager, current, 2)
    service = service_for(harness, random_source=CountingRandom())
    await create_waiting(service, current)
    await join_player(service, current, members[1], "join-2")
    async with harness.session_factory() as blocker:
        await blocker.begin()
        blocker_pid = await backend_pid(blocker)
        await hold_group_lock(blocker, current)
        start_started = asyncio.Event()
        cancel_started = asyncio.Event()

        async def run_start() -> CommandReceipt:
            start_started.set()
            return await start_game(service, current, members[0], "race-start")

        async def run_cancel() -> CommandReceipt:
            cancel_started.set()
            return await service.execute_group_command(
                request(
                    current,
                    "race-cancel",
                    command_factory("cancel"),
                    member_openid=members[0],
                )
            )

        start_task = asyncio.create_task(run_start())
        cancel_task = asyncio.create_task(run_cancel())
        await start_started.wait()
        await cancel_started.wait()
        await wait_for_blocked(harness.session_factory, blocker_pid)
        await blocker.commit()
        start_result, cancel_result = await asyncio.gather(start_task, cancel_task)

    row = await current_game_row(harness.session_factory, current)
    assert row is not None
    codes = {start_result.result_code, cancel_result.result_code}
    assert codes & {"started", "cancelled"}
    if row["lifecycle"] == "active":
        assert start_result.result_code == "started"
    else:
        assert row["lifecycle"] == "cancelled"
        assert cancel_result.result_code == "cancelled"
    assert sum(
        result.result_code in {"started", "cancelled"}
        for result in (start_result, cancel_result)
    ) == 1
    await delete_scope(harness.engine, current)


async def test_waiting_join_start_competition_is_ordered_by_group_lock(
    harness: Harness,
) -> None:
    async def race(first_operation: str) -> tuple[CommandReceipt, CommandReceipt, Scope]:
        current = scope(f"waiting-{first_operation}-race")
        members = await seed_players(harness.binding_manager, current, 3)
        service = service_for(harness, random_source=CountingRandom())
        await create_waiting(service, current, member_openid=members[0])
        assert (await join_player(service, current, members[1], "join-2")).result_code == "joined"
        async with harness.session_factory() as blocker:
            await blocker.begin()
            blocker_pid = await backend_pid(blocker)
            await hold_group_lock(blocker, current)
            join_started = asyncio.Event()
            start_started = asyncio.Event()

            async def join() -> CommandReceipt:
                join_started.set()
                return await join_player(service, current, members[2], "race-join")

            async def start() -> CommandReceipt:
                start_started.set()
                return await start_game(service, current, members[0], "race-start")

            runners = [join, start] if first_operation == "join" else [start, join]
            first_task = asyncio.create_task(runners[0]())
            first_event = join_started if first_operation == "join" else start_started
            second_event = start_started if first_operation == "join" else join_started
            await first_event.wait()
            await wait_for_blocked(harness.session_factory, blocker_pid)
            second_task = asyncio.create_task(runners[1]())
            await second_event.wait()
            await wait_for_blocked(harness.session_factory, blocker_pid)
            await blocker.commit()
            first, second = await asyncio.gather(first_task, second_task)
        if first_operation == "join":
            return first, second, current
        return second, first, current

    join_result, start_result, join_first_scope = await race("join")
    assert join_result.result_code == "joined"
    assert start_result.result_code == "started"
    join_result_second, start_result_first, start_first_scope = await race("start")
    assert start_result_first.result_code == "started"
    assert join_result_second.result_code == "game_already_started"
    await delete_scope(harness.engine, join_first_scope)
    await delete_scope(harness.engine, start_first_scope)


async def test_binding_name_is_required_for_join_and_frozen_after_seating(
    harness: Harness,
) -> None:
    current = scope("binding-freeze")
    first = await seed_binding(harness.binding_manager, current, 1, name="甲")
    second = await seed_binding(harness.binding_manager, current, 2, name="乙")
    third = await seed_binding(harness.binding_manager, current, 3, name="丙")
    fourth = await seed_binding(harness.binding_manager, current, 4, name="丁")
    service = service_for(harness, random_source=CountingRandom())
    await create_waiting(service, current, member_openid=first)
    joined = await join_player(service, current, second, "join-2")
    assert joined.result_code == "joined"

    await harness.binding_manager.clear_character_name(
        app_id=current.app_id,
        group_openid=current.group_openid,
        member_openid=first,
    )

    await harness.binding_manager.bind_group_member(
        app_id=current.app_id,
        group_id=f"qq-group-{current.group_openid}",
        group_openid=current.group_openid,
        member_qq=f"qq-{fourth}",
        member_openid=fourth,
        character_name="甲",
    )
    normalized_collision = await join_player(service, current, fourth, "join-name-collision")
    assert normalized_collision.result_code in {
        "character_name_taken",
        "name_conflict",
        "binding_name_conflict",
    }

    await harness.binding_manager.clear_character_name(
        app_id=current.app_id,
        group_openid=current.group_openid,
        member_openid=third,
    )

    missing_name = await join_player(service, current, third, "join-no-name")
    assert missing_name.result_code in {
        "binding_required",
        "character_name_required",
        "member_not_bound",
    }
    started = await start_game(service, current, first)
    assert started.result_code == "started"
    await harness.binding_manager.clear_character_name(
        app_id=current.app_id,
        group_openid=current.group_openid,
        member_openid=first,
    )
    await harness.binding_manager.bind_group_member(
        app_id=current.app_id,
        group_id=f"qq-group-{current.group_openid}",
        group_openid=current.group_openid,
        member_qq=f"qq-{second}",
        member_openid=second,
        character_name="乙改名",
    )
    async with harness.session_factory() as session:
        names = (
            await session.execute(
                text(
                    "SELECT display_name FROM komari_roulette_players "
                    "WHERE game_id = (SELECT game_id FROM komari_roulette_games "
                    "WHERE app_id = :app_id AND group_openid = :group_openid) "
                    "ORDER BY join_seq"
                ),
                {
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                },
            )
        ).scalars().all()
    assert names == ["甲", "乙"]
    await delete_scope(harness.engine, current)


async def test_active_same_observation_only_first_action_can_commit(
    harness: Harness,
) -> None:
    current = scope("observation-race")
    members = await seed_players(harness.binding_manager, current, 3)
    entropy = CountingRandom()
    service = service_for(harness, random_source=entropy)
    await create_waiting(service, current)
    await join_player(service, current, members[1], "join-2")
    await join_player(service, current, members[2], "join-3")
    started = await start_game(service, current, members[0])
    assert started.result_code == "started"
    row = await current_game_row(harness.session_factory, current)
    assert row is not None
    game_observation = observation(
        game_id=str(row["game_id"]),
        state_revision=int(row["state_revision"]),
        turn_seq=int(row["turn_seq"]),
    )
    first_shot = await service.execute_group_command(
        request(
            current,
            "first-shot",
            command_factory("shoot"),
            member_openid=members[0],
        ),
        observation=game_observation,
    )
    assert first_shot.result_code == "shot"
    after_first = await current_game_row(harness.session_factory, current)
    assert after_first is not None
    follow_up_observation = observation(
        game_id=str(after_first["game_id"]),
        state_revision=int(after_first["state_revision"]),
        turn_seq=int(after_first["turn_seq"]),
    )
    async with harness.session_factory() as blocker:
        await blocker.begin()
        blocker_pid = await backend_pid(blocker)
        await hold_group_lock(blocker, current)
        first_started = asyncio.Event()
        second_started = asyncio.Event()

        async def run_shot(message_id: str, started: asyncio.Event) -> CommandReceipt:
            started.set()
            return await service.execute_group_command(
                request(
                    current,
                    message_id,
                    command_factory("shoot"),
                    member_openid=members[0],
                ),
                observation=follow_up_observation,
            )

        first_task = asyncio.create_task(run_shot("follow-up-a", first_started))
        second_task = asyncio.create_task(run_shot("follow-up-b", second_started))
        await first_started.wait()
        await second_started.wait()
        await wait_for_blocked(harness.session_factory, blocker_pid)
        await blocker.commit()
        results = await asyncio.gather(first_task, second_task)
    codes = [result.result_code for result in results]
    assert codes.count("state_conflict") == 1
    assert codes.count("shot") == 1
    assert entropy.chamber_calls == 1
    assert entropy.item_calls == 1
    after = await current_game_row(harness.session_factory, current)
    assert after is not None
    assert int(after["state_revision"]) == int(after_first["state_revision"]) + 1
    assert int(after["chamber_revision"]) == int(after_first["chamber_revision"]) + 1
    await delete_scope(harness.engine, current)


async def test_item_choice_same_observation_consumes_once_and_renews_once(
    harness: Harness,
) -> None:
    current = scope("item-choice-race")
    members = await seed_players(harness.binding_manager, current, 3)
    entropy = CountingRandom()
    service = service_for(harness, random_source=entropy)
    await create_waiting(service, current)
    await join_player(service, current, members[1], "join-2")
    await join_player(service, current, members[2], "join-3")
    started = await start_game(service, current, members[0])
    assert started.result_code == "started"
    before = await current_game_row(harness.session_factory, current)
    assert before is not None
    async with harness.session_factory() as session:
        await session.execute(
            text(
                "UPDATE komari_roulette_games "
                "SET phase = 'item_choice', "
                "pending_rewards = ARRAY['beer', 'lock']::text[] "
                "WHERE game_id = :game_id"
            ),
            {"game_id": before["game_id"]},
        )
        await session.execute(
            text(
                "UPDATE komari_roulette_players "
                "SET magnifier_count = 1, beer_count = 1, "
                "burst_count = 1, lock_count = 1 "
                "WHERE game_id = :game_id AND join_seq = 1"
            ),
            {"game_id": before["game_id"]},
        )
        await session.commit()
    item_observation = observation(
        game_id=str(before["game_id"]),
        state_revision=int(before["state_revision"]),
        turn_seq=int(before["turn_seq"]),
    )
    async with harness.session_factory() as blocker:
        await blocker.begin()
        blocker_pid = await backend_pid(blocker)
        await hold_group_lock(blocker, current)
        first_started = asyncio.Event()
        second_started = asyncio.Event()

        async def choose(message_id: str, started_event: asyncio.Event) -> CommandReceipt:
            started_event.set()
            return await service.execute_group_command(
                request(
                    current,
                    message_id,
                    command_factory("choose_item", decision="discard"),
                    member_openid=members[0],
                ),
                observation=item_observation,
            )

        first_task = asyncio.create_task(choose("item-choice-a", first_started))
        second_task = asyncio.create_task(choose("item-choice-b", second_started))
        await first_started.wait()
        await second_started.wait()
        await wait_for_blocked(harness.session_factory, blocker_pid)
        await blocker.commit()
        results = await asyncio.gather(first_task, second_task)
    assert [result.result_code for result in results].count("item_choice_updated") == 1
    assert [result.result_code for result in results].count("state_conflict") == 1
    assert entropy.chamber_calls == 1
    assert entropy.item_calls == 0
    after = await current_game_row(harness.session_factory, current)
    assert after is not None
    assert int(after["state_revision"]) == int(before["state_revision"]) + 1
    assert after["pending_rewards"] == ["lock"]
    await delete_scope(harness.engine, current)


async def test_read_only_panel_and_leaderboard_ignore_stale_observation(
    harness: Harness,
) -> None:
    current = scope("readonly-observation")
    members = await seed_players(harness.binding_manager, current, 3)
    service = service_for(harness)
    await create_waiting(service, current)
    await join_player(service, current, members[1], "join-2")
    await join_player(service, current, members[2], "join-3")
    await start_game(service, current, members[0])
    before = await current_game_row(harness.session_factory, current)
    assert before is not None
    old_observation = observation(
        game_id=str(before["game_id"]),
        state_revision=int(before["state_revision"]),
        turn_seq=int(before["turn_seq"]),
    )
    changed = await service.execute_group_command(
        request(
            current,
            "readonly-advance",
            command_factory("forfeit"),
            member_openid=members[0],
        ),
        observation=old_observation,
    )
    assert changed.result_code != "state_conflict"
    changed_row = await current_game_row(harness.session_factory, current)
    assert changed_row is not None
    panel = await service.execute_group_command(
        request(
            current,
            "item-panel",
            command_factory("open_item_panel"),
            member_openid=members[1],
        ),
        observation=old_observation,
    )
    leaderboard = await service.execute_group_command(
        request(
            current,
            "leaderboard",
            command_factory("leaderboard"),
            member_openid=members[1],
        ),
        observation=old_observation,
    )
    assert panel.result_code != "state_conflict"
    assert leaderboard.result_code != "state_conflict"
    after = await current_game_row(harness.session_factory, current)
    assert after is not None
    assert after["state_revision"] == changed_row["state_revision"]
    assert after["turn_seq"] == changed_row["turn_seq"]
    assert after["turn_deadline_at"] == changed_row["turn_deadline_at"]
    await delete_scope(harness.engine, current)


async def test_expiry_worker_and_action_share_pg_deadline_boundary(
    harness: Harness,
) -> None:
    current = scope("expiry-race")
    members = await seed_players(harness.binding_manager, current, 3)
    service = service_for(harness)
    await create_waiting(service, current)
    await join_player(service, current, members[1], "join-2")
    await join_player(service, current, members[2], "join-3")
    await start_game(service, current, members[0])
    before = await current_game_row(harness.session_factory, current)
    assert before is not None
    async with harness.session_factory() as session:
        await session.execute(
            text(
                "UPDATE komari_roulette_games "
                "SET turn_deadline_at = clock_timestamp() + interval '1 second' "
                "WHERE game_id = :game_id"
            ),
            {"game_id": before["game_id"]},
        )
        await session.commit()
    async with harness.session_factory() as session:
        deadline = await session.scalar(
            text(
                "SELECT turn_deadline_at FROM komari_roulette_games "
                "WHERE game_id = :game_id"
            ),
            {"game_id": before["game_id"]},
        )
    assert deadline is not None
    async with harness.session_factory() as session:
        receipt_count_before = await count_rows(
            session, "komari_roulette_command_receipts", current
        )
    old_observation = observation(
        game_id=str(before["game_id"]),
        state_revision=int(before["state_revision"]),
        turn_seq=int(before["turn_seq"]),
    )
    async with harness.session_factory() as blocker:
        await blocker.begin()
        blocker_pid = await backend_pid(blocker)
        await hold_group_lock(blocker, current)
        expiry_started = asyncio.Event()
        action_started = asyncio.Event()

        async def run_expiry() -> ExpiryAdvance:
            expiry_started.set()
            return await service.advance_expired(
                current.group, observation=old_observation
            )

        async def run_action() -> CommandReceipt:
            action_started.set()
            return await service.execute_group_command(
                request(
                    current,
                    "late-shoot",
                    command_factory("shoot"),
                    member_openid=members[0],
                ),
                observation=old_observation,
            )

        expiry_task = asyncio.create_task(run_expiry())
        action_task = asyncio.create_task(run_action())
        try:
            await expiry_started.wait()
            await action_started.wait()
            await wait_for_blocked(harness.session_factory, blocker_pid)
            async with asyncio.timeout(5):
                while True:
                    async with harness.session_factory() as probe:
                        waiting_xacts = (
                            await probe.execute(
                                text(
                                    "SELECT xact_start FROM pg_stat_activity "
                                    "WHERE :blocker = ANY(pg_blocking_pids(pid)) "
                                    "AND xact_start IS NOT NULL"
                                ),
                                {"blocker": blocker_pid},
                            )
                        ).scalars().all()
                    if len(waiting_xacts) >= 2:
                        break
                    await asyncio.sleep(0.02)
            assert all(xact_start < deadline for xact_start in waiting_xacts)
            async with asyncio.timeout(5):
                while True:
                    async with harness.session_factory() as clock_probe:
                        pg_now = await clock_probe.scalar(
                            text("SELECT clock_timestamp()")
                        )
                    if pg_now >= deadline:
                        break
                    await asyncio.sleep(0.02)
            await blocker.commit()
            expiry, action = await asyncio.gather(expiry_task, action_task)
        finally:
            if blocker.in_transaction():
                await blocker.rollback()
            for task in (expiry_task, action_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(expiry_task, action_task, return_exceptions=True)
    assert expiry.receipt_id is None
    assert action.result_code in {"turn_expired", "state_conflict"}
    async with harness.session_factory() as session:
        assert await count_rows(
            session, "komari_roulette_command_receipts", current
        ) == receipt_count_before + 1
        deadline_in_full_window = await session.scalar(
            text(
                "SELECT turn_deadline_at BETWEEN "
                "clock_timestamp() + interval '14 minutes 30 seconds' AND "
                "clock_timestamp() + interval '15 minutes 30 seconds' "
                "FROM komari_roulette_games "
                "WHERE app_id = :app_id AND group_openid = :group_openid"
            ),
            {"app_id": current.app_id, "group_openid": current.group_openid},
        )
        eliminated = await session.scalar(
            text(
                "SELECT count(*) FROM komari_roulette_players "
                "WHERE game_id = (SELECT game_id FROM komari_roulette_games "
                "WHERE app_id = :app_id AND group_openid = :group_openid) "
                "AND alive = false"
            ),
            {"app_id": current.app_id, "group_openid": current.group_openid},
        )
    assert deadline_in_full_window is True
    assert eliminated == 1
    async with harness.session_factory() as session:
        assert await session.scalar(
            text(
                "SELECT current_player_seq FROM komari_roulette_games "
                "WHERE app_id = :app_id AND group_openid = :group_openid"
            ),
            {
                "app_id": current.app_id,
                "group_openid": current.group_openid,
            },
        ) == 2
    await delete_scope(harness.engine, current)


async def test_same_key_replay_uses_first_receipt_and_different_payload_raises(
    harness: Harness,
) -> None:
    current = scope("replay")
    members = await seed_players(harness.binding_manager, current, 2)
    projector = CountingProjector()
    service = service_for(harness, projector=projector)
    await create_waiting(service, current)
    first = await join_player(service, current, members[1], "join-replay")
    snapshot = {
        "fingerprint": first.fingerprint,
        "result_code": first.result_code,
        "game_id": first.game_id,
        "state_revision": first.state_revision,
        "reply": first.reply,
    }
    await join_player(service, current, members[0], "join-other")
    replay = await service.execute_group_command(
        request(
            current,
            "join-replay",
            command_factory("join"),
            member_openid=members[1],
        ),
        observation=observation(game_id="stale", state_revision=999, turn_seq=999),
    )
    assert {
        "fingerprint": replay.fingerprint,
        "result_code": replay.result_code,
        "game_id": replay.game_id,
        "state_revision": replay.state_revision,
        "reply": replay.reply,
    } == snapshot
    assert projector.calls == 3

    with pytest.raises(IdempotencyKeyConflictError):
        await service.execute_group_command(
            request(
                current,
                "join-replay",
                command_factory("leave"),
                member_openid=members[1],
            )
        )
    with pytest.raises(IdempotencyKeyConflictError):
        await service.execute_group_command(
            request(
                current,
                "join-replay",
                command_factory("join"),
                member_openid=f"{members[1]}-different-caller",
            )
        )
    async with harness.session_factory() as session:
        assert await count_rows(session, "komari_roulette_command_receipts", current) == 3
    await delete_scope(harness.engine, current)


async def test_same_key_first_receipt_competition_has_one_effect_and_projection(
    harness: Harness,
) -> None:
    current = scope("same-key-first-race")
    await seed_binding(harness.binding_manager, current, 1)
    projector = CountingProjector()
    service = service_for(harness, projector=projector)
    command_request = request(
        current,
        "same-key-first",
        command_factory("create"),
    )
    async with harness.session_factory() as blocker:
        await blocker.begin()
        blocker_pid = await backend_pid(blocker)
        await hold_group_lock(blocker, current)
        started = [asyncio.Event(), asyncio.Event()]

        async def run(index: int) -> CommandReceipt:
            started[index].set()
            return await service.execute_group_command(command_request)

        tasks = [asyncio.create_task(run(index)) for index in range(2)]
        for event in started:
            await event.wait()
        await wait_for_blocked(harness.session_factory, blocker_pid)
        await blocker.commit()
        first, second = await asyncio.gather(*tasks)
    assert first.receipt_id == second.receipt_id
    assert first.fingerprint == second.fingerprint
    assert first.result_code == second.result_code == "created"
    assert projector.calls == 1
    async with harness.session_factory() as session:
        assert await count_rows(session, "komari_roulette_games", current) == 1
        assert await count_rows(
            session, "komari_roulette_command_receipts", current
        ) == 1
    await delete_scope(harness.engine, current)


async def test_fingerprint_is_safe_and_excludes_observation_and_runtime_values(
    harness: Harness,
) -> None:
    current = scope("fingerprint")
    await seed_binding(harness.binding_manager, current, 1)
    service = service_for(harness)
    receipt = await service.execute_group_command(
        request(current, "safe-fingerprint", command_factory("create")),
        observation=observation(game_id="observed-later", state_revision=7, turn_seq=2),
    )
    fingerprint = receipt.fingerprint
    encoded = json.dumps(fingerprint, ensure_ascii=False, default=str)
    assert "observed-later" not in encoded
    assert "ordered_chamber" not in encoded
    assert "pending_rewards" not in encoded
    assert "random" not in encoded.lower()
    assert "runtime_config" not in encoded
    await delete_scope(harness.engine, current)


async def test_syntax_failure_can_receipt_without_expiring_waiting_game(
    harness: Harness,
) -> None:
    current = scope("syntax")
    await seed_binding(harness.binding_manager, current, 1)
    service = service_for(harness)
    await create_waiting(service, current)
    before = await current_game_row(harness.session_factory, current)
    assert before is not None
    async with harness.session_factory() as session:
        await session.execute(
            text(
                "UPDATE komari_roulette_games "
                "SET waiting_expires_at = clock_timestamp() - interval '1 second' "
                "WHERE game_id = :game_id"
            ),
            {"game_id": before["game_id"]},
        )
        await session.commit()
    failure = await service.execute_group_command(
        request(
            current,
            "syntax-error",
            command_factory("syntax_failure", code="invalid_syntax"),
        )
    )
    assert failure.result_code == "invalid_syntax"
    after = await current_game_row(harness.session_factory, current)
    assert after is not None
    assert after["lifecycle"] == "waiting"
    assert after["state_revision"] == before["state_revision"]
    assert not hasattr(service, "execute_raw_text")
    await delete_scope(harness.engine, current)


async def test_commit_failure_rolls_back_terminal_result_wins_and_receipt(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = scope("rollback")
    members = await seed_players(harness.binding_manager, current, 2)
    projector = CountingProjector()
    service = service_for(harness, projector=projector)
    await create_waiting(service, current)
    await join_player(service, current, members[1], "join-2")
    await start_game(service, current, members[0])
    before = await current_game_row(harness.session_factory, current)
    assert before is not None
    action_observation = observation(
        game_id=str(before["game_id"]),
        state_revision=int(before["state_revision"]),
        turn_seq=int(before["turn_seq"]),
    )
    original_commit = AsyncSession.commit

    async def fail_before_commit(session: AsyncSession) -> None:
        del session
        raise ConnectionError from None

    monkeypatch.setattr(AsyncSession, "commit", fail_before_commit)
    with pytest.raises((StorageUnavailableError, CommitOutcomeUnknownError, ConnectionError)):
        await service.execute_group_command(
            request(
                current,
                "forfeit-failure",
                command_factory("forfeit"),
                member_openid=members[0],
            ),
            observation=action_observation,
        )
    monkeypatch.setattr(AsyncSession, "commit", original_commit)
    retried = await service.execute_group_command(
        request(
            current,
            "forfeit-failure",
            command_factory("forfeit"),
            member_openid=members[0],
        ),
        observation=action_observation,
    )
    assert retried.result_code in {"forfeited", "completed"}
    assert getattr(projector.context_objects[-1], "winner_group_wins", None) == 1
    row = await current_game_row(harness.session_factory, current)
    assert row is not None
    assert row["lifecycle"] == "completed"
    async with harness.session_factory() as session:
        assert await count_rows(session, "komari_roulette_results", current) == 1
        assert await count_rows(session, "komari_roulette_leaderboard", current) == 1
        assert await count_rows(session, "komari_roulette_command_receipts", current) == 4
    await delete_scope(harness.engine, current)


@pytest.mark.parametrize(
    "failure_table",
    ["komari_roulette_command_receipts", "komari_roulette_fulfillments"],
)
async def test_receipt_or_fulfillment_insert_failure_rolls_back_terminal_and_wins(
    harness: Harness,
    failure_table: str,
) -> None:
    current = scope(f"trigger-{failure_table.rsplit('_', 1)[-1]}")
    members = await seed_players(harness.binding_manager, current, 2)
    service = service_for(harness)
    await create_waiting(service, current)
    await join_player(service, current, members[1], "join-2")
    await start_game(service, current, members[0])
    before = await current_game_row(harness.session_factory, current)
    assert before is not None
    action_observation = observation(
        game_id=str(before["game_id"]),
        state_revision=int(before["state_revision"]),
        turn_seq=int(before["turn_seq"]),
    )
    suffix = uuid4().hex
    function_name = f"tsk276_fail_{suffix}"
    trigger_name = f"tsk276_trigger_{suffix}"
    async with harness.engine.begin() as connection:
        await connection.execute(
            text(
                f"CREATE FUNCTION {function_name}() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN "
                "RAISE EXCEPTION 'TSK-276 injected insert failure'; "
                "END; $$"
            )
        )
        await connection.execute(
            text(
                f"CREATE TRIGGER {trigger_name} BEFORE INSERT ON {failure_table} "
                f"FOR EACH ROW EXECUTE FUNCTION {function_name}()"
            )
        )
    try:
        with pytest.raises((StorageUnavailableError, CommitOutcomeUnknownError)):
            await service.execute_group_command(
                request(
                    current,
                    f"trigger-{suffix}",
                    command_factory("forfeit"),
                    member_openid=members[0],
                ),
                observation=action_observation,
            )
    finally:
        async with harness.engine.begin() as connection:
            await connection.execute(
                text(f"DROP TRIGGER {trigger_name} ON {failure_table}")
            )
            await connection.execute(text(f"DROP FUNCTION {function_name}()"))
    retried = await service.execute_group_command(
        request(
            current,
            f"trigger-{suffix}",
            command_factory("forfeit"),
            member_openid=members[0],
        ),
        observation=action_observation,
    )
    assert retried.result_code in {"forfeited", "completed"}
    row = await current_game_row(harness.session_factory, current)
    assert row is not None
    assert row["lifecycle"] == "completed"
    async with harness.session_factory() as session:
        assert await count_rows(session, "komari_roulette_results", current) == 1
        assert await count_rows(session, "komari_roulette_leaderboard", current) == 1
        assert await count_rows(session, "komari_roulette_command_receipts", current) == 4
    await delete_scope(harness.engine, current)


@pytest.mark.parametrize("commit_after_pg_write", [False, True])
async def test_commit_outcome_unknown_retries_only_with_original_key(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    *,
    commit_after_pg_write: bool,
) -> None:
    current = scope(f"commit-unknown-{commit_after_pg_write}")
    await seed_binding(harness.binding_manager, current, 1)
    projector = CountingProjector()
    service = service_for(harness, projector=projector)
    original_commit = AsyncSession.commit
    calls = 0

    async def injected_commit(session: AsyncSession) -> None:
        nonlocal calls
        calls += 1
        if not commit_after_pg_write:
            raise ConnectionError from None
        await original_commit(session)
        raise ConnectionError from None

    monkeypatch.setattr(AsyncSession, "commit", injected_commit)
    with pytest.raises(CommitOutcomeUnknownError):
        await service.execute_group_command(
            request(current, "commit-unknown", command_factory("create"))
        )
    monkeypatch.setattr(AsyncSession, "commit", original_commit)
    retried = await service.execute_group_command(
        request(current, "commit-unknown", command_factory("create"))
    )
    assert calls == 1
    assert retried.result_code == "created"
    assert projector.calls == (1 if commit_after_pg_write else 2)
    async with harness.session_factory() as session:
        assert await count_rows(session, "komari_roulette_games", current) == 1
        assert await count_rows(session, "komari_roulette_command_receipts", current) == 1
    await delete_scope(harness.engine, current)


async def test_projected_reply_is_frozen_and_contains_no_private_runtime_state(
    harness: Harness,
) -> None:
    current = scope("projection")
    members = await seed_players(harness.binding_manager, current, 2)
    projector = CountingProjector()
    service = service_for(harness, projector=projector)
    await create_waiting(service, current)
    first = await join_player(service, current, members[1], "projection-replay")
    assert all(
        secret not in context
        for context in projector.contexts
        for secret in ("ordered_chamber", "pending_rewards", "random_seed")
    )
    await join_player(service, current, members[0], "projection-other")
    replay = await service.execute_group_command(
        request(
            current,
            "projection-replay",
            command_factory("join"),
            member_openid=members[1],
        )
    )
    assert replay.reply == first.reply
    assert projector.calls == 3
    await delete_scope(harness.engine, current)


async def test_active_projection_reveals_one_item_without_secret_queue(
    harness: Harness,
) -> None:
    current = scope("projection-active")
    members = await seed_players(harness.binding_manager, current, 3)
    projector = CountingProjector(metadata={"target_member_openid": members[0]})
    service = service_for(harness, projector=projector)
    await create_waiting(service, current)
    await join_player(service, current, members[1], "join-2")
    await join_player(service, current, members[2], "join-3")
    assert (await start_game(service, current, members[0])).result_code == "started"
    before = await current_game_row(harness.session_factory, current)
    assert before is not None
    async with harness.session_factory() as session:
        await session.execute(
            text(
                "UPDATE komari_roulette_games SET "
                "ordered_chamber = ARRAY['live', 'blank', 'live', 'blank']::text[], "
                "phase = 'item_choice', "
                "pending_rewards = ARRAY['beer', 'lock']::text[] "
                "WHERE game_id = :game_id"
            ),
            {"game_id": before["game_id"]},
        )
        await session.execute(
            text(
                "UPDATE komari_roulette_players "
                "SET magnifier_count = 1, beer_count = 1, "
                "burst_count = 1, lock_count = 1 "
                "WHERE game_id = :game_id AND join_seq = 1"
            ),
            {"game_id": before["game_id"]},
        )
        await session.commit()
    result = await service.execute_group_command(
        request(
            current,
            "projection-active-choice",
            command_factory("choose_item", decision="discard"),
            member_openid=members[0],
        ),
        observation=observation(
            game_id=str(before["game_id"]),
            state_revision=int(before["state_revision"]),
            turn_seq=int(before["turn_seq"]),
        ),
    )
    assert result.result_code == "item_choice_updated"
    public_context = projector.context_objects[-1]
    context_repr = repr(public_context)
    assert "live', 'blank', 'live', 'blank" not in context_repr
    assert "beer', 'lock" not in context_repr
    assert "lock" in context_repr
    assert members[0] in repr(result.reply)
    await delete_scope(harness.engine, current)


async def test_storage_failure_does_not_turn_into_no_game_or_failed_receipt(
    harness: Harness,
) -> None:
    current = scope("storage-failure")
    await seed_binding(harness.binding_manager, current, 1)
    service = RouletteCommandService(
        session_factory=_failing_session_factory,
        reply_projector=CountingProjector(),
    )
    with pytest.raises(StorageUnavailableError):
        await service.execute_group_command(
            request(current, "storage-down", command_factory("create"))
        )
    async with harness.session_factory() as session:
        assert await count_rows(session, "komari_roulette_games", current) == 0
        assert await count_rows(session, "komari_roulette_command_receipts", current) == 0
    await delete_scope(harness.engine, current)


async def test_actual_pg_disconnect_fails_closed_without_no_game_or_fake_receipt(
    harness: Harness,
) -> None:
    current = scope("pg-disconnect")
    await seed_binding(harness.binding_manager, current, 1)

    @asynccontextmanager
    async def disconnecting_session_factory() -> AsyncIterator[AsyncSession]:
        async with harness.session_factory() as session:
            service_pid = await backend_pid(session)
            original_execute = session.execute
            terminated = False

            async def execute_and_disconnect(*args: Any, **kwargs: Any) -> Any:
                nonlocal terminated
                result = await original_execute(*args, **kwargs)
                if not terminated:
                    terminated = True
                    async with harness.engine.connect() as killer:
                        await killer.execute(
                            text("SELECT pg_terminate_backend(:pid)"),
                            {"pid": service_pid},
                        )
                return result

            session.execute = execute_and_disconnect  # type: ignore[method-assign]
            yield session

    service = RouletteCommandService(
        session_factory=disconnecting_session_factory,
        reply_projector=CountingProjector(),
    )
    with pytest.raises((StorageUnavailableError, ConnectionError)):
        await service.execute_group_command(
            request(current, "actual-pg-disconnect", command_factory("create"))
        )
    async with harness.session_factory() as session:
        assert await count_rows(session, "komari_roulette_games", current) == 0
        assert await count_rows(
            session, "komari_roulette_command_receipts", current
        ) == 0
    await delete_scope(harness.engine, current)


def test_user_canonical_command_cannot_request_internal_expiry() -> None:
    for name in ("expire", "help", "plain_text"):
        with pytest.raises(AttributeError):
            command_factory(name)


async def test_confirmed_corrupt_aggregate_uses_controlled_failed_projection(
    harness: Harness,
) -> None:
    current = scope("corrupt-aggregate")
    await seed_players(harness.binding_manager, current, 2)
    service = service_for(harness)
    await create_waiting(service, current)
    async with harness.session_factory() as session:
        await session.execute(
            text(
                "UPDATE komari_roulette_games SET next_join_seq = 1 "
                "WHERE app_id = :app_id AND group_openid = :group_openid"
            ),
            {
                "app_id": current.app_id,
                "group_openid": current.group_openid,
            },
        )
        await session.commit()
    member_two = f"{current.member_openid}-2"
    receipt = await join_player(service, current, member_two, "corrupt-join")
    assert receipt.result_code in {"aggregate_corrupt", "failed"}
    row = await current_game_row(harness.session_factory, current)
    assert row is not None
    assert row["lifecycle"] == "failed"
    async with harness.session_factory() as session:
        assert await count_rows(session, "komari_roulette_results", current) == 1
        assert await count_rows(session, "komari_roulette_leaderboard", current) == 0
    await delete_scope(harness.engine, current)


@asynccontextmanager
async def _failing_session_factory() -> AsyncIterator[AsyncSession]:
    raise ConnectionError
    yield  # pragma: no cover
