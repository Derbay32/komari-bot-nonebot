"""群成员角色资料管理器。

管理器把 OneBot 的 ``(group_id, user_id)`` 只作为查询/命令桥接输入；正式
资料始终按 QQ 应用、官方群和官方成员 OpenID 保存。数据库事务成功后才
替换整份不可变快照，读取路径不会观察到半笔绑定。
"""

from __future__ import annotations

import asyncio
import unicodedata
from dataclasses import dataclass
from typing import Any

from nonebot import logger
from sqlalchemy.exc import IntegrityError

from .database import CharacterBindingDB, _open_session

MAX_CHARACTER_NAME_LENGTH = 64
_EMPTY_NAME_ERROR = "角色名不能为空"
_NAME_TOO_LONG_ERROR = f"角色名不能超过 {MAX_CHARACTER_NAME_LENGTH} 个 Unicode 字符"
_UNSAFE_NAME_ERROR = "角色名不能包含换行或控制字符"
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


class CharacterNameValidationError(ValueError):
    """角色名不满足输入约束。"""


class BindingConflictError(RuntimeError):
    """群、成员身份或同群角色名发生冲突。"""


class BindingPersistenceError(RuntimeError):
    """角色绑定无法安全持久化或读取。"""


@dataclass(frozen=True, slots=True)
class GroupBindingRecord:
    """快照中的一条群成员资料。"""

    app_id: str
    group_id: str
    group_openid: str
    member_qq: str
    member_openid: str
    character_name: str | None
    character_name_key: str | None


def _require_current_onebot_identity(
    current: GroupBindingRecord | None,
    cached: GroupBindingRecord,
) -> GroupBindingRecord:
    if (
        current is None
        or current.group_id != cached.group_id
        or current.member_openid != cached.member_openid
    ):
        raise BindingPersistenceError("暂时无法确认本群身份")
    return current


def validate_character_name(character_name: str) -> str:
    """整理并校验角色名，返回可展示的规范文本。"""
    if not isinstance(character_name, str):
        raise CharacterNameValidationError(_EMPTY_NAME_ERROR)
    if any(unicodedata.category(char) in _UNSAFE_CATEGORIES for char in character_name):
        raise CharacterNameValidationError(_UNSAFE_NAME_ERROR)

    normalized = " ".join(character_name.split())
    if not normalized:
        raise CharacterNameValidationError(_EMPTY_NAME_ERROR)
    if len(normalized) > MAX_CHARACTER_NAME_LENGTH:
        raise CharacterNameValidationError(_NAME_TOO_LONG_ERROR)
    return normalized


def character_name_key(character_name: str) -> str:
    """生成同群唯一性使用的 NFKC + casefold 比较键。"""
    return unicodedata.normalize("NFKC", character_name).casefold()


@dataclass(slots=True)
class PluginState:
    """character_binding 模块级状态。"""

    manager: CharacterBindingManager | None = None


state = PluginState()


def get_manager() -> CharacterBindingManager:
    """获取管理器单例实例。"""
    if state.manager is None:
        state.manager = CharacterBindingManager()
    return state.manager


