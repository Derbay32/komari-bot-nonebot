"""角色绑定 PostgreSQL 访问层。

群映射、成员身份和角色名必须在同一个事务中写入。此模块只负责 SQL 与
事务边界，不拥有进程内快照；快照由 :mod:`manager` 在事务提交后更新。
连接和 ``AsyncSession`` 的生命周期仍由 ``nonebot-plugin-orm`` 托管。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert

from .orm_models import CharacterBindingGroupRow, CharacterBindingMemberRow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_GROUPS = CharacterBindingGroupRow.__table__
_MEMBERS = CharacterBindingMemberRow.__table__


class DatabaseBindingConflictError(RuntimeError):
    """数据库中已有与本次身份或角色名冲突的记录。"""


def _open_session() -> "AsyncSession":
    """打开 nonebot-plugin-orm 管理的共享会话。"""
    from nonebot_plugin_orm import get_session

    return get_session(expire_on_commit=False)


class CharacterBindingDB:
    """角色绑定关系的事务存储。"""

    def __init__(self) -> None:
        self._initialize_lock = asyncio.Lock()
        self._ready = False

    async def initialize(self) -> None:
        """单飞确认存储可用；迁移是唯一建表入口。"""
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
        if not self._ready:
            raise RuntimeError("character_binding 数据库尚未初始化")  # noqa: TRY003

    async def load_all(self) -> list[dict[str, Any]]:
        """加载完整的群/成员关系，供管理器构造不可变快照。"""
        self._require_ready()
        session = _open_session()
        try:
            statement = (
                select(
                    _GROUPS.c.app_id,
                    _GROUPS.c.group_id,
                    _GROUPS.c.group_openid,
                    _MEMBERS.c.member_qq,
                    _MEMBERS.c.member_openid,
                    _MEMBERS.c.character_name,
                    _MEMBERS.c.character_name_key,
                )
                .select_from(
                    _GROUPS.join(
                        _MEMBERS,
                        (_GROUPS.c.app_id == _MEMBERS.c.app_id)
                        & (_GROUPS.c.group_openid == _MEMBERS.c.group_openid),
                        isouter=True,
                    )
                )
                .order_by(
                    _GROUPS.c.app_id,
                    _GROUPS.c.group_openid,
                    _MEMBERS.c.member_openid,
                )
            )
            rows = (await session.execute(statement)).mappings().all()
            return [dict(row) for row in rows if row["member_openid"] is not None]
        finally:
            await session.close()

    async def bind_group_member(
        self,
        *,
        app_id: str,
        group_id: str,
        group_openid: str,
        member_qq: str,
        member_openid: str,
        character_name: str,
        character_name_key: str,
    ) -> None:
        """原子建立/复用群和成员身份，并写入角色名。"""
        self._require_ready()
        session = _open_session()
        try:
            async with session.begin():
                group_by_id = await self._load_group_for_update(
                    session,
                    app_id=app_id,
                    group_id=group_id,
                )
                group_by_openid = await self._load_group_for_update(
                    session,
                    app_id=app_id,
                    group_openid=group_openid,
                )

                if group_by_id is not None and group_by_id["group_openid"] != group_openid:
                    raise DatabaseBindingConflictError("group_id 已关联其他官方群身份")  # noqa: TRY003
                if group_by_openid is not None and group_by_openid["group_id"] != group_id:
                    raise DatabaseBindingConflictError("group_openid 已关联其他 OneBot 群")  # noqa: TRY003

                if group_by_id is None and group_by_openid is None:
                    await session.execute(
                        postgres_insert(CharacterBindingGroupRow)
                        .values(
                            app_id=app_id,
                            group_openid=group_openid,
                            group_id=group_id,
                        )
                        .on_conflict_do_nothing()
                    )
                    group_by_id = await self._load_group_for_update(
                        session,
                        app_id=app_id,
                        group_id=group_id,
                    )
                    group_by_openid = await self._load_group_for_update(
                        session,
                        app_id=app_id,
                        group_openid=group_openid,
                    )
                    if (
                        group_by_id is None
                        or group_by_openid is None
                        or group_by_id["group_openid"] != group_openid
                        or group_by_openid["group_id"] != group_id
                    ):
                        raise DatabaseBindingConflictError("群身份关联不完整")
                elif group_by_id is None or group_by_openid is None:
                    raise DatabaseBindingConflictError("群身份关联不完整")

                existing_by_qq = await self._load_member_for_update(
                    session,
                    app_id=app_id,
                    group_openid=group_openid,
                    member_qq=member_qq,
                )
                existing_by_openid = await self._load_member_for_update(
                    session,
                    app_id=app_id,
                    group_openid=group_openid,
                    member_openid=member_openid,
                )

                if (
                    existing_by_qq is not None
                    and existing_by_qq["member_openid"] != member_openid
                ):
                    raise DatabaseBindingConflictError("member_qq 已关联其他官方群成员")  # noqa: TRY003
                if (
                    existing_by_openid is not None
                    and existing_by_openid["member_qq"] != member_qq
                ):
                    raise DatabaseBindingConflictError("member_openid 已关联其他 QQ")  # noqa: TRY003
                if (
                    existing_by_qq is not None
                    and existing_by_openid is not None
                    and existing_by_qq["member_openid"] != existing_by_openid["member_openid"]
                ):
                    raise DatabaseBindingConflictError("成员双向身份关联不一致")

                existing = existing_by_qq or existing_by_openid
                name_conflict = await self._name_conflicts(
                    session,
                    app_id=app_id,
                    group_openid=group_openid,
                    character_name_key=character_name_key,
                    member_openid=member_openid,
                )
                if name_conflict:
                    raise DatabaseBindingConflictError("同群角色名已被使用")

                if existing is None:
                    await session.execute(
                        postgres_insert(CharacterBindingMemberRow).values(
                            app_id=app_id,
                            group_openid=group_openid,
                            member_openid=member_openid,
                            member_qq=member_qq,
                            character_name=character_name,
                            character_name_key=character_name_key,
                        )
                    )
                else:
                    await session.execute(
                        update(_MEMBERS)
                        .where(
                            (_MEMBERS.c.app_id == app_id)
                            & (_MEMBERS.c.group_openid == group_openid)
                            & (_MEMBERS.c.member_openid == member_openid)
                        )
                        .values(
                            character_name=character_name,
                            character_name_key=character_name_key,
                            updated_at=func.now(),
                        )
                    )
        finally:
            await session.close()

    async def clear_character_name(
        self,
        *,
        app_id: str,
        group_openid: str,
        member_openid: str,
    ) -> bool:
        """清除角色名但保留群与成员身份关联。"""
        self._require_ready()
        session = _open_session()
        try:
            async with session.begin():
                result = await session.execute(
                    update(_MEMBERS)
                    .where(
                        (_MEMBERS.c.app_id == app_id)
                        & (_MEMBERS.c.group_openid == group_openid)
                        & (_MEMBERS.c.member_openid == member_openid)
                        & _MEMBERS.c.character_name.is_not(None)
                    )
                    .values(
                        character_name=None,
                        character_name_key=None,
                        updated_at=func.now(),
                    )
                )
                return int(getattr(result, "rowcount", 0)) > 0
        finally:
            await session.close()

    async def load_legacy_character_name(self, user_id: str) -> str | None:
        """读取旧全局表的一条记录，供显式迁移流程使用。"""
        self._require_ready()
        session = _open_session()
        try:
            row = (
                await session.execute(
                    text(
                        "SELECT character_name "
                        "FROM komari_character_bindings "
                        "WHERE user_id = :user_id"
                    ),
                    {"user_id": str(user_id)},
                )
            ).mappings().one_or_none()
            return str(row["character_name"]) if row is not None else None
        finally:
            await session.close()

    async def _load_group_for_update(
        self,
        session: "AsyncSession",
        *,
        app_id: str,
        group_id: str | None = None,
        group_openid: str | None = None,
    ) -> Any:
        if (group_id is None) == (group_openid is None):
            raise ValueError("必须且只能提供一种群身份查询键")
        column = _GROUPS.c.group_id if group_id is not None else _GROUPS.c.group_openid
        value = group_id if group_id is not None else group_openid
        return (
            await session.execute(
                select(_GROUPS)
                .where((_GROUPS.c.app_id == app_id) & (column == value))
                .with_for_update()
            )
        ).mappings().one_or_none()

    async def _load_member_for_update(
        self,
        session: "AsyncSession",
        *,
        app_id: str,
        group_openid: str,
        member_qq: str | None = None,
        member_openid: str | None = None,
    ) -> Any:
        if (member_qq is None) == (member_openid is None):
            raise ValueError("必须且只能提供一种成员身份查询键")
        column = (
            _MEMBERS.c.member_qq
            if member_qq is not None
            else _MEMBERS.c.member_openid
        )
        value = member_qq if member_qq is not None else member_openid
        return (
            await session.execute(
                select(_MEMBERS)
                .where(
                    (_MEMBERS.c.app_id == app_id)
                    & (_MEMBERS.c.group_openid == group_openid)
                    & (column == value)
                )
                .with_for_update()
            )
        ).mappings().one_or_none()

    async def _name_conflicts(
        self,
        session: "AsyncSession",
        *,
        app_id: str,
        group_openid: str,
        character_name_key: str,
        member_openid: str,
    ) -> bool:
        row = (
            await session.execute(
                select(_MEMBERS.c.member_openid)
                .where(
                    (_MEMBERS.c.app_id == app_id)
                    & (_MEMBERS.c.group_openid == group_openid)
                    & (_MEMBERS.c.character_name_key == character_name_key)
                    & (_MEMBERS.c.member_openid != member_openid)
                )
                .with_for_update()
            )
        ).first()
        return row is not None

    async def close(self) -> None:
        """重置就绪状态；不 dispose 共享 ORM engine。"""
        async with self._initialize_lock:
            self._ready = False


__all__ = ["CharacterBindingDB", "DatabaseBindingConflictError"]
