# ruff: noqa: TRY301

"""TSK-280 角色绑定受权运维修复服务。

统一 REST 控制面的业务核心：诊断只读直读 PostgreSQL；预览计算影响范围并
签发绑定操作者与目标版本的短期单次令牌；确认在共享组锁内复核槽位、依赖
集合与令牌 TTL 后原子清除错误关联。绝不强制终局、改写历史结果/胜场、
修改准入或替用户重绑。

对局状态经构造注入的 ``game_state_reader`` 读取（由管理装配注入真实存储
公共 seam）；本模块不接触任何游戏插件内部，也不反向依赖管理插件。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from nonebot import logger
from sqlalchemy import delete, literal_column, select
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from komari_bot.db.group_transaction_locks import lock_group_scope

from .manager import BindingPersistenceError
from .orm_models import CharacterBindingGroupRow, CharacterBindingMemberRow

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from .manager import CharacterBindingManager

TOKEN_TTL = timedelta(minutes=10)

RepairScope = Literal["member", "group"]

_GROUPS = CharacterBindingGroupRow.__table__
_MEMBERS = CharacterBindingMemberRow.__table__
_GROUP_GENERATION = literal_column(
    "komari_character_binding_groups.xmin::text"
).label("row_generation")
_MEMBER_GENERATION = literal_column(
    "komari_character_binding_members.xmin::text"
).label("row_generation")

_NOT_FOUND_GROUP = "未找到目标群映射"
_NOT_FOUND_MEMBER = "未找到目标成员关联"
_DEPENDENCY_CHANGED = "绑定状态已变化，请重新预览"
_BLOCKED_BY_GAME = "群内存在进行中的对局，无法修复"
_TOKEN_INVALID = "确认令牌无效、已过期或已使用"
_STORAGE_UNAVAILABLE = "绑定修复存储暂不可用"
_CLOSED = "绑定修复服务已关闭"


class _GameSnapshotView(Protocol):
    """注入读取器返回的对局快照最小结构面。"""

    game_id: str
    lifecycle: str
    state_revision: int


@dataclass(frozen=True, slots=True)
class MemberBindingView:
    """诊断中的单个成员关系视图（含已解绑、角色名为空的成员）。"""

    app_id: str
    group_openid: str
    member_openid: str
    member_qq: str
    character_name: str | None


@dataclass(frozen=True, slots=True)
class BindingDiagnosis:
    """只读诊断结果：群映射、全部成员视图与是否存在 waiting/active 对局。"""

    app_id: str
    group_openid: str
    group_id: str
    members: tuple[MemberBindingView, ...]
    game_present: bool
    game_lifecycle: str | None


@dataclass(frozen=True, slots=True)
class RepairPreview:
    """影响预览与一次性确认令牌。"""

    token: str
    scope: RepairScope
    app_id: str
    group_openid: str
    member_openid: str | None
    affected_count: int
    cleared_names: tuple[str | None, ...]
    version: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class RepairConfirmResult:
    """确认清除的实际结果，携带本次消耗的预览版本与预期清除数量。"""

    scope: RepairScope
    app_id: str
    group_openid: str
    member_openid: str | None
    cleared_count: int
    cleared_names: tuple[str | None, ...]
    version: str
    expected_count: int


class RepairTokenError(RuntimeError):
    """确认令牌无效、已过期、已使用或与操作者/目标不符。"""


class RepairTargetNotFoundError(RuntimeError):
    """未找到目标群映射或成员关联。"""


class RepairDependencyChangedError(RuntimeError):
    """预览后目标版本或依赖集合已变化，必须重新预览。"""


class RepairBlockedByGameError(RuntimeError):
    """群内存在 waiting/active 对局，禁止修复。"""


@dataclass(frozen=True, slots=True)
class _GroupRow:
    """目标群映射行及其行版本标记。"""

    app_id: str
    group_openid: str
    group_id: str
    marker: str


@dataclass(frozen=True, slots=True)
class _MemberRow:
    """目标成员关联行及其行版本标记。"""

    app_id: str
    group_openid: str
    member_openid: str
    member_qq: str
    character_name: str | None
    character_name_key: str | None
    marker: str


@dataclass(slots=True)
class _TokenEntry:
    """进程内一次性修复令牌。"""

    scope: RepairScope
    app_id: str
    group_openid: str
    member_openid: str | None
    operator_id: str
    version: str
    expected_count: int
    expires_at: datetime
    used: bool = False


def _update_fingerprint(digest: Any, value: object) -> None:
    """把单个指纹组成部分写入摘要（长度前缀，None 与字符串严格区分）。"""
    encoded = b"\x00" if value is None else str(value).encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def _row_marker(row: Mapping[Any, Any]) -> str:
    """行版本标记：包含每行的 ``updated_at`` / ``created_at`` 与 ``xmin``。

    改名后改回原值、删后重建同值等 ABA 场景都必须改变该标记，使旧令牌
    失效；行内容本身不参与此标记，由指纹的其余字段负责。
    """
    return (
        f"{row['updated_at']}|{row['created_at']}|{row['row_generation']}"
    )


def _member_row(row: Mapping[Any, Any]) -> _MemberRow:
    return _MemberRow(
        app_id=str(row["app_id"]),
        group_openid=str(row["group_openid"]),
        member_openid=str(row["member_openid"]),
        member_qq=str(row["member_qq"]),
        character_name=(
            str(row["character_name"]) if row["character_name"] is not None else None
        ),
        character_name_key=(
            str(row["character_name_key"])
            if row["character_name_key"] is not None
            else None
        ),
        marker=_row_marker(row),
    )


def _binding_version(
    group: _GroupRow,
    members: Sequence[_MemberRow],
    game_snapshot: _GameSnapshotView | None,
) -> str:
    """目标行（群行 + 受影响成员行）与当前游戏快照的规范化指纹。"""
    digest = hashlib.sha256()
    _update_fingerprint(digest, "group")
    _update_fingerprint(digest, group.app_id)
    _update_fingerprint(digest, group.group_openid)
    _update_fingerprint(digest, group.group_id)
    _update_fingerprint(digest, group.marker)
    for member in sorted(members, key=lambda record: record.member_openid):
        _update_fingerprint(digest, "member")
        _update_fingerprint(digest, member.app_id)
        _update_fingerprint(digest, member.group_openid)
        _update_fingerprint(digest, member.member_openid)
        _update_fingerprint(digest, member.member_qq)
        _update_fingerprint(digest, member.character_name)
        _update_fingerprint(digest, member.character_name_key)
        _update_fingerprint(digest, member.marker)
    if game_snapshot is not None:
        _update_fingerprint(digest, "game")
        _update_fingerprint(digest, game_snapshot.game_id)
        _update_fingerprint(digest, game_snapshot.lifecycle)
        _update_fingerprint(digest, game_snapshot.state_revision)
    return digest.hexdigest()


_service_registry: BindingRepairService | None = None


def set_binding_repair_service(service: BindingRepairService | None) -> None:
    """安装或移除全局修复服务（生命周期接管，重启即清除）。"""
    global _service_registry  # noqa: PLW0603
    _service_registry = service


def get_binding_repair_service() -> BindingRepairService | None:
    """读取全局修复服务；未初始化或已关闭时为 ``None``。"""
    return _service_registry


class BindingRepairService:
    """角色绑定受权修复服务。

    服务自持 ``AsyncSession``（``session_factory`` 产出），不借用共享引擎的
    生命周期；token 仅存进程内，实例重建即失效。``close()`` 立即生效且
    绝不等待在途任务，避免与群锁等待互相死锁。
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession],
        clock: Callable[[], datetime],
        game_state_reader: Callable[..., Awaitable[object | None]],
        manager: CharacterBindingManager | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._game_state_reader = game_state_reader
        self._manager = manager
        self._tokens: dict[str, _TokenEntry] = {}
        self._closed = False

    # ------------------------------------------------------------------ 内部

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError(_CLOSED)

    def _purge_expired_tokens(self) -> None:
        now = self._clock()
        self._tokens = {
            token: entry
            for token, entry in self._tokens.items()
            if entry.expires_at > now
        }

    async def _load_group_row(
        self,
        session: AsyncSession,
        *,
        app_id: str,
        group_openid: str,
    ) -> _GroupRow | None:
        row = (
            await session.execute(
                select(
                    _GROUPS.c.app_id,
                    _GROUPS.c.group_openid,
                    _GROUPS.c.group_id,
                    _GROUPS.c.updated_at,
                    _GROUPS.c.created_at,
                    _GROUP_GENERATION,
                ).where(
                    (_GROUPS.c.app_id == str(app_id))
                    & (_GROUPS.c.group_openid == str(group_openid))
                )
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        return _GroupRow(
            app_id=str(row["app_id"]),
            group_openid=str(row["group_openid"]),
            group_id=str(row["group_id"]),
            marker=_row_marker(row),
        )

    async def _load_member_rows(
        self,
        session: AsyncSession,
        *,
        app_id: str,
        group_openid: str,
    ) -> list[_MemberRow]:
        rows = (
            await session.execute(
                select(
                    _MEMBERS.c.app_id,
                    _MEMBERS.c.group_openid,
                    _MEMBERS.c.member_openid,
                    _MEMBERS.c.member_qq,
                    _MEMBERS.c.character_name,
                    _MEMBERS.c.character_name_key,
                    _MEMBERS.c.updated_at,
                    _MEMBERS.c.created_at,
                    _MEMBER_GENERATION,
                )
                .where(
                    (_MEMBERS.c.app_id == str(app_id))
                    & (_MEMBERS.c.group_openid == str(group_openid))
                )
                .order_by(_MEMBERS.c.member_openid)
            )
        ).mappings().all()
        return [_member_row(row) for row in rows]

    async def _load_member_row(
        self,
        session: AsyncSession,
        *,
        app_id: str,
        group_openid: str,
        member_openid: str,
    ) -> _MemberRow | None:
        row = (
            await session.execute(
                select(
                    _MEMBERS.c.app_id,
                    _MEMBERS.c.group_openid,
                    _MEMBERS.c.member_openid,
                    _MEMBERS.c.member_qq,
                    _MEMBERS.c.character_name,
                    _MEMBERS.c.character_name_key,
                    _MEMBERS.c.updated_at,
                    _MEMBERS.c.created_at,
                    _MEMBER_GENERATION,
                ).where(
                    (_MEMBERS.c.app_id == str(app_id))
                    & (_MEMBERS.c.group_openid == str(group_openid))
                    & (_MEMBERS.c.member_openid == str(member_openid))
                )
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        return _member_row(row)

    async def _load_game_snapshot(
        self,
        session: AsyncSession,
        *,
        app_id: str,
        group_openid: str,
        for_update: bool,
    ) -> _GameSnapshotView | None:
        """读取对局快照；任何读取失败都 fail closed（不可用 ≠ 无对局）。"""
        try:
            snapshot = await self._game_state_reader(
                session,
                app_id=app_id,
                group_openid=group_openid,
                for_update=for_update,
            )
        except Exception as error:
            logger.error(
                "[BindingRepair] 对局状态读取失败: error_type={}",
                type(error).__name__,
            )
            raise BindingPersistenceError(_STORAGE_UNAVAILABLE) from error
        return cast("_GameSnapshotView | None", snapshot)

    # ---------------------------------------------------------------- 诊断

    async def diagnose(
        self,
        *,
        app_id: str,
        group_openid: str,
    ) -> BindingDiagnosis:
        """只读诊断：直读 PostgreSQL，绝不使用 manager 缓存快照。"""
        self._ensure_open()
        session = self._session_factory()
        try:
            try:
                group = await self._load_group_row(
                    session,
                    app_id=app_id,
                    group_openid=group_openid,
                )
                if group is None:
                    raise RepairTargetNotFoundError(_NOT_FOUND_GROUP)
                members = await self._load_member_rows(
                    session,
                    app_id=app_id,
                    group_openid=group_openid,
                )
                snapshot = await self._load_game_snapshot(
                    session,
                    app_id=app_id,
                    group_openid=group_openid,
                    for_update=False,
                )
            except RepairTargetNotFoundError:
                raise
            except BindingPersistenceError:
                raise
            except (
                DBAPIError,
                SQLAlchemyError,
                ConnectionError,
                OSError,
                TimeoutError,
            ) as error:
                logger.error(
                    "[BindingRepair] 诊断读取失败: error_type={}",
                    type(error).__name__,
                )
                raise BindingPersistenceError(_STORAGE_UNAVAILABLE) from error
        finally:
            await session.close()

        return BindingDiagnosis(
            app_id=str(app_id),
            group_openid=str(group_openid),
            group_id=group.group_id,
            members=tuple(
                MemberBindingView(
                    app_id=member.app_id,
                    group_openid=member.group_openid,
                    member_openid=member.member_openid,
                    member_qq=member.member_qq,
                    character_name=member.character_name,
                )
                for member in members
            ),
            game_present=snapshot is not None,
            game_lifecycle=snapshot.lifecycle if snapshot is not None else None,
        )

    # ---------------------------------------------------------------- 预览

    async def preview(
        self,
        *,
        app_id: str,
        group_openid: str,
        operator_id: str,
        member_openid: str | None = None,
        reason: str,
    ) -> RepairPreview:
        """计算影响范围并签发一次性确认令牌；对局存在时预览即拒绝。"""
        del reason
        self._ensure_open()
        self._purge_expired_tokens()
        session = self._session_factory()
        try:
            try:
                async with session.begin():
                    await lock_group_scope(
                        session,
                        app_id=str(app_id),
                        group_openid=str(group_openid),
                    )
                    # 群锁等待可能跨过 close()：签发令牌前必须复核已关闭状态。
                    self._ensure_open()
                    group = await self._load_group_row(
                        session,
                        app_id=app_id,
                        group_openid=group_openid,
                    )
                    if group is None:
                        raise RepairTargetNotFoundError(_NOT_FOUND_GROUP)
                    if member_openid is None:
                        scope: RepairScope = "group"
                        members = await self._load_member_rows(
                            session,
                            app_id=app_id,
                            group_openid=group_openid,
                        )
                    else:
                        scope = "member"
                        member = await self._load_member_row(
                            session,
                            app_id=app_id,
                            group_openid=group_openid,
                            member_openid=member_openid,
                        )
                        if member is None:
                            raise RepairTargetNotFoundError(_NOT_FOUND_MEMBER)
                        members = [member]
                    snapshot = await self._load_game_snapshot(
                        session,
                        app_id=app_id,
                        group_openid=group_openid,
                        for_update=True,
                    )
                    if snapshot is not None:
                        raise RepairBlockedByGameError(_BLOCKED_BY_GAME)
                    version = _binding_version(group, members, snapshot)
                    affected_count = len(members)
                    cleared_names = tuple(
                        member.character_name for member in members
                    )
            except RepairTargetNotFoundError:
                raise
            except RepairBlockedByGameError:
                raise
            except BindingPersistenceError:
                raise
            except (
                DBAPIError,
                SQLAlchemyError,
                ConnectionError,
                OSError,
                TimeoutError,
            ) as error:
                logger.error(
                    "[BindingRepair] 修复预览存储失败: error_type={}",
                    type(error).__name__,
                )
                raise BindingPersistenceError(_STORAGE_UNAVAILABLE) from error
        finally:
            await session.close()

        # 会话关闭是 await 点：close() 可能在此期间完成，签发前再次复核。
        self._ensure_open()
        token = secrets.token_urlsafe(24)
        entry = _TokenEntry(
            scope=scope,
            app_id=str(app_id),
            group_openid=str(group_openid),
            member_openid=member_openid,
            operator_id=str(operator_id),
            version=version,
            expected_count=affected_count,
            expires_at=self._clock() + TOKEN_TTL,
        )
        self._tokens[token] = entry
        return RepairPreview(
            token=token,
            scope=scope,
            app_id=str(app_id),
            group_openid=str(group_openid),
            member_openid=member_openid,
            affected_count=affected_count,
            cleared_names=cleared_names,
            version=version,
            expires_at=entry.expires_at,
        )

    # ---------------------------------------------------------------- 确认

    async def confirm(
        self,
        *,
        app_id: str,
        group_openid: str,
        token: str,
        operator_id: str,
        request_id: str,
        reason: str,
    ) -> RepairConfirmResult:
        """确认清除：先原子消耗令牌，持锁后复核槽位、依赖与 TTL。"""
        del request_id, reason
        self._ensure_open()
        self._purge_expired_tokens()
        entry = self._tokens.get(token)
        if entry is None or entry.used:
            raise RepairTokenError(_TOKEN_INVALID)
        if self._clock() >= entry.expires_at:
            raise RepairTokenError(_TOKEN_INVALID)
        if entry.operator_id != str(operator_id):
            raise RepairTokenError(_TOKEN_INVALID)
        if entry.app_id != str(app_id) or entry.group_openid != str(group_openid):
            raise RepairTokenError(_TOKEN_INVALID)
        if entry.scope == "member" and entry.member_openid is None:
            raise RepairTokenError(_TOKEN_INVALID)
        # 每次确认尝试开始时原子消耗：成功、依赖变化或对局阻断后均不可再用。
        entry.used = True

        session = self._session_factory()
        try:
            try:
                async with session.begin():
                    await lock_group_scope(
                        session,
                        app_id=str(app_id),
                        group_openid=str(group_openid),
                    )
                    # 群锁等待可能跨过 close() 或绝对 TTL：写库前必须复核。
                    self._ensure_open()
                    if self._clock() >= entry.expires_at:
                        raise RepairTokenError(_TOKEN_INVALID)
                    snapshot = await self._load_game_snapshot(
                        session,
                        app_id=app_id,
                        group_openid=group_openid,
                        for_update=True,
                    )
                    if snapshot is not None:
                        raise RepairBlockedByGameError(_BLOCKED_BY_GAME)
                    group = await self._load_group_row(
                        session,
                        app_id=app_id,
                        group_openid=group_openid,
                    )
                    if group is None:
                        raise RepairDependencyChangedError(_DEPENDENCY_CHANGED)
                    if entry.scope == "member":
                        member_openid = entry.member_openid
                        if member_openid is None:
                            raise RepairTokenError(_TOKEN_INVALID)
                        member = await self._load_member_row(
                            session,
                            app_id=app_id,
                            group_openid=group_openid,
                            member_openid=member_openid,
                        )
                        if member is None:
                            raise RepairDependencyChangedError(_DEPENDENCY_CHANGED)
                        members = [member]
                    else:
                        members = await self._load_member_rows(
                            session,
                            app_id=app_id,
                            group_openid=group_openid,
                        )
                    version = _binding_version(group, members, snapshot)
                    if version != entry.version:
                        raise RepairDependencyChangedError(_DEPENDENCY_CHANGED)
                    cleared_names = tuple(
                        member.character_name for member in members
                    )
                    # 所有等待结束后、写库前再次复核：close 或 TTL 跨过即拒绝。
                    self._ensure_open()
                    if self._clock() >= entry.expires_at:
                        raise RepairTokenError(_TOKEN_INVALID)
                    if entry.scope == "member":
                        await session.execute(
                            delete(_MEMBERS).where(
                                (_MEMBERS.c.app_id == str(app_id))
                                & (_MEMBERS.c.group_openid == str(group_openid))
                                & (
                                    _MEMBERS.c.member_openid
                                    == entry.member_openid
                                )
                            )
                        )
                    else:
                        await session.execute(
                            delete(_GROUPS).where(
                                (_GROUPS.c.app_id == str(app_id))
                                & (_GROUPS.c.group_openid == str(group_openid))
                            )
                        )
            except RepairTokenError:
                raise
            except RepairBlockedByGameError:
                raise
            except RepairDependencyChangedError:
                raise
            except BindingPersistenceError:
                raise
            except (
                DBAPIError,
                SQLAlchemyError,
                ConnectionError,
                OSError,
                TimeoutError,
            ) as error:
                logger.error(
                    "[BindingRepair] 修复确认存储失败: error_type={}",
                    type(error).__name__,
                )
                raise BindingPersistenceError(_STORAGE_UNAVAILABLE) from error
        finally:
            await session.close()

        # 仅提交成功后刷新正式绑定快照，保证双协议查询一致。
        if self._manager is not None:
            try:
                await self._manager.refresh_snapshot()
            except Exception as error:
                logger.error(
                    "[BindingRepair] 修复确认后刷新快照失败: error_type={}",
                    type(error).__name__,
                )
                raise BindingPersistenceError("绑定修复保存结果无法确认") from error

        return RepairConfirmResult(
            scope=entry.scope,
            app_id=str(app_id),
            group_openid=str(group_openid),
            member_openid=entry.member_openid,
            cleared_count=len(cleared_names),
            cleared_names=cleared_names,
            version=entry.version,
            expected_count=entry.expected_count,
        )

    # ---------------------------------------------------------------- 生命周期

    async def close(self) -> None:
        """立即关闭服务：拒绝一切新操作并使全部令牌失效。

        绝不等待在途任务（否则会与群锁等待互相死锁）：已阻塞在群锁上的
        confirm/preview 在锁释放后经 ``_ensure_open`` 复核并失败。
        """
        self._closed = True
        self._tokens.clear()


__all__ = [
    "BindingDiagnosis",
    "BindingRepairService",
    "MemberBindingView",
    "RepairBlockedByGameError",
    "RepairConfirmResult",
    "RepairDependencyChangedError",
    "RepairPreview",
    "RepairScope",
    "RepairTargetNotFoundError",
    "RepairTokenError",
    "get_binding_repair_service",
    "set_binding_repair_service",
]
