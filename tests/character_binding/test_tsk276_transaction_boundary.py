"""TSK-276 caller-owned character-binding facade and lock-boundary tests."""

from __future__ import annotations

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
    create_engine_and_factory,
    scope,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.asyncio, PG_REQUIRED]


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
        group_id=f"qq-{current.group_openid}",
        group_openid=current.group_openid,
        member_qq="qq-member-1",
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
