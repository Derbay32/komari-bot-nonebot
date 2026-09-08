"""Shared PostgreSQL transaction locks for one canonical group scope.

The lock deliberately has no transaction or session lifecycle of its own.  A
caller starts the transaction and keeps the same ``AsyncSession`` for every
write that must be ordered with another plugin.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _scope_key(*, app_id: str, group_openid: str) -> str:
    return f"komari-roulette:{app_id}:{group_openid}"


async def lock_group_scope(
    session: AsyncSession,
    *,
    app_id: str,
    group_openid: str,
) -> None:
    """Acquire the shared transaction-scoped lock for a group.

    The key is intentionally kept byte-for-byte compatible with the existing
    roulette storage adapter so old writers and the command service serialize
    on the same PostgreSQL advisory lock.
    """

    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:scope_key, 0))"),
        {"scope_key": _scope_key(app_id=app_id, group_openid=group_openid)},
    )


__all__ = ["lock_group_scope"]
