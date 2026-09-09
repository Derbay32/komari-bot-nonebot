"""TSK-280 独立测试辅助：作用域、REST 桩服务与真实 PG 门控。

本模块只依赖既有共享件（管理审计工具、SQLAlchemy、NoneBot ORM），不导入
尚未实现的 ``repair`` 业务模块，避免把「缺失业务模块」的 RED 混进夹具。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from komari_bot.management.management_audit import hash_management_target

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")
SQLALCHEMY_URL = os.getenv("SQLALCHEMY_DATABASE_URL", "")

REPAIR_TOKEN_TTL = timedelta(minutes=10)
REPAIR_API_PREFIX = "/api/v2/character-bindings/repair"

PG_REQUIRED = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="未设置 KOMARI_TEST_POSTGRES_URL，不能执行 TSK-280 真实 PG 测试",
)


def _same_database(left: str, right: str) -> bool:
    left_parsed = urlparse(left.replace("postgresql+asyncpg://", "postgresql://"))
    right_parsed = urlparse(right.replace("postgresql+asyncpg://", "postgresql://"))
    return (
        left_parsed.hostname == right_parsed.hostname
        and (left_parsed.port or 5432) == (right_parsed.port or 5432)
        and left_parsed.path == right_parsed.path
    )


def require_postgres() -> None:
    """对每个真实库测试显式执行连接配置与同库守卫。"""
    if not POSTGRES_URL:
        pytest.skip("未配置真实 PostgreSQL 测试连接")
    if not _same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip(
            "KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致"
        )


@dataclass(frozen=True, slots=True)
class Scope:
    """一次测试唯一的 app/group/member 作用域。"""

    app_id: str
    group_openid: str
    member_openid: str
    group_id: str
    member_qq: str

    def with_member(self, number: int) -> "Scope":
        return Scope(
            app_id=self.app_id,
            group_openid=self.group_openid,
            member_openid=f"{self.member_openid}-{number}",
            group_id=self.group_id,
            member_qq=f"qq-{self.member_openid}-{number}",
        )


def scope(tag: str = "repair") -> Scope:
    """生成唯一的测试作用域。"""
    suffix = f"{tag}-{uuid4().hex}"
    return Scope(
        app_id=f"tsk280-app-{suffix}",
        group_openid=f"tsk280-group-{suffix}",
        member_openid=f"tsk280-member-{suffix}",
        group_id=f"qq-group-{suffix}",
        member_qq=f"qq-member-{suffix}",
    )


async def reset_shared_orm_engine() -> None:
    """归还 nonebot-plugin-orm 共享引擎连接。"""
    from nonebot import require

    require("nonebot_plugin_orm")
    import nonebot_plugin_orm as orm_module

    engines = getattr(orm_module, "_engines", None)
    if not engines:
        return
    for engine in list(engines.values()):
        with suppress(Exception):
            await engine.dispose()


async def create_engine_and_factory() -> AsyncIterator[
    tuple[AsyncEngine, async_sessionmaker[AsyncSession]]
]:
    """创建真实 PostgreSQL 引擎与会话工厂。"""
    require_postgres()
    await reset_shared_orm_engine()
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


async def backend_pid(session: AsyncSession) -> int:
    return int(await session.scalar(text("SELECT pg_backend_pid()")))


async def wait_for_blocked(
    session_factory: async_sessionmaker[AsyncSession],
    blocker_pid: int,
) -> None:
    """等待真实 PostgreSQL 锁等待者出现，带边界断言。"""
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
    """占用与绑定/轮盘相同的共享组锁。"""
    from komari_bot.db.group_transaction_locks import lock_group_scope

    await lock_group_scope(
        session,
        app_id=current.app_id,
        group_openid=current.group_openid,
    )


async def clear_binding_scope(
    engine: AsyncEngine,
    current: Scope,
) -> None:
    """只清理本测试作用域的绑定表。"""
    params = {
        "app_id": current.app_id,
        "group_openid": current.group_openid,
    }
    async with engine.begin() as connection:
        for statement in (
            "DELETE FROM komari_character_binding_members "
            "WHERE app_id = :app_id AND group_openid = :group_openid",
            "DELETE FROM komari_character_binding_groups "
            "WHERE app_id = :app_id AND group_openid = :group_openid",
        ):
            with suppress(Exception):
                await connection.execute(text(statement), params)


async def clear_roulette_scope(
    engine: AsyncEngine,
    current: Scope,
) -> None:
    """只清理本测试作用域的轮盘表（FK 顺序）。"""
    params = {
        "app_id": current.app_id,
        "group_openid": current.group_openid,
    }
    statements = (
        "DELETE FROM komari_roulette_fulfillments "
        "WHERE receipt_id IN (SELECT receipt_id FROM komari_roulette_command_receipts "
        "WHERE app_id = :app_id AND group_openid = :group_openid)",
        "DELETE FROM komari_roulette_command_receipts "
        "WHERE app_id = :app_id AND group_openid = :group_openid",
        "DELETE FROM komari_roulette_result_players "
        "WHERE game_id IN (SELECT game_id FROM komari_roulette_games "
        "WHERE app_id = :app_id AND group_openid = :group_openid)",
        "DELETE FROM komari_roulette_results "
        "WHERE game_id IN (SELECT game_id FROM komari_roulette_games "
        "WHERE app_id = :app_id AND group_openid = :group_openid)",
        "DELETE FROM komari_roulette_players "
        "WHERE game_id IN (SELECT game_id FROM komari_roulette_games "
        "WHERE app_id = :app_id AND group_openid = :group_openid)",
        "DELETE FROM komari_roulette_games "
        "WHERE app_id = :app_id AND group_openid = :group_openid",
        "DELETE FROM komari_roulette_leaderboard "
        "WHERE app_id = :app_id AND group_openid = :group_openid",
    )
    async with engine.begin() as connection:
        for statement in statements:
            with suppress(Exception):
                await connection.execute(text(statement), params)


async def seed_binding(
    manager: Any,
    current: Scope,
    number: int,
    *,
    name: str | None = None,
) -> str:
    """经既有 271 manager seam 建立一条测试绑定。"""
    member = current.with_member(number)
    await manager.bind_group_member(
        app_id=current.app_id,
        group_id=current.group_id,
        group_openid=current.group_openid,
        member_qq=member.member_qq,
        member_openid=member.member_openid,
        character_name=name or f"Seat {number}",
        bot_self_id="tsk280-test-bot",
    )
    return member.member_openid


# ─── REST 桩服务（duck-typed，只经 API 路由 seam 观察） ───────────────────

READ_CREDENTIALS = [
    {
        "credential_id": "binding-reader",
        "token": "reader-token-00000000",
        "permissions": ["character_binding:read"],
    }
]
MANAGE_CREDENTIALS = [
    {
        "credential_id": "binding-operator",
        "token": "operator-token-000000",
        "permissions": ["character_binding:manage"],
    }
]
WILDCARD_CREDENTIALS = [
    {
        "credential_id": "binding-wildcard",
        "token": "wildcard-token-00000",
        "permissions": ["*"],
    }
]


class StubBindingRepairService:
    """REST 路由测试使用的桩服务端口实现。

    桩服务只复现协议（diagnose/preview/confirm 三个关键字方法）与
    预先配置的失败异常；返回值是普通 dict，由路由层的 response_model
    负责定型，避免在夹具里 import 尚未实现的业务模块。
    """

    def __init__(self) -> None:
        self.diagnose_calls: list[dict[str, str]] = []
        self.preview_calls: list[dict[str, object]] = []
        self.confirm_calls: list[dict[str, object]] = []
        self.diagnosis: dict[str, object] | None = None
        self.preview_result: dict[str, object] | None = None
        self.confirm_result: dict[str, object] | None = None
        self.error: BaseException | None = None

    async def diagnose(self, **kwargs: object) -> dict[str, object]:
        self.diagnose_calls.append({key: str(value) for key, value in kwargs.items()})
        if self.error is not None:
            raise self.error
        if self.diagnosis is None:
            raise AssertionError("stub diagnosis is not configured")  # noqa: TRY003
        return self.diagnosis

    async def preview(self, **kwargs: object) -> dict[str, object]:
        self.preview_calls.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        if self.preview_result is None:
            raise AssertionError("stub preview result is not configured")  # noqa: TRY003
        return self.preview_result

    async def confirm(self, **kwargs: object) -> dict[str, object]:
        self.confirm_calls.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        if self.confirm_result is None:
            raise AssertionError("stub confirm result is not configured")  # noqa: TRY003
        return self.confirm_result


def diagnosis_payload(
    current: Scope,
    *,
    group_id: str | None = None,
    members: Sequence[dict[str, object]] = (),
    game_present: bool = False,
    game_lifecycle: str | None = None,
) -> dict[str, object]:
    return {
        "app_id": current.app_id,
        "group_openid": current.group_openid,
        "group_id": current.group_id if group_id is None else group_id,
        "members": list(members),
        "game_present": game_present,
        "game_lifecycle": game_lifecycle,
    }


def member_view(
    current: Scope,
    number: int = 1,
    *,
    name: str | None = "花火",
) -> dict[str, object]:
    member = current.with_member(number)
    return {
        "app_id": current.app_id,
        "group_openid": current.group_openid,
        "member_openid": member.member_openid,
        "member_qq": member.member_qq,
        "character_name": name,
    }


def preview_payload(
    current: Scope,
    *,
    token: str = "preview-token-1",
    scope: str = "member",
    member_openid: str | None = None,
    affected_count: int = 1,
    cleared_names: Sequence[str] = ("花火",),
    version: str = "deadbeef",
    expires_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "token": token,
        "scope": scope,
        "app_id": current.app_id,
        "group_openid": current.group_openid,
        "member_openid": member_openid or current.member_openid,
        "affected_count": affected_count,
        "cleared_names": list(cleared_names),
        "version": version,
        "expires_at": (expires_at or datetime(2026, 9, 8, 12, 0, tzinfo=UTC)).isoformat(),
    }


def confirm_result_payload(
    current: Scope,
    *,
    scope: str = "member",
    member_openid: str | None = None,
    cleared_count: int = 1,
    cleared_names: Sequence[str] = ("花火",),
) -> dict[str, object]:
    return {
        "scope": scope,
        "app_id": current.app_id,
        "group_openid": current.group_openid,
        "member_openid": member_openid or current.member_openid,
        "cleared_count": cleared_count,
        "cleared_names": list(cleared_names),
    }


def safe_target_hash(current: Scope, *, member_openid: str | None = None) -> str:
    """审计期望使用的安全目标哈希（与路由约定一致）。"""
    return hash_management_target(
        current.app_id,
        current.group_openid,
        member_openid or "<group>",
    )


def write_headers(
    *,
    request_id: str,
    token: str = "wildcard-token-00000",
    reason: str = "运营核对错误关联",
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Komari-Change-Reason": reason,
        "X-Request-ID": request_id,
    }


def read_headers(token: str = "reader-token-00000000") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def now_utc() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "MANAGE_CREDENTIALS",
    "PG_REQUIRED",
    "READ_CREDENTIALS",
    "REPAIR_API_PREFIX",
    "REPAIR_TOKEN_TTL",
    "WILDCARD_CREDENTIALS",
    "Scope",
    "StubBindingRepairService",
    "backend_pid",
    "clear_binding_scope",
    "clear_roulette_scope",
    "confirm_result_payload",
    "create_engine_and_factory",
    "diagnosis_payload",
    "hold_group_lock",
    "member_view",
    "now_utc",
    "preview_payload",
    "read_headers",
    "require_postgres",
    "reset_shared_orm_engine",
    "safe_target_hash",
    "scope",
    "seed_binding",
    "wait_for_blocked",
    "write_headers",
]