class CharacterBindingManager:
    """持有角色资料快照并协调 PostgreSQL 原子写入。"""

    def __init__(self) -> None:
        self._database = CharacterBindingDB()
        self._records: tuple[GroupBindingRecord, ...] = ()
        self._lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        """初始化存储并发布完整快照；失败时以空快照故障关闭。"""
        if self._initialized:
            return

        async with self._lock:
            if self._initialized:
                return
            try:
                await self._database.initialize()
                rows = await self._database.load_all()
                snapshot = self._build_snapshot(rows)
            except Exception as error:
                await self._close_database_after_failure()
                self._records = ()
                logger.error(
                    "[CharacterBinding] PostgreSQL 初始化失败，使用空绑定快照降级: error_type={}",
                    type(error).__name__,
                )
                return

            self._records = snapshot
            self._initialized = True
            logger.info("[CharacterBinding] 加载群成员角色资料: {} 条", len(snapshot))

    @staticmethod
    def _build_snapshot(rows: list[dict[str, Any]]) -> tuple[GroupBindingRecord, ...]:
        return tuple(
            GroupBindingRecord(
                app_id=str(row["app_id"]),
                group_id=str(row["group_id"]),
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
            for row in rows
        )

    async def _refresh_snapshot_locked(self) -> None:
        rows = await self._database.load_all()
        self._records = self._build_snapshot(rows)

    async def refresh_snapshot(self) -> None:
        """重新发布已提交的正式绑定快照。

        只允许在调用方确认提交成功后发布；提交失败或结果不确定时不得调用，
        以免读取路径观察到未提交或不确定的正式值。
        """
        async with self._lock:
            if not self._initialized:
                return
            await self._refresh_snapshot_locked()

    async def invalidate_repair_scope(
        self,
        *,
        app_id: str,
        group_openid: str,
        member_openid: str | None = None,
    ) -> None:
        """定向移除已提交修复影响的快照条目（两种协议一致）。

        只允许在确认正式删除已提交后调用：按 ``(app_id, group_openid)`` 移除
        整组（``member_openid`` 为空）或单个成员关系，绝不触碰其他成员/群；
        不重载全表、不新增 schema。持 ``_lock`` 与在途快照重建串行，使读+
        发布原子，避免旧 ``load_all`` 读数回填已删身份。
        """
        target_app = str(app_id)
        target_group = str(group_openid)
        async with self._lock:
            if member_openid is None:
                self._records = tuple(
                    record
                    for record in self._records
                    if not (
                        record.app_id == target_app
                        and record.group_openid == target_group
                    )
                )
                return
            target_member = str(member_openid)
            self._records = tuple(
                record
                for record in self._records
                if not (
                    record.app_id == target_app
                    and record.group_openid == target_group
                    and record.member_openid == target_member
                )
            )

    async def _close_database_after_failure(self) -> None:
        try:
            await self._database.close()
        except Exception:
            logger.exception("[CharacterBinding] 初始化失败后的数据库清理失败")

    async def close(self) -> None:
        """清空快照和全局引用；不销毁共享 ORM engine。"""
        if state.manager is self:
            state.manager = None

        async with self._lock:
            self._records = ()
            self._initialized = False
            await self._database.close()

    async def bind_group_member(
        self,
        *,
        app_id: str,
        group_id: str,
        group_openid: str,
        member_qq: str,
        member_openid: str,
        character_name: str,
        bot_self_id: str | None = None,
    ) -> None:
        """原子绑定群成员角色名；``bot_self_id`` 仅为审计兼容参数。"""
        del bot_self_id
        normalized_name = validate_character_name(character_name)
        async with self._lock:
            if not self._initialized:
                raise BindingPersistenceError(  # noqa: TRY003
                    "character_binding 数据库尚未初始化"
                )
            from .transaction import BindingTransaction

            session = _open_session()
            try:
                async with session.begin():
                    transaction = BindingTransaction(session)
                    await transaction.bind(
                        app_id=str(app_id),
                        group_id=str(group_id),
                        group_openid=str(group_openid),
                        member_qq=str(member_qq),
                        member_openid=str(member_openid),
                        character_name=normalized_name,
                    )
            except BindingConflictError:
                raise
            except BindingPersistenceError:
                raise
            except IntegrityError as error:
                raise BindingConflictError("群、成员或角色名已被其他绑定占用") from error
            except Exception as error:
                logger.error(
                    "[CharacterBinding] 群成员绑定写入失败: error_type={}",
                    type(error).__name__,
                )
                raise BindingPersistenceError("角色绑定保存失败") from error
            finally:
                await session.close()

            try:
                await self._refresh_snapshot_locked()
            except Exception as error:
                logger.error(
                    "[CharacterBinding] 群成员绑定提交后刷新快照失败: error_type={}",
                    type(error).__name__,
                )
                raise BindingPersistenceError("角色绑定保存结果无法确认") from error

        logger.info(
            "[CharacterBinding] 绑定群成员角色: name_length={}",
            len(normalized_name),
        )

    async def _mutate_onebot_cached_member(
        self,
        record: GroupBindingRecord,
        *,
        character_name: str | None,
    ) -> bool:
        """Revalidate a cached OneBot identity before a rename or clear."""

        normalized_name = (
            validate_character_name(character_name)
            if character_name is not None
            else None
        )
        async with self._lock:
            if not self._initialized:
                raise BindingPersistenceError(  # noqa: TRY003
                    "character_binding 数据库尚未初始化"
                )
            from .transaction import BindingTransaction

            session = _open_session()
            try:
                async with session.begin():
                    transaction = BindingTransaction(session)
                    current = await transaction.resolve_member_by_qq(
                        app_id=record.app_id,
                        group_openid=record.group_openid,
                        member_qq=record.member_qq,
                    )
                    current = _require_current_onebot_identity(current, record)
                    if normalized_name is None:
                        changed = await transaction.clear(
                            app_id=current.app_id,
                            group_openid=current.group_openid,
                            member_openid=current.member_openid,
                        )
                    else:
                        await transaction.rename(
                            app_id=current.app_id,
                            group_openid=current.group_openid,
                            member_openid=current.member_openid,
                            character_name=normalized_name,
                        )
                        changed = True
            except (BindingConflictError, BindingPersistenceError):
                raise
            except Exception as error:
                logger.error(
                    "[CharacterBinding] OneBot 群成员角色写入失败: error_type={}",
                    type(error).__name__,
                )
                raise BindingPersistenceError("角色绑定保存失败") from error
            finally:
                await session.close()

            if not changed:
                return False
            try:
                await self._refresh_snapshot_locked()
            except Exception as error:
                logger.error(
                    "[CharacterBinding] 群成员角色提交后刷新快照失败: error_type={}",
                    type(error).__name__,
                )
                raise BindingPersistenceError("角色绑定保存结果无法确认") from error
            return True

    async def clear_character_name(
        self,
        *,
        app_id: str,
        group_openid: str,
        member_openid: str,
    ) -> bool:
        """只清除当前群角色名，保留已验证的身份关系。"""
        async with self._lock:
            if not self._initialized:
                raise BindingPersistenceError(  # noqa: TRY003
                    "character_binding 数据库尚未初始化"
                )
            from .transaction import BindingTransaction

            session = _open_session()
            try:
                async with session.begin():
                    transaction = BindingTransaction(session)
                    cleared = await transaction.clear(
                        app_id=str(app_id),
                        group_openid=str(group_openid),
                        member_openid=str(member_openid),
                    )
            except Exception as error:
                logger.error(
                    "[CharacterBinding] 清除群成员角色失败: error_type={}",
                    type(error).__name__,
                )
                raise BindingPersistenceError("角色绑定保存失败") from error
            finally:
                await session.close()
            if not cleared:
                return False
            try:
                await self._refresh_snapshot_locked()
            except Exception as error:
                logger.error(
                    "[CharacterBinding] 清除角色后刷新快照失败: error_type={}",
                    type(error).__name__,
                )
                raise BindingPersistenceError("角色绑定保存结果无法确认") from error
            return True

    def get_qq_character_name(
        self,
        *,
        app_id: str,
        group_openid: str,
        member_openid: str,
        fallback_nickname: str | None = None,
    ) -> str | None:
        """按正式 QQ 群身份查询角色名。"""
        for record in self._records:
            if (
                record.app_id == str(app_id)
                and record.group_openid == str(group_openid)
                and record.member_openid == str(member_openid)
                and record.character_name is not None
            ):
                return record.character_name
        return fallback_nickname

    def get_character_name(
        self,
        *,
        group_id: str,
        user_id: str,
        fallback_nickname: str | None = None,
    ) -> str:
        """OneBot 窄桥接查询；多应用映射歧义时故障关闭。"""
        member_records = [
            record
            for record in self._records
            if record.group_id == str(group_id)
            and record.member_qq == str(user_id)
        ]
        identities = {
            (record.app_id, record.group_openid, record.member_openid)
            for record in member_records
        }
        if len(identities) == 1 and member_records:
            character_name = member_records[0].character_name
            if character_name is not None:
                return character_name
        return fallback_nickname or str(user_id)

    def list_group_bindings(
        self,
        *,
        app_id: str,
        group_openid: str,
    ) -> tuple[GroupBindingRecord, ...]:
        """列出 canonical QQ 群的完整成员关系。"""
        return tuple(
            record
            for record in self._records
            if record.app_id == str(app_id) and record.group_openid == str(group_openid)
        )

    def list_onebot_group_bindings(self, *, group_id: str) -> dict[str, str]:
        """列出唯一可解析 OneBot 群的已设置角色名。"""
        candidates = [record for record in self._records if record.group_id == str(group_id)]
        identities = {(record.app_id, record.group_openid) for record in candidates}
        if len(identities) != 1:
            return {}
        return {
            record.member_qq: record.character_name
            for record in candidates
            if record.character_name is not None
        }

    def _resolve_onebot_member(self, *, group_id: str, user_id: str) -> GroupBindingRecord:
        candidates = [
            record
            for record in self._records
            if record.group_id == str(group_id) and record.member_qq == str(user_id)
        ]
        identities = {
            (record.app_id, record.group_openid, record.member_openid)
            for record in candidates
        }
        if len(identities) != 1:
            raise BindingPersistenceError("暂时无法确认本群身份")
        return candidates[0]

    async def set_group_character_name(
        self,
        group_id: str,
        user_id: str,
        character_name: str,
    ) -> None:
        """通过已验证 OneBot 桥接设置当前群角色名。"""
        record = self._resolve_onebot_member(group_id=group_id, user_id=user_id)
        await self._mutate_onebot_cached_member(
            record,
            character_name=character_name,
        )

    async def clear_group_character_name(self, group_id: str, user_id: str) -> bool:
        """通过已验证 OneBot 桥接清除当前群角色名。"""
        record = self._resolve_onebot_member(group_id=group_id, user_id=user_id)
        return await self._mutate_onebot_cached_member(
            record,
            character_name=None,
        )

    async def get_legacy_character_name(self, user_id: str) -> str | None:
        """读取旧全局表作为显式 /bind 迁移候选，不参与日常查询。"""
        try:
            return await self._database.load_legacy_character_name(user_id)
        except Exception as error:
            logger.error(
                "[CharacterBinding] 读取旧角色名迁移候选失败: error_type={}",
                type(error).__name__,
            )
            raise BindingPersistenceError("角色绑定迁移候选读取失败") from error


__all__ = [
    "MAX_CHARACTER_NAME_LENGTH",
    "BindingConflictError",
    "BindingPersistenceError",
    "CharacterBindingManager",
    "CharacterNameValidationError",
    "GroupBindingRecord",
    "character_name_key",
    "get_manager",
    "validate_character_name",
]
