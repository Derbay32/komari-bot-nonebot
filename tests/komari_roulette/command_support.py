"""Real-PG support and typed request factories for TSK-276 tests."""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from komari_bot.db.group_transaction_locks import lock_group_scope
from komari_bot.plugins.komari_roulette import (
    CanonicalCommand,
    CommandRequest,
    Observation,
)
from komari_bot.plugins.komari_roulette.domain import GroupRef

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

    from komari_bot.plugins.character_binding.manager import CharacterBindingManager


POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")
SQLALCHEMY_URL = os.getenv("SQLALCHEMY_DATABASE_URL", "")
PG_REQUIRED = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="未设置 KOMARI_TEST_POSTGRES_URL，不能执行 TSK-276 真实 PG 测试",
)


async def reset_shared_orm_engine() -> None:
    """Dispose nonebot-plugin-orm engines before crossing pytest event loops."""

    from nonebot import require

    require("nonebot_plugin_orm")
    import nonebot_plugin_orm as orm_module

    engines = getattr(orm_module, "_engines", None)
    if not engines:
        return
    for engine in list(engines.values()):
        with suppress(Exception):
            await engine.dispose()


@dataclass(frozen=True, slots=True)
class Scope:
    """A unique app/group/member scope for one test."""

    app_id: str
    group_openid: str
    member_openid: str

    @property
    def group(self) -> GroupRef:
        return GroupRef(app_id=self.app_id, group_openid=self.group_openid)


def scope(tag: str = "command") -> Scope:
    suffix = f"{tag}-{uuid4().hex}"
    return Scope(
        app_id=f"tsk276-app-{suffix}",
        group_openid=f"tsk276-group-{suffix}",
        member_openid=f"tsk276-member-{suffix}",
    )


def member_id(current: Scope, number: int) -> str:
    if number == 1:
        return current.member_openid
    return f"{current.member_openid}-{number}"


async def seed_binding(
    manager: CharacterBindingManager,
    current: Scope,
    number: int,
    *,
    name: str | None = None,
) -> str:
    """Seed only test setup through the existing 271 manager seam."""

    member_openid = member_id(current, number)
    bind = manager.bind_group_member
    await bind(
        app_id=current.app_id,
        group_id=f"qq-group-{current.group_openid}",
        group_openid=current.group_openid,
        member_qq=f"qq-{member_openid}",
        member_openid=member_openid,
        character_name=name or f"Seat {number}",
        bot_self_id="tsk276-test-bot",
    )
    return member_openid


def request(
    current: Scope,
    message_id: str,
    command: CanonicalCommand,
    *,
    member_openid: str | None = None,
    mention_count: int = 1,
) -> CommandRequest:
    """Build a request only from parsed, protocol-scoped values."""

    return CommandRequest(
        app_id=current.app_id,
        group_openid=current.group_openid,
        inbound_msg_id=message_id,
        member_openid=member_openid or current.member_openid,
        command=command,
        target_mention_count=mention_count,
    )


def observation(
    *,
    game_id: str,
    state_revision: int,
    turn_seq: int,
) -> Observation:
    return Observation(
        game_id=game_id,
        state_revision=state_revision,
        turn_seq=turn_seq,
    )


async def create_engine_and_factory() -> AsyncIterator[
    tuple[AsyncEngine, async_sessionmaker[AsyncSession]]
]:
    """Yield a real PostgreSQL engine and a caller-owned session factory."""

    if not POSTGRES_URL or not SQLALCHEMY_URL:
        pytest.skip("TSK-276 需要真实 PG 与 SQLAlchemy DSN")
    engine = create_async_engine(
        SQLALCHEMY_URL,
        pool_pre_ping=True,
        pool_size=4,
        max_overflow=4,
    )
    try:
        yield engine, async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def delete_scope(engine: AsyncEngine, current: Scope) -> None:
    """Best-effort cleanup after a test; missing 0020 tables remain a RED signal."""

    params = {
        "app_id": current.app_id,
        "group_openid": current.group_openid,
    }
    for statement in (
        "DELETE FROM komari_roulette_fulfillments "
        "WHERE receipt_id IN (SELECT receipt_id "
        "FROM komari_roulette_command_receipts "
        "WHERE app_id = :app_id AND group_openid = :group_openid)",
        "DELETE FROM komari_roulette_command_receipts "
        "WHERE app_id = :app_id AND group_openid = :group_openid",
        "DELETE FROM komari_roulette_result_players "
        "WHERE game_id IN (SELECT game_id FROM komari_roulette_games "
        "WHERE app_id = :app_id AND group_openid = :group_openid)",
        "DELETE FROM komari_roulette_results "
        "WHERE app_id = :app_id AND group_openid = :group_openid",
        "DELETE FROM komari_roulette_players "
        "WHERE game_id IN (SELECT game_id FROM komari_roulette_games "
        "WHERE app_id = :app_id AND group_openid = :group_openid)",
        "DELETE FROM komari_roulette_games "
        "WHERE app_id = :app_id AND group_openid = :group_openid",
        "DELETE FROM komari_roulette_leaderboard "
        "WHERE app_id = :app_id AND group_openid = :group_openid",
    ):
        async with engine.begin() as connection:
            with suppress(Exception):
                await connection.execute(text(statement), params)


async def count_rows(
    session: AsyncSession,
    table: str,
    current: Scope,
) -> int:
    """Count rows in one of the fixed TSK-276/275 tables."""

    allowed = {
        "komari_roulette_command_receipts",
        "komari_roulette_fulfillments",
        "komari_roulette_games",
        "komari_roulette_results",
        "komari_roulette_leaderboard",
    }
    if table not in allowed:
        raise ValueError(table)
    if table == "komari_roulette_fulfillments":
        statement = (
            "SELECT count(*) FROM komari_roulette_fulfillments AS f "
            "JOIN komari_roulette_command_receipts AS r "
            "ON r.receipt_id = f.receipt_id "
            "WHERE r.app_id = :app_id AND r.group_openid = :group_openid"
        )
    else:
        statement = (
            f"SELECT count(*) FROM {table} "
            "WHERE app_id = :app_id AND group_openid = :group_openid"
        )
    return int(
        await session.scalar(
            text(statement),
            {
                "app_id": current.app_id,
                "group_openid": current.group_openid,
            },
        )
        or 0
    )


async def backend_pid(session: AsyncSession) -> int:
    return int(await session.scalar(text("SELECT pg_backend_pid()")))


async def wait_for_blocked(
    session_factory: async_sessionmaker[AsyncSession],
    blocker_pid: int,
) -> None:
    """Wait for a real PostgreSQL lock waiter, with a bounded assertion."""

    async with asyncio.timeout(5):
        while True:
            async with session_factory() as session:
                blocked = await session.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE :blocker = ANY(pg_blocking_pids(pid))"
                    ),
                    {"blocker": blocker_pid},
                )
            if int(blocked or 0) > 0:
                return
            await asyncio.sleep(0.02)


async def hold_group_lock(
    session: AsyncSession,
    current: Scope,
) -> None:
    """Acquire the same shared lock used by roulette and binding services."""

    await lock_group_scope(
        session,
        app_id=current.app_id,
        group_openid=current.group_openid,
    )


def command_factory(name: str, **params: object) -> CanonicalCommand:
    """Use the typed command factory while keeping raw QQ text out of tests."""

    factory: Callable[..., CanonicalCommand] = getattr(CanonicalCommand, name)
    return factory(**params)
