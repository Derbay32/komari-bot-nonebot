# ruff: noqa: N806,TRY003,TRY301

"""Caller-owned transaction facade for canonical character bindings.

The regular manager is a convenient OneBot-facing cache, while this facade is
the narrow shared boundary used by a larger business transaction.  It never
opens, commits, rolls back, or closes the supplied session.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError

from komari_bot.db.group_transaction_locks import lock_group_scope

from .manager import (
    BindingConflictError,
    BindingPersistenceError,
    GroupBindingRecord,
    character_name_key,
    validate_character_name,
)
from .orm_models import CharacterBindingGroupRow, CharacterBindingMemberRow

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.ext.asyncio import AsyncSession


_GROUPS = CharacterBindingGroupRow.__table__
_MEMBERS = CharacterBindingMemberRow.__table__


def _binding_errors() -> tuple[type[RuntimeError], type[RuntimeError]]:
    return BindingConflictError, BindingPersistenceError


class BindingTransaction:
    """Read and write canonical bindings on a caller-owned session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def bind(
        self,
        *,
        app_id: str,
        group_id: str,
        group_openid: str,
        member_qq: str,
        member_openid: str,
        character_name: str,
    ) -> None:
        """Create or update a member binding without committing the session."""

        BindingConflictError, BindingPersistenceError = _binding_errors()
        try:
            normalized_name = validate_character_name(character_name)
            name_key = character_name_key(normalized_name)
            await lock_group_scope(
                self._session,
                app_id=str(app_id),
                group_openid=str(group_openid),
            )
            await self._ensure_group(
                app_id=str(app_id),
                group_id=str(group_id),
                group_openid=str(group_openid),
            )
            existing_by_qq = await self._load_member(
                app_id=str(app_id),
                group_openid=str(group_openid),
                member_qq=str(member_qq),
                for_update=True,
            )
            existing_by_openid = await self._load_member(
                app_id=str(app_id),
                group_openid=str(group_openid),
                member_openid=str(member_openid),
                for_update=True,
            )
            if (
                existing_by_qq is not None
                and existing_by_qq["member_openid"] != str(member_openid)
            ):
                raise BindingConflictError("member_qq 已关联其他官方群成员")
            if (
                existing_by_openid is not None
                and existing_by_openid["member_qq"] != str(member_qq)
            ):
                raise BindingConflictError("member_openid 已关联其他 QQ")
            if await self._name_conflicts(
                app_id=str(app_id),
                group_openid=str(group_openid),
                character_name_key=name_key,
                member_openid=str(member_openid),
            ):
                raise BindingConflictError("同群角色名已被使用")

            existing = existing_by_qq or existing_by_openid
            if existing is None:
                await self._session.execute(
                    CharacterBindingMemberRow.__table__.insert().values(
                        app_id=str(app_id),
                        group_openid=str(group_openid),
                        member_openid=str(member_openid),
                        member_qq=str(member_qq),
                        character_name=normalized_name,
                        character_name_key=name_key,
                    )
                )
            else:
                await self._session.execute(
                    update(_MEMBERS)
                    .where(
                        (_MEMBERS.c.app_id == str(app_id))
                        & (_MEMBERS.c.group_openid == str(group_openid))
                        & (_MEMBERS.c.member_openid == str(member_openid))
                    )
                    .values(
                        character_name=normalized_name,
                        character_name_key=name_key,
                        updated_at=func.now(),
                    )
                )
            await self._session.flush()
        except BindingConflictError:
            raise
        except IntegrityError as error:
            raise BindingConflictError("群、成员或角色名已被其他绑定占用") from error
        except (DBAPIError, SQLAlchemyError, ConnectionError, OSError, TimeoutError) as error:
            raise BindingPersistenceError("角色绑定保存失败") from error

    async def rename(
        self,
        *,
        app_id: str,
        group_openid: str,
        member_openid: str,
        character_name: str,
    ) -> None:
        """Rename a member in the caller's transaction."""

        BindingConflictError, BindingPersistenceError = _binding_errors()
        try:
            normalized_name = validate_character_name(character_name)
            await lock_group_scope(
                self._session,
                app_id=str(app_id),
                group_openid=str(group_openid),
            )
            member = await self._load_member(
                app_id=str(app_id),
                group_openid=str(group_openid),
                member_openid=str(member_openid),
                for_update=True,
            )
            if member is None:
                raise BindingPersistenceError("群成员身份不存在")
            name_key = character_name_key(normalized_name)
            if await self._name_conflicts(
                app_id=str(app_id),
                group_openid=str(group_openid),
                character_name_key=name_key,
                member_openid=str(member_openid),
            ):
                raise BindingConflictError("同群角色名已被使用")
            await self._session.execute(
                update(_MEMBERS)
                .where(
                    (_MEMBERS.c.app_id == str(app_id))
                    & (_MEMBERS.c.group_openid == str(group_openid))
                    & (_MEMBERS.c.member_openid == str(member_openid))
                )
                .values(
                    character_name=normalized_name,
                    character_name_key=name_key,
                    updated_at=func.now(),
                )
            )
            await self._session.flush()
        except (BindingConflictError, BindingPersistenceError):
            raise
        except IntegrityError as error:
            raise BindingConflictError("群、成员或角色名已被其他绑定占用") from error
        except (DBAPIError, SQLAlchemyError, ConnectionError, OSError, TimeoutError) as error:
            raise BindingPersistenceError("角色绑定保存失败") from error

    async def clear(
        self,
        *,
        app_id: str,
        group_openid: str,
        member_openid: str,
    ) -> bool:
        """Clear only the display name, preserving canonical identity."""

        _, BindingPersistenceError = _binding_errors()
        try:
            await lock_group_scope(
                self._session,
                app_id=str(app_id),
                group_openid=str(group_openid),
            )
            result = await self._session.execute(
                update(_MEMBERS)
                .where(
                    (_MEMBERS.c.app_id == str(app_id))
                    & (_MEMBERS.c.group_openid == str(group_openid))
                    & (_MEMBERS.c.member_openid == str(member_openid))
                    & _MEMBERS.c.character_name.is_not(None)
                )
                .values(
                    character_name=None,
                    character_name_key=None,
                    updated_at=func.now(),
                )
            )
            await self._session.flush()
            return int(getattr(result, "rowcount", 0)) > 0
        except (DBAPIError, SQLAlchemyError, ConnectionError, OSError, TimeoutError) as error:
            raise BindingPersistenceError("角色绑定保存失败") from error

    async def resolve_group(
        self,
        *,
        app_id: str,
        group_openid: str,
    ) -> GroupBindingGroup | None:
        """Resolve a group mapping; ``None`` means it truly is unmapped."""

        _, BindingPersistenceError = _binding_errors()
        try:
            await lock_group_scope(
                self._session,
                app_id=str(app_id),
                group_openid=str(group_openid),
            )
            row = (
                await self._session.execute(
                    select(_GROUPS).where(
                        (_GROUPS.c.app_id == str(app_id))
                        & (_GROUPS.c.group_openid == str(group_openid))
                    )
                )
            ).mappings().one_or_none()
            if row is None:
                return None
            return GroupBindingGroup(
                app_id=str(row["app_id"]),
                group_openid=str(row["group_openid"]),
                group_id=str(row["group_id"]),
            )
        except (DBAPIError, SQLAlchemyError, ConnectionError, OSError, TimeoutError) as error:
            raise BindingPersistenceError("角色绑定读取失败") from error

    async def resolve_member(
        self,
        *,
        app_id: str,
        group_openid: str,
        member_openid: str,
    ) -> GroupBindingRecord | None:
        """Read the current canonical member row from PostgreSQL."""

        _, BindingPersistenceError = _binding_errors()
        try:
            await lock_group_scope(
                self._session,
                app_id=str(app_id),
                group_openid=str(group_openid),
            )
            result = await self._session.execute(
                select(
                    _MEMBERS,
                    _GROUPS.c.group_id.label("binding_group_id"),
                )
                .select_from(
                    _GROUPS.join(
                        _MEMBERS,
                        (_GROUPS.c.app_id == _MEMBERS.c.app_id)
                        & (_GROUPS.c.group_openid == _MEMBERS.c.group_openid),
                    )
                )
                .where(
                    (_MEMBERS.c.app_id == str(app_id))
                    & (_MEMBERS.c.group_openid == str(group_openid))
                    & (_MEMBERS.c.member_openid == str(member_openid))
                )
            )
            row = result.mappings().one_or_none()
            if row is None:
                return None
            return _member_record(row)
        except (DBAPIError, SQLAlchemyError, ConnectionError, OSError, TimeoutError) as error:
            raise BindingPersistenceError("角色绑定读取失败") from error

    async def resolve_member_by_qq(
        self,
        *,
        app_id: str,
        group_openid: str,
        member_qq: str,
    ) -> GroupBindingRecord | None:
        """Resolve a member by its OneBot QQ identity under the group lock."""

        _, BindingPersistenceError = _binding_errors()
        try:
            await lock_group_scope(
                self._session,
                app_id=str(app_id),
                group_openid=str(group_openid),
            )
            result = await self._session.execute(
                select(
                    _MEMBERS,
                    _GROUPS.c.group_id.label("binding_group_id"),
                )
                .select_from(
                    _GROUPS.join(
                        _MEMBERS,
                        (_GROUPS.c.app_id == _MEMBERS.c.app_id)
                        & (_GROUPS.c.group_openid == _MEMBERS.c.group_openid),
                    )
                )
                .where(
                    (_MEMBERS.c.app_id == str(app_id))
                    & (_MEMBERS.c.group_openid == str(group_openid))
                    & (_MEMBERS.c.member_qq == str(member_qq))
                )
            )
            row = result.mappings().one_or_none()
            if row is None:
                return None
            return _member_record(row)
        except (DBAPIError, SQLAlchemyError, ConnectionError, OSError, TimeoutError) as error:
            raise BindingPersistenceError("角色绑定读取失败") from error

    async def _ensure_group(
        self,
        *,
        app_id: str,
        group_id: str,
        group_openid: str,
    ) -> None:
        by_id = (
            await self._session.execute(
                select(_GROUPS)
                .where(
                    (_GROUPS.c.app_id == app_id) & (_GROUPS.c.group_id == group_id)
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        by_openid = (
            await self._session.execute(
                select(_GROUPS)
                .where(
                    (_GROUPS.c.app_id == app_id)
                    & (_GROUPS.c.group_openid == group_openid)
                )
                .with_for_update()
            )
        ).mappings().one_or_none()
        if by_id is not None and by_id["group_openid"] != group_openid:
            BindingConflictError, _ = _binding_errors()
            raise BindingConflictError("group_id 已关联其他官方群身份")
        if by_openid is not None and by_openid["group_id"] != group_id:
            BindingConflictError, _ = _binding_errors()
            raise BindingConflictError("group_openid 已关联其他 OneBot 群")
        if by_id is None and by_openid is None:
            await self._session.execute(
                _GROUPS.insert().values(
                    app_id=app_id,
                    group_openid=group_openid,
                    group_id=group_id,
                )
            )
            await self._session.flush()
            return
        if by_id is None or by_openid is None:
            _, BindingPersistenceError = _binding_errors()
            raise BindingPersistenceError("群身份关联不完整")

    async def _load_member(
        self,
        *,
        app_id: str,
        group_openid: str,
        for_update: bool,
        member_qq: str | None = None,
        member_openid: str | None = None,
    ) -> Any | None:
        if (member_qq is None) == (member_openid is None):
            raise ValueError("必须且只能提供一种成员身份查询键")
        column = _MEMBERS.c.member_qq if member_qq is not None else _MEMBERS.c.member_openid
        value = member_qq if member_qq is not None else member_openid
        statement = select(_MEMBERS).where(
            (_MEMBERS.c.app_id == app_id)
            & (_MEMBERS.c.group_openid == group_openid)
            & (column == value)
        )
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).mappings().one_or_none()

    async def _name_conflicts(
        self,
        *,
        app_id: str,
        group_openid: str,
        character_name_key: str,
        member_openid: str,
    ) -> bool:
        row = (
            await self._session.execute(
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


@dataclass(frozen=True, slots=True)
class GroupBindingGroup:
    """Small public DTO for a canonical group mapping."""

    app_id: str
    group_openid: str
    group_id: str


def _member_record(row: Mapping[Any, Any]) -> GroupBindingRecord:
    return GroupBindingRecord(
        app_id=str(row["app_id"]),
        group_id=str(row["binding_group_id"]),
        group_openid=str(row["group_openid"]),
        member_qq=str(row["member_qq"]),
        member_openid=str(row["member_openid"]),
        character_name=(
            str(row["character_name"])
            if row["character_name"] is not None
            else None
        ),
        character_name_key=(
            str(row["character_name_key"])
            if row["character_name_key"] is not None
            else None
        ),
    )

__all__ = [
    "BindingConflictError",
    "BindingPersistenceError",
    "BindingTransaction",
    "GroupBindingGroup",
]
