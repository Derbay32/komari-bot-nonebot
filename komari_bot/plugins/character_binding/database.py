"""角色名绑定 PostgreSQL 访问层（SQLModel + nonebot-plugin-orm AsyncSession）。

连接池与 engine 生命周期由 nonebot-plugin-orm 托管（本模块不再依赖
``komari_bot.common.postgres`` 自研池）；表结构由 Alembic 迁移统一管理，
启动期与懒路径均无任何 DDL。同表只保留本仓储一套访问路径。

SQLModel 字段在 Pyright 下被推断为 Python 值类型而非列表达式，因此
列访问统一走 ``模型.__table__.c``（与 user_ban 仓储同一约定）。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert

from .orm_models import CharacterBindingRow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_BINDINGS = CharacterBindingRow.__table__


def _open_session() -> "AsyncSession":
    """打开绑定 nonebot-plugin-orm 共享引擎的会话。"""
    from nonebot_plugin_orm import get_session

    return get_session(expire_on_commit=False)


class CharacterBindingDB:
    """角色名绑定数据库操作类。"""

    def __init__(self) -> None:
        self._initialize_lock = asyncio.Lock()
        self._ready = False

    async def initialize(self) -> None:
        """单飞确认 ORM 存储可连接；表结构由 Alembic 迁移统一管理。

        PostgreSQL 不可用时抛出异常，由管理器降级为空快照。
        """
        async with self._initialize_lock:
            if self._ready:
                return
            session = _open_session()
            try:
                await session.execute(select(1))
            finally:
                await session.close()
            self._ready = True

    def _require_ready(self) -> None:
        """未初始化时拒绝读写，保持原“尚未初始化”错误语义。"""
        if not self._ready:
            msg = "character_binding 数据库尚未初始化"
            raise RuntimeError(msg)

    async def load_all(self) -> dict[str, str]:
        """读取全部角色名绑定（按 user_id 升序）。"""
        self._require_ready()
        session = _open_session()
        try:
            rows = (
                (
                    await session.execute(
                        select(CharacterBindingRow).order_by(_BINDINGS.c.user_id)
                    )
                )
                .scalars()
                .all()
            )
        finally:
            await session.close()
        return {row.user_id: row.character_name for row in rows}

    async def upsert(self, user_id: str, character_name: str) -> None:
        """新增或更新角色名绑定，冲突时覆盖名称并刷新 updated_at。"""
        self._require_ready()
        session = _open_session()
        try:
            async with session.begin():
                statement = postgres_insert(CharacterBindingRow).values(
                    user_id=user_id, character_name=character_name
                )
                excluded = statement.excluded
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=["user_id"],
                        set_={
                            "character_name": excluded.character_name,
                            "updated_at": func.now(),
                        },
                    )
                )
        finally:
            await session.close()

    async def delete(self, user_id: str) -> bool:
        """删除角色名绑定，并返回记录是否存在。"""
        self._require_ready()
        session = _open_session()
        try:
            async with session.begin():
                deleted = (
                    await session.execute(
                        delete(CharacterBindingRow)
                        .where(_BINDINGS.c.user_id == user_id)
                        .returning(_BINDINGS.c.user_id)
                    )
                ).scalar_one_or_none()
        finally:
            await session.close()
        return deleted is not None

    async def close(self) -> None:
        """重置就绪状态；engine 生命周期由 nonebot-plugin-orm 托管。"""
        async with self._initialize_lock:
            self._ready = False


__all__ = ["CharacterBindingDB"]
