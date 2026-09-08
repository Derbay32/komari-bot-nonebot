"""TSK-276 caller-owned character-binding facade and lock-boundary tests."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from komari_bot.plugins.character_binding import BindingTransaction
from komari_bot.plugins.character_binding.manager import (
    BindingConflictError,
    BindingPersistenceError,
    CharacterBindingManager,
)
from tests.komari_roulette.command_support import (
    PG_REQUIRED,
    backend_pid,
    create_engine_and_factory,
    delete_scope,
    hold_group_lock,
    request,
    scope,
    seed_binding,
    wait_for_blocked,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.asyncio, PG_REQUIRED]


@contextmanager
def capture_binding_logs() -> Iterator[list[str]]:
    from nonebot import logger

    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), level="TRACE")
    try:
        yield messages
    finally:
        logger.remove(sink_id)


@pytest.fixture
async def database() -> AsyncIterator[
    tuple[AsyncEngine, async_sessionmaker[AsyncSession]]
]:
    async for engine, factory in create_engine_and_factory():
        yield engine, factory


async def clear_binding_scope(
    engine: AsyncEngine,
    *,
    app_id: str,
    group_openid: str,
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "DELETE FROM komari_character_binding_members "
                "WHERE app_id = :app_id AND group_openid = :group_openid"
            ),
            {"app_id": app_id, "group_openid": group_openid},
        )
        await connection.execute(
            text(
                "DELETE FROM komari_character_binding_groups "
                "WHERE app_id = :app_id AND group_openid = :group_openid"
            ),
            {"app_id": app_id, "group_openid": group_openid},
        )


async def test_binding_transaction_is_caller_owned_and_group_resolver_is_independent(
    database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    engine, factory = database
    current = scope("binding-facade")
    async with factory() as session:
        transaction = BindingTransaction(session)
        await transaction.bind(
            app_id=current.app_id,
            group_id=f"qq-{current.group_openid}",
            group_openid=current.group_openid,
            member_qq="qq-member-1",
            member_openid=current.member_openid,
            character_name="雪见",
        )
        group = await transaction.resolve_group(
            app_id=current.app_id,
            group_openid=current.group_openid,
        )
        member = await transaction.resolve_member(
            app_id=current.app_id,
            group_openid=current.group_openid,
            member_openid=current.member_openid,
        )
        assert group is not None
        assert member is not None
        assert member.character_name == "雪见"
        # A facade write is visible only after the caller's one commit boundary.
        await session.rollback()
    async with factory() as session:
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM komari_character_binding_members "
                    "WHERE app_id = :app_id AND group_openid = :group_openid"
                ),
                {"app_id": current.app_id, "group_openid": current.group_openid},
            )
            == 0
        )
        await session.execute(
            text(
                "INSERT INTO komari_character_binding_groups "
                "(app_id, group_openid, group_id) "
                "VALUES (:app_id, :group_openid, :group_id)"
            ),
            {
                "app_id": current.app_id,
                "group_openid": current.group_openid,
                "group_id": f"qq-{current.group_openid}",
            },
        )
        await session.commit()
        transaction = BindingTransaction(session)
        group = await transaction.resolve_group(
            app_id=current.app_id,
            group_openid=current.group_openid,
        )
        assert group is not None
        assert (
            await transaction.resolve_member(
                app_id=current.app_id,
                group_openid=current.group_openid,
                member_openid=current.member_openid,
            )
            is None
        )
    await clear_binding_scope(
        engine,
        app_id=current.app_id,
        group_openid=current.group_openid,
    )


async def test_binding_transaction_rename_and_clear_share_the_same_uow(
    database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    engine, factory = database
    current = scope("binding-write")
    async with factory() as session:
        transaction = BindingTransaction(session)
        await transaction.bind(
            app_id=current.app_id,
            group_id=f"qq-{current.group_openid}",
            group_openid=current.group_openid,
            member_qq="qq-member-1",
            member_openid=current.member_openid,
            character_name="原名",
        )
        await session.commit()

    async with factory() as session:
        transaction = BindingTransaction(session)
        await transaction.rename(
            app_id=current.app_id,
            group_openid=current.group_openid,
            member_openid=current.member_openid,
            character_name="新名",
        )
        await transaction.clear(
            app_id=current.app_id,
            group_openid=current.group_openid,
            member_openid=current.member_openid,
        )
        await session.commit()
        member = await transaction.resolve_member(
            app_id=current.app_id,
            group_openid=current.group_openid,
            member_openid=current.member_openid,
        )
        assert member is not None
        assert member.character_name is None
    await clear_binding_scope(
        engine,
        app_id=current.app_id,
        group_openid=current.group_openid,
    )


async def test_binding_transaction_rejects_normalized_name_collision(
    database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    engine, factory = database
    current = scope("binding-name")
    async with factory() as session:
        transaction = BindingTransaction(session)
        await transaction.bind(
            app_id=current.app_id,
            group_id=f"qq-{current.group_openid}",
            group_openid=current.group_openid,
            member_qq="qq-member-1",
            member_openid=current.member_openid,
            character_name="Ａ",  # noqa: RUF001 - NFKC collision fixture
        )
        await transaction.bind(
            app_id=current.app_id,
            group_id=f"qq-{current.group_openid}",
            group_openid=current.group_openid,
            member_qq="qq-member-2",
            member_openid=f"{current.member_openid}-2",
            character_name="乙",
        )
        await session.commit()
    async with factory() as session:
        transaction = BindingTransaction(session)
        with pytest.raises(BindingConflictError):
            await transaction.rename(
                app_id=current.app_id,
                group_openid=current.group_openid,
                member_openid=f"{current.member_openid}-2",
                character_name="A",
            )
    await clear_binding_scope(
        engine,
        app_id=current.app_id,
        group_openid=current.group_openid,
    )


async def test_unmapped_group_is_none_but_storage_failure_is_explicit(
    database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    engine, factory = database
    current = scope("binding-error")
    async with factory() as session:
        transaction = BindingTransaction(session)
        assert (
            await transaction.resolve_group(
                app_id=current.app_id,
                group_openid=current.group_openid,
            )
            is None
        )
    async with factory() as session:
        transaction = BindingTransaction(session)
        pid = int(await session.scalar(text("SELECT pg_backend_pid()")))
        async with engine.connect() as killer:
            await killer.execute(
                text("SELECT pg_terminate_backend(:pid)"), {"pid": pid}
            )
        with pytest.raises(BindingPersistenceError):
            await transaction.resolve_group(
                app_id=current.app_id,
                group_openid=current.group_openid,
            )


def test_character_binding_has_no_roulette_reverse_dependency() -> None:
    package_root = Path(__file__).resolve().parents[2]
    binding_root = package_root / "komari_bot" / "plugins" / "character_binding"
    source = "\n".join(path.read_text() for path in binding_root.rglob("*.py"))
    assert "komari_roulette" not in source


async def test_manager_cache_cannot_authorize_a_cleared_member(
    database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    binding_manager: CharacterBindingManager,
) -> None:
    engine, factory = database
    current = scope("binding-cache")
    manager = binding_manager
    await manager.bind_group_member(
        app_id=current.app_id,
        group_id=f"qq-group-{current.group_openid}",
        group_openid=current.group_openid,
        member_qq=f"qq-{current.member_openid}",
        member_openid=current.member_openid,
        character_name="可见名",
    )
    assert (
        manager.get_qq_character_name(
            app_id=current.app_id,
            group_openid=current.group_openid,
            member_openid=current.member_openid,
        )
        == "可见名"
    )

    async with factory() as session:
        transaction = BindingTransaction(session)
        await transaction.clear(
            app_id=current.app_id,
            group_openid=current.group_openid,
            member_openid=current.member_openid,
        )
        await session.commit()
    from komari_bot.plugins.komari_roulette import (
        ReplyProjection,
        RouletteCommandService,
    )
    from tests.komari_roulette.command_support import command_factory, request

    service = RouletteCommandService(
        session_factory=factory,
        reply_projector=lambda _context: ReplyProjection(
            body="safe", metadata={"test": True}
        ),
    )
    receipt = await service.execute_group_command(
        request(current, "cache-must-not-authorize", command_factory("create"))
    )
    assert receipt.result_code != "created"
    await clear_binding_scope(
        engine,
        app_id=current.app_id,
        group_openid=current.group_openid,
    )


@pytest.mark.parametrize("operation", ["clear", "rename"])
@pytest.mark.parametrize("operation_first", [True, False])
async def test_binding_write_and_join_share_scope_lock_for_both_commit_orders(
    database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    binding_manager: CharacterBindingManager,
    operation: str,
    operation_first: bool,  # noqa: FBT001
) -> None:
    engine, factory = database
    current = scope(f"binding-join-{operation}-{operation_first}")
    member = await seed_binding(binding_manager, current, 1)
    joining = await seed_binding(binding_manager, current, 2, name="丙")
    from komari_bot.plugins.komari_roulette import (
        ReplyProjection,
        RouletteCommandService,
    )
    from tests.komari_roulette.command_support import command_factory

    service = RouletteCommandService(
        session_factory=factory,
        reply_projector=lambda _context: ReplyProjection(
            body="safe", metadata={"test": True}
        ),
    )
    await service.execute_group_command(
        request(current, "binding-race-create", command_factory("create"), member_openid=member)
    )
    async with factory() as blocker:
        await blocker.begin()
        blocker_pid = await backend_pid(blocker)
        await hold_group_lock(blocker, current)
        clear_started = asyncio.Event()
        join_started = asyncio.Event()

        async def mutate_binding() -> None:
            clear_started.set()
            if operation == "clear":
                await binding_manager.clear_character_name(
                    app_id=current.app_id,
                    group_openid=current.group_openid,
                    member_openid=joining,
                )
            else:
                await binding_manager.bind_group_member(
                    app_id=current.app_id,
                    group_id=f"qq-group-{current.group_openid}",
                    group_openid=current.group_openid,
                    member_qq=f"qq-{joining}",
                    member_openid=joining,
                    character_name="丁",
                )

        async def join_binding() -> object:
            join_started.set()
            return await service.execute_group_command(
                request(
                    current,
                    f"binding-race-join-{operation_first}",
                    command_factory("join"),
                    member_openid=joining,
                )
            )

        first, second = (
            (mutate_binding, join_binding)
            if operation_first
            else (join_binding, mutate_binding)
        )
        first_task = asyncio.create_task(first())
        first_event = clear_started if operation_first else join_started
        second_event = join_started if operation_first else clear_started
        await first_event.wait()
        await wait_for_blocked(factory, blocker_pid)
        second_task = asyncio.create_task(second())
        await second_event.wait()
        await wait_for_blocked(factory, blocker_pid)
        await blocker.commit()
        results = await asyncio.gather(first_task, second_task)

    join_result = results[1 if operation_first else 0]

    result_code = getattr(join_result, "result_code", None)
    if operation == "clear" and operation_first:
        assert result_code in {
            "binding_required",
            "character_name_required",
            "member_not_bound",
        }
    else:
        assert result_code == "joined"
        async with factory() as session:
            frozen_name = await session.scalar(
                text(
                    "SELECT display_name FROM komari_roulette_players "
                    "WHERE game_id = (SELECT game_id FROM komari_roulette_games "
                    "WHERE app_id = :app_id AND group_openid = :group_openid) "
                    "AND member_openid = :member_openid"
                ),
                {
                    "app_id": current.app_id,
                    "group_openid": current.group_openid,
                    "member_openid": joining,
                },
            )
        assert frozen_name == ("丁" if operation == "rename" and operation_first else "丙")
    await delete_scope(engine, current)
    await clear_binding_scope(
        engine,
        app_id=current.app_id,
        group_openid=current.group_openid,
    )


async def test_binding_manager_runtime_logs_redact_scope_identities(
    database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    binding_manager: CharacterBindingManager,
) -> None:
    engine, _factory = database
    current = scope("binding-log-redaction")
    with capture_binding_logs() as messages:
        await binding_manager.bind_group_member(
            app_id=current.app_id,
            group_id=f"qq-group-{current.group_openid}",
            group_openid=current.group_openid,
            member_qq=f"qq-{current.member_openid}",
            member_openid=current.member_openid,
            character_name="日志名",
        )
    rendered = "".join(messages)
    assert current.app_id not in rendered
    assert current.group_openid not in rendered
    assert "name_length" in rendered
    await clear_binding_scope(
        engine,
        app_id=current.app_id,
        group_openid=current.group_openid,
    )
