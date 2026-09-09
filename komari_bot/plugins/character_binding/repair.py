# ruff: noqa: TRY301

"""TSK-280 角色绑定受权运维修复服务。

统一 REST 控制面的业务核心：诊断只读直读 PostgreSQL；预览计算影响范围并
签发绑定操作者与目标版本的短期单次令牌；确认在共享组锁内复核槽位、依赖
集合与令牌 TTL 后原子清除错误关联。绝不强制终局、改写历史结果/胜场、
修改准入或替用户重绑。

对局状态经轮盘插件顶层公开 seam 读取（延迟导入，模块名拼接避免与
轮盘命令服务的 ``require("character_binding")`` 形成静态循环）；本模块不
反向 import 任何管理插件内部。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol

from nonebot import logger
from sqlalchemy import delete, select
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from komari_bot.db.group_transaction_locks import lock_group_scope

from .manager import (
    BindingPersistenceError,
    CharacterBindingManager,
    GroupBindingRecord,
)
from .orm_models import CharacterBindingGroupRow, CharacterBindingMemberRow
from .transaction import BindingTransaction, GroupBindingGroup

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

TOKEN_TTL = timedelta(minutes=10)

RepairScope = Literal["member", "group"]

_GROUPS = CharacterBindingGroupRow.__table__
_MEMBERS = CharacterBindingMemberRow.__table__

_NOT_FOUND_GROUP = "未找到目标群映射"
_NOT_FOUND_MEMBER = "未找到目标成员关联"
_DEPENDENCY_CHANGED = "绑定状态已变化，请重新预览"
_BLOCKED_BY_GAME = "群内存在进行中的对局，无法修复"
_TOKEN_INVALID = "确认令牌无效、已过期或已使用"
_STORAGE_UNAVAILABLE = "绑定修复存储暂不可用"


class _GameSnapshotProtocol(Protocol):
    """轮盘游戏快照的最小结构面（延迟导入避免循环依赖）。"""

    game_id: str
    lifecycle: str
    state_revision: int


class _RouletteCommandRequest(Protocol):
    """轮盘命令请求的最小结构面（延迟导入避免循环依赖）。"""


class _RouletteCommandReceipt(Protocol):
    """轮盘命令回执的最小结构面。"""

    result_code: str


def _roulette_module() -> Any:
    """延迟加载轮盘插件顶层包；模块名拼接以免静态 import 形成环。"""
    import importlib

    return importlib.import_module("komari_bot.plugins." + "komari_" + "roulette")


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
    """确认清除的实际结果。"""

    scope: RepairScope
    app_id: str
    group_openid: str
    member_openid: str | None
    cleared_count: int
    cleared_names: tuple[str | None, ...]


class RepairTokenError(RuntimeError):
    """确认令牌无效、已过期、已使用或与操作者/目标不符。"""


class RepairTargetNotFoundError(RuntimeError):
    """未找到目标群映射或成员关联。"""


class RepairDependencyChangedError(RuntimeError):
    """预览后目标版本或依赖集合已变化，必须重新预览。"""


class RepairBlockedByGameError(RuntimeError):
    """群内存在 waiting/active 对局，禁止修复。"""


@dataclass(slots=True)
class _TokenEntry:
    """进程内一次性修复令牌。"""

    scope: RepairScope
    app_id: str
    group_openid: str
    member_openid: str | None
    operator_id: str
    version: str
    expires_at: datetime
    used: bool = False


def _update_fingerprint(digest: Any, value: object) -> None:
    """把单个指纹组成部分写入摘要（长度前缀，None 与字符串严格区分）。"""
    encoded = b"\x00" if value is None else str(value).encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def _binding_version(
    group: GroupBindingGroup,
    members: Sequence[GroupBindingRecord],
    game_snapshot: _GameSnapshotProtocol | None,
) -> str:
    """目标行（群行 + 受影响成员行）与当前游戏快照的规范化指纹。"""
    digest = hashlib.sha256()
    _update_fingerprint(digest, "group")
    _update_fingerprint(digest, group.app_id)
    _update_fingerprint(digest, group.group_openid)
    _update_fingerprint(digest, group.group_id)
    for member in sorted(members, key=lambda record: record.member_openid):
        _update_fingerprint(digest, "member")
        _update_fingerprint(digest, member.app_id)
        _update_fingerprint(digest, member.group_openid)
        _update_fingerprint(digest, member.member_openid)
        _update_fingerprint(digest, member.member_qq)
        _update_fingerprint(digest, member.character_name)
        _update_fingerprint(digest, member.character_name_key)
    if game_snapshot is not None:
        _update_fingerprint(digest, "game")
        _update_fingerprint(digest, game_snapshot.game_id)
        _update_fingerprint(digest, game_snapshot.lifecycle)
        _update_fingerprint(digest, game_snapshot.state_revision)
    return digest.hexdigest()


def _member_record(row: Mapping[Any, Any]) -> GroupBindingRecord:
    return GroupBindingRecord(
        app_id=str(row["app_id"]),
        group_id=str(row["binding_group_id"]),
        group_openid=str(row["group_openid"]),
        member_qq=str(row["member_qq"]),
        member_openid=str(row["member_openid"]),
        character_name=(
            str(row["character_name"]) if row["character_name"] is not None else None
        ),
        character_name_key=(
            str(row["character_name_key"])
            if row["character_name_key"] is not None
            else None
        ),
    )


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
    生命周期；token 仅存进程内，实例重建即失效。
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession],
        clock: Callable[[], datetime],
        manager: CharacterBindingManager | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._manager = manager
        self._tokens: dict[str, _TokenEntry] = {}

    # ------------------------------------------------------------------ 内部

    def _purge_expired_tokens(self) -> None:
        now = self._clock()
        self._tokens = {
            token: entry
            for token, entry in self._tokens.items()
            if entry.expires_at > now
        }

    async def _load_member_records(
        self,
        session: AsyncSession,
        *,
        app_id: str,
        group_openid: str,
    ) -> list[GroupBindingRecord]:
        rows = await session.execute(
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
                _MEMBERS.c.app_id == str(app_id),
                _MEMBERS.c.group_openid == str(group_openid),
            )
            .order_by(_MEMBERS.c.member_openid)
        )
        return [_member_record(row) for row in rows.mappings().all()]

    @staticmethod
    def _member_views(
        records: Sequence[GroupBindingRecord],
    ) -> tuple[MemberBindingView, ...]:
        return tuple(
            MemberBindingView(
                app_id=record.app_id,
                group_openid=record.group_openid,
                member_openid=record.member_openid,
                member_qq=record.member_qq,
                character_name=record.character_name,
            )
            for record in records
        )

    @staticmethod
    def _cleared_names(
        records: Sequence[GroupBindingRecord],
    ) -> tuple[str | None, ...]:
        return tuple(record.character_name for record in records)

    # ---------------------------------------------------------------- 诊断

    async def diagnose(
        self,
        *,
        app_id: str,
        group_openid: str,
    ) -> BindingDiagnosis:
        """只读诊断：直读 PostgreSQL，绝不使用 manager 缓存快照。"""
        roulette = _roulette_module()
        session = self._session_factory()
        try:
            try:
                transaction = BindingTransaction(session)
                group = await transaction.resolve_group(
                    app_id=app_id,
                    group_openid=group_openid,
                    lock=False,
                )
                if group is None:
                    raise RepairTargetNotFoundError(_NOT_FOUND_GROUP)
                members = await self._load_member_records(
                    session,
                    app_id=app_id,
                    group_openid=group_openid,
                )
                snapshot = await roulette.PostgresRouletteStorage(session).load_current(
                    roulette.GroupRef(app_id=app_id, group_openid=group_openid)
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
                roulette.StorageUnavailableError,
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
            members=self._member_views(members),
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
        roulette = _roulette_module()
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
                    transaction = BindingTransaction(session)
                    group = await transaction.resolve_group(
                        app_id=app_id,
                        group_openid=group_openid,
                        lock=False,
                    )
                    if group is None:
                        raise RepairTargetNotFoundError(_NOT_FOUND_GROUP)
                    if member_openid is None:
                        scope: RepairScope = "group"
                        members = await self._load_member_records(
                            session,
                            app_id=app_id,
                            group_openid=group_openid,
                        )
                    else:
                        scope = "member"
                        member = await transaction.resolve_member(
                            app_id=app_id,
                            group_openid=group_openid,
                            member_openid=member_openid,
                            lock=False,
                        )
                        if member is None:
                            raise RepairTargetNotFoundError(_NOT_FOUND_MEMBER)
                        members = [member]
                    snapshot = await roulette.PostgresRouletteStorage(
                        session
                    ).load_current(
                        roulette.GroupRef(app_id=app_id, group_openid=group_openid),
                        for_update=True,
                    )
                    if snapshot is not None:
                        raise RepairBlockedByGameError(_BLOCKED_BY_GAME)
                    version = _binding_version(group, members, snapshot)
                    affected_count = len(members)
                    cleared_names = self._cleared_names(members)
            except RepairTargetNotFoundError:
                raise
            except RepairBlockedByGameError:
                raise
            except BindingPersistenceError:
                raise
            except roulette.AggregateCorruptError as error:
                raise RepairBlockedByGameError(_BLOCKED_BY_GAME) from error
            except (
                DBAPIError,
                SQLAlchemyError,
                ConnectionError,
                OSError,
                TimeoutError,
                roulette.StorageUnavailableError,
            ) as error:
                logger.error(
                    "[BindingRepair] 修复预览存储失败: error_type={}",
                    type(error).__name__,
                )
                raise BindingPersistenceError(_STORAGE_UNAVAILABLE) from error
        finally:
            await session.close()

        token = secrets.token_urlsafe(24)
        self._tokens[token] = _TokenEntry(
            scope=scope,
            app_id=str(app_id),
            group_openid=str(group_openid),
            member_openid=member_openid,
            operator_id=str(operator_id),
            version=version,
            expires_at=self._clock() + TOKEN_TTL,
        )
        return RepairPreview(
            token=token,
            scope=scope,
            app_id=str(app_id),
            group_openid=str(group_openid),
            member_openid=member_openid,
            affected_count=affected_count,
            cleared_names=cleared_names,
            version=version,
            expires_at=self._tokens[token].expires_at,
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
        roulette = _roulette_module()
        self._purge_expired_tokens()
        entry = self._tokens.get(token)
        if entry is None:
            raise RepairTokenError(_TOKEN_INVALID)
        if entry.used:
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
                    # 锁等待可能跨过绝对 TTL：提交前必须复核，过期则拒绝且不写库。
                    if self._clock() >= entry.expires_at:
                        raise RepairTokenError(_TOKEN_INVALID)
                    snapshot = await roulette.PostgresRouletteStorage(
                        session
                    ).load_current(
                        roulette.GroupRef(app_id=app_id, group_openid=group_openid),
                        for_update=True,
                    )
                    if snapshot is not None:
                        raise RepairBlockedByGameError(_BLOCKED_BY_GAME)
                    transaction = BindingTransaction(session)
                    group = await transaction.resolve_group(
                        app_id=app_id,
                        group_openid=group_openid,
                        lock=False,
                    )
                    if group is None:
                        raise RepairDependencyChangedError(_DEPENDENCY_CHANGED)
                    if entry.scope == "member":
                        member_openid = entry.member_openid
                        if member_openid is None:
                            raise RepairTokenError(_TOKEN_INVALID)
                        member = await transaction.resolve_member(
                            app_id=app_id,
                            group_openid=group_openid,
                            member_openid=member_openid,
                            lock=False,
                        )
                        if member is None:
                            raise RepairDependencyChangedError(_DEPENDENCY_CHANGED)
                        members = [member]
                    else:
                        members = await self._load_member_records(
                            session,
                            app_id=app_id,
                            group_openid=group_openid,
                        )
                    version = _binding_version(group, members, snapshot)
                    if version != entry.version:
                        raise RepairDependencyChangedError(_DEPENDENCY_CHANGED)
                    cleared_names = self._cleared_names(members)
                    if entry.scope == "member":
                        await session.execute(
                            delete(_MEMBERS).where(
                                _MEMBERS.c.app_id == str(app_id),
                                _MEMBERS.c.group_openid == str(group_openid),
                                _MEMBERS.c.member_openid == entry.member_openid,
                            )
                        )
                    else:
                        await session.execute(
                            delete(_GROUPS).where(
                                _GROUPS.c.app_id == str(app_id),
                                _GROUPS.c.group_openid == str(group_openid),
                            )
                        )
            except RepairTokenError:
                raise
            except RepairBlockedByGameError:
                raise
            except RepairDependencyChangedError:
                raise
            except RepairTargetNotFoundError:
                raise
            except BindingPersistenceError:
                raise
            except roulette.AggregateCorruptError as error:
                raise RepairBlockedByGameError(_BLOCKED_BY_GAME) from error
            except (
                DBAPIError,
                SQLAlchemyError,
                ConnectionError,
                OSError,
                TimeoutError,
                roulette.StorageUnavailableError,
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
        )

    # ---------------------------------------------------------------- 并发验证面

    async def execute_group_command(
        self,
        request: _RouletteCommandRequest,
        *,
        observation: Any = None,
    ) -> _RouletteCommandReceipt:
        """把轮盘命令转发给轮盘命令服务（共享群锁的并发验证面）。

        仅用于与轮盘开局等写路径共用 TSK-276 组锁的串行化验证；回复投影
        使用空安全占位，业务侧不会经此入口发送任何通知。
        """
        roulette = _roulette_module()
        command_service = roulette.RouletteCommandService(
            session_factory=self._session_factory,
            reply_projector=lambda _context: roulette.ReplyProjection(
                body="",
                metadata={},
            ),
        )
        if observation is None:
            return await command_service.execute_group_command(request)
        return await command_service.execute_group_command(
            request, observation=observation
        )


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
