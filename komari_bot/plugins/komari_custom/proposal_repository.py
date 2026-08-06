"""komari_custom 提案 PostgreSQL 访问层（SQLModel + nonebot-plugin-orm AsyncSession）。

连接池与 engine 生命周期由 nonebot-plugin-orm 托管（本模块不再依赖
``komari_bot.common.postgres`` 自研池）；表结构由 Alembic 迁移统一管理，
启动期与懒路径均无任何 DDL。

SQLModel 字段在 Pyright 下被推断为 Python 值类型而非列表达式，因此列访问
统一走 ``模型.__table__.c``（与 user_ban / komari_decision 仓储同一约定）。
间隔运算使用 ``CAST(:param AS INTERVAL)``（``_interval`` 辅助）；UPDATE ...
RETURNING 无顺序保证，需要顺序契约的查询由调用方客户端稳定重排。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import (
    ColumnElement,
    Interval,
    String,
    and_,
    delete,
    func,
    null,
    or_,
    select,
    update,
)
from sqlalchemy import cast as sqlalchemy_cast
from sqlalchemy.dialects.postgresql import insert as postgres_insert

from komari_bot.common.sql_like_utils import escape_like_pattern

from .models import Proposal, ProposalStatus
from .orm_models import ProposalRow

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession

_P = ProposalRow.__table__


def _interval(seconds: float) -> Any:
    """把秒数构造成 ``CAST(%s AS INTERVAL)`` 表达式（PG 侧原生 interval）。"""
    return sqlalchemy_cast(timedelta(seconds=seconds), Interval)


def _active_status_condition() -> ColumnElement[bool]:
    """列表可见性谓词：已通过/采纳中，或仍在投票期内。"""
    return or_(
        _P.c.status.in_(["approved", "approving"]),
        and_(
            _P.c.status == "voting",
            or_(
                _P.c.expired_at.is_(None),
                _P.c.expired_at > func.now(),
            ),
        ),
    )


def _open_session() -> "AsyncSession":
    """打开绑定 nonebot-plugin-orm 共享引擎的会话。"""
    from nonebot_plugin_orm import get_session

    return get_session(expire_on_commit=False)


class ProposalRepository:
    """知识库提案 DAO。"""

    def __init__(self) -> None:
        self._initialize_lock = asyncio.Lock()
        self._ready = False

    async def initialize(self) -> None:
        """单飞确认 ORM 存储可连接；表结构由 Alembic 迁移统一管理。"""
        async with self._initialize_lock:
            if self._ready:
                return
            session = _open_session()
            try:
                await session.execute(select(1))
            finally:
                await session.close()
            self._ready = True

    async def close(self) -> None:
        """重置就绪状态；engine 生命周期由 nonebot-plugin-orm 托管。"""
        async with self._initialize_lock:
            self._ready = False

    def _require_ready(self) -> None:
        if not self._ready:
            msg = "komari_custom 数据库尚未初始化"
            raise RuntimeError(msg)

    @staticmethod
    def _row_to_proposal(row: Any) -> Proposal:
        return Proposal(
            id=int(row.id),
            group_id=int(row.group_id),
            proposer_id=int(row.proposer_id),
            proposer_name=row.proposer_name,
            title=str(row.title),
            content=str(row.content),
            status=cast("ProposalStatus", row.status),
            publication_key=str(row.publication_key),
            publication_token=row.publication_token,
            publication_started_at=row.publication_started_at,
            publication_attempts=int(row.publication_attempts),
            publication_error_code=row.publication_error_code,
            vote_message_id=(
                int(row.vote_message_id)
                if row.vote_message_id is not None
                else None
            ),
            vote_count=int(row.vote_count),
            required_votes=int(row.required_votes),
            voted_users=[str(item) for item in row.voted_users],
            created_at=row.created_at,
            updated_at=row.updated_at,
            approved_at=row.approved_at,
            knowledge_id=(
                int(row.knowledge_id) if row.knowledge_id is not None else None
            ),
            expired_at=row.expired_at,
            approval_token=row.approval_token,
            approval_started_at=row.approval_started_at,
        )

    async def cleanup_expired(self) -> int:
        """删除过期投票和超过会话恢复窗口的遗留发布记录。"""
        self._require_ready()
        statement = delete(_P).where(
            or_(
                and_(
                    _P.c.status == "voting",
                    _P.c.expired_at.is_not(None),
                    _P.c.expired_at < func.now(),
                ),
                and_(
                    _P.c.status.in_(["publishing", "failed"]),
                    _P.c.updated_at < func.now() - _interval(7 * 24 * 3600),
                ),
            )
        )
        session = _open_session()
        try:
            async with session.begin():
                result = cast(
                    "CursorResult[Any]",
                    await session.execute(statement),
                )
        finally:
            await session.close()
        return int(result.rowcount or 0)

    async def count_active_by_user(self, group_id: int, proposer_id: int) -> int:
        """统计用户在本群仍在投票期内的提案数量。"""
        self._require_ready()
        statement = (
            select(func.count())
            .select_from(_P)
            .where(
                _P.c.group_id == group_id,
                _P.c.proposer_id == proposer_id,
                _P.c.status.in_(["publishing", "voting", "approving"]),
                or_(
                    _P.c.expired_at.is_(None),
                    _P.c.expired_at > func.now(),
                ),
            )
        )
        session = _open_session()
        try:
            value = (await session.execute(statement)).scalar_one()
        finally:
            await session.close()
        return int(value)

    async def claim_publication(
        self,
        *,
        publication_key: str,
        publication_token: str,
        group_id: int,
        proposer_id: int,
        proposer_name: str | None,
        title: str,
        content: str,
        required_votes: int,
        expire_hours: int,
        lease_seconds: int,
    ) -> Proposal | None:
        """按幂等键创建或认领待发布提案，并拒绝活跃的并发发布。"""
        del lease_seconds  # 租约语义由发布服务层与数据库事务共同保证
        self._require_ready()
        expired_at = datetime.now().astimezone() + timedelta(hours=expire_hours)
        statement = (
            postgres_insert(ProposalRow)
            .values(
                publication_key=publication_key,
                publication_token=publication_token,
                publication_started_at=func.now(),
                publication_attempts=1,
                group_id=group_id,
                proposer_id=proposer_id,
                proposer_name=proposer_name,
                title=title,
                content=content,
                status="publishing",
                required_votes=required_votes,
                expired_at=expired_at,
            )
            .on_conflict_do_update(
                index_elements=["publication_key"],
                set_={
                    "publication_token": publication_token,
                    "publication_started_at": func.now(),
                    "publication_attempts": _P.c.publication_attempts + 1,
                    "publication_error_code": null(),
                    "vote_message_id": null(),
                    "group_id": group_id,
                    "proposer_id": proposer_id,
                    "proposer_name": proposer_name,
                    "title": title,
                    "content": content,
                    "status": "publishing",
                    "required_votes": required_votes,
                    "expired_at": expired_at,
                    "updated_at": func.now(),
                },
                where=and_(
                    _P.c.status == "failed",
                    _P.c.publication_error_code.in_(
                        ["send_rejected", "send_failed"]
                    ),
                ),
            )
            .returning(ProposalRow)
        )
        session = _open_session()
        try:
            async with session.begin():
                row = (await session.execute(statement)).scalars().one_or_none()
        finally:
            await session.close()
        return self._row_to_proposal(row) if row is not None else None

    async def get_by_publication_key(self, publication_key: str) -> Proposal | None:
        """按发布幂等键读取提案。"""
        self._require_ready()
        session = _open_session()
        try:
            row = (
                await session.execute(
                    select(ProposalRow).where(
                        _P.c.publication_key == publication_key
                    )
                )
            ).scalar_one_or_none()
        finally:
            await session.close()
        return self._row_to_proposal(row) if row is not None else None

    async def complete_publication(
        self,
        proposal_id: int,
        message_id: int,
        publication_token: str,
    ) -> Proposal | None:
        """由当前发布认领者原子回填消息 ID 并进入投票态。"""
        self._require_ready()
        statement = (
            update(ProposalRow)
            .where(
                _P.c.id == proposal_id,
                _P.c.status == "publishing",
                _P.c.publication_token == publication_token,
                or_(
                    _P.c.vote_message_id.is_(None),
                    _P.c.vote_message_id == message_id,
                ),
            )
            .values(
                status="voting",
                vote_message_id=message_id,
                publication_token=None,
                publication_error_code=None,
                updated_at=func.now(),
            )
            .returning(ProposalRow)
        )
        session = _open_session()
        try:
            async with session.begin():
                row = (await session.execute(statement)).scalars().one_or_none()
        finally:
            await session.close()
        return self._row_to_proposal(row) if row is not None else None

    async def recover_publication(
        self,
        publication_key: str,
        message_id: int,
    ) -> Proposal | None:
        """用编辑会话已记录的消息 ID 恢复中断的发布提交。"""
        self._require_ready()
        statement = (
            update(ProposalRow)
            .where(
                _P.c.publication_key == publication_key,
                _P.c.status.in_(["publishing", "failed"]),
                or_(
                    _P.c.vote_message_id.is_(None),
                    _P.c.vote_message_id == message_id,
                ),
            )
            .values(
                status="voting",
                vote_message_id=message_id,
                publication_token=None,
                publication_error_code=None,
                updated_at=func.now(),
            )
            .returning(ProposalRow)
        )
        session = _open_session()
        try:
            async with session.begin():
                row = (await session.execute(statement)).scalars().one_or_none()
        finally:
            await session.close()
        return self._row_to_proposal(row) if row is not None else None

    async def mark_publication_failed(
        self,
        proposal_id: int,
        publication_token: str,
        error_code: str,
    ) -> Proposal | None:
        """由当前认领者把发送失败记录为可重试状态。"""
        self._require_ready()
        statement = (
            update(ProposalRow)
            .where(
                _P.c.id == proposal_id,
                _P.c.status == "publishing",
                _P.c.publication_token == publication_token,
            )
            .values(
                status="failed",
                publication_token=None,
                publication_error_code=error_code,
                updated_at=func.now(),
            )
            .returning(ProposalRow)
        )
        session = _open_session()
        try:
            async with session.begin():
                row = (await session.execute(statement)).scalars().one_or_none()
        finally:
            await session.close()
        return self._row_to_proposal(row) if row is not None else None

    async def find_by_vote_message_id(self, message_id: int) -> Proposal | None:
        """通过投票消息 ID 查找提案。"""
        self._require_ready()
        session = _open_session()
        try:
            row = (
                await session.execute(
                    select(ProposalRow).where(
                        _P.c.vote_message_id == message_id
                    )
                )
            ).scalar_one_or_none()
        finally:
            await session.close()
        return self._row_to_proposal(row) if row is not None else None

    async def get_by_id(
        self, proposal_id: int, group_id: int | None = None
    ) -> Proposal | None:
        """按 ID 获取提案。"""
        self._require_ready()
        statement = select(ProposalRow).where(_P.c.id == proposal_id)
        if group_id is not None:
            statement = statement.where(_P.c.group_id == group_id)
        session = _open_session()
        try:
            row = (await session.execute(statement)).scalars().one_or_none()
        finally:
            await session.close()
        return self._row_to_proposal(row) if row is not None else None

    async def list_proposals(
        self,
        *,
        group_id: int,
        limit: int,
        offset: int,
    ) -> tuple[list[Proposal], int]:
        """分页列出本群有效提案。"""
        self._require_ready()
        where = and_(_P.c.group_id == group_id, _active_status_condition())
        session = _open_session()
        try:
            rows = (
                await session.execute(
                    select(ProposalRow)
                    .where(where)
                    .order_by(_P.c.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).scalars().all()
            total = (
                await session.execute(
                    select(func.count()).select_from(_P).where(where)
                )
            ).scalar_one()
        finally:
            await session.close()
        return (
            [self._row_to_proposal(row) for row in rows],
            int(total),
        )

    async def find_in_group_by_index_or_keyword(
        self,
        *,
        group_id: int,
        selector: str,
        limit_for_index: int,
    ) -> Proposal | None:
        """按列表序号或标题关键词查找提案。"""
        if selector.isdigit():
            index = int(selector)
            if index < 1:
                return None
            proposals, total = await self.list_proposals(
                group_id=group_id,
                limit=limit_for_index,
                offset=0,
            )
            if index <= total and index <= len(proposals):
                return proposals[index - 1]
            return None

        self._require_ready()
        keyword_pattern = f"%{escape_like_pattern(selector)}%"
        session = _open_session()
        try:
            row = (
                await session.execute(
                    select(ProposalRow)
                    .where(
                        _P.c.group_id == group_id,
                        _P.c.title.ilike(keyword_pattern, escape="\\"),
                        _active_status_condition(),
                    )
                    .order_by(_P.c.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        finally:
            await session.close()
        return self._row_to_proposal(row) if row is not None else None

    async def add_vote(self, proposal_id: int, user_id: str) -> Proposal | None:
        """为提案添加一个去重后的有效投票。"""
        self._require_ready()
        statement = (
            update(ProposalRow)
            .where(
                _P.c.id == proposal_id,
                _P.c.status == "voting",
                sqlalchemy_cast(_P.c.proposer_id, String) != user_id,
                ~_P.c.voted_users.any(user_id),
                or_(
                    _P.c.expired_at.is_(None),
                    _P.c.expired_at > func.now(),
                ),
            )
            .values(
                voted_users=func.array_append(_P.c.voted_users, user_id),
                vote_count=_P.c.vote_count + 1,
                updated_at=func.now(),
            )
            .returning(ProposalRow)
        )
        session = _open_session()
        try:
            async with session.begin():
                row = (await session.execute(statement)).scalars().one_or_none()
        finally:
            await session.close()
        return self._row_to_proposal(row) if row is not None else None

    async def replace_votes(
        self, proposal_id: int, voted_users: list[str]
    ) -> Proposal | None:
        """用主动拉取结果覆盖投票用户集合。"""
        self._require_ready()
        statement = (
            update(ProposalRow)
            .where(
                _P.c.id == proposal_id,
                _P.c.status == "voting",
                or_(
                    _P.c.expired_at.is_(None),
                    _P.c.expired_at > func.now(),
                ),
            )
            .values(
                voted_users=voted_users,
                vote_count=len(voted_users),
                updated_at=func.now(),
            )
            .returning(ProposalRow)
        )
        session = _open_session()
        try:
            async with session.begin():
                row = (await session.execute(statement)).scalars().one_or_none()
        finally:
            await session.close()
        return self._row_to_proposal(row) if row is not None else None

    async def claim_for_approval(
        self,
        proposal_id: int,
        approval_token: str,
        *,
        lease_seconds: int,
    ) -> Proposal | None:
        """原子认领达到票数的提案，并允许接管已过期的认领。"""
        self._require_ready()
        statement = (
            update(ProposalRow)
            .where(
                _P.c.id == proposal_id,
                _P.c.vote_count >= _P.c.required_votes,
                or_(
                    and_(
                        _P.c.status == "voting",
                        or_(
                            _P.c.expired_at.is_(None),
                            _P.c.expired_at > func.now(),
                        ),
                    ),
                    and_(
                        _P.c.status == "approving",
                        _P.c.approval_started_at
                        < func.now() - _interval(lease_seconds),
                    ),
                ),
            )
            .values(
                status="approving",
                approval_token=approval_token,
                approval_started_at=func.now(),
                updated_at=func.now(),
            )
            .returning(ProposalRow)
        )
        session = _open_session()
        try:
            async with session.begin():
                row = (await session.execute(statement)).scalars().one_or_none()
        finally:
            await session.close()
        return self._row_to_proposal(row) if row is not None else None

    async def list_approval_candidates(
        self,
        *,
        lease_seconds: int,
        limit: int,
    ) -> list[int]:
        """列出待采纳或认领租约已过期的提案，供周期恢复任务处理。"""
        self._require_ready()
        statement = (
            select(_P.c.id)
            .where(
                _P.c.vote_count >= _P.c.required_votes,
                or_(
                    and_(
                        _P.c.status == "voting",
                        or_(
                            _P.c.expired_at.is_(None),
                            _P.c.expired_at > func.now(),
                        ),
                    ),
                    and_(
                        _P.c.status == "approving",
                        or_(
                            _P.c.approval_started_at.is_(None),
                            _P.c.approval_started_at
                            < func.now() - _interval(lease_seconds),
                        ),
                    ),
                ),
            )
            .order_by(_P.c.updated_at, _P.c.id)
            .limit(limit)
        )
        session = _open_session()
        try:
            rows = (await session.execute(statement)).scalars().all()
        finally:
            await session.close()
        return [int(value) for value in rows]

    async def release_approval(
        self, proposal_id: int, approval_token: str
    ) -> None:
        """采纳失败时释放当前认领，让后续通知可以重试。"""
        self._require_ready()
        statement = (
            update(ProposalRow)
            .where(
                _P.c.id == proposal_id,
                _P.c.status == "approving",
                _P.c.approval_token == approval_token,
            )
            .values(
                status="voting",
                approval_token=None,
                approval_started_at=None,
                updated_at=func.now(),
            )
        )
        session = _open_session()
        try:
            async with session.begin():
                await session.execute(statement)
        finally:
            await session.close()

    async def mark_approved(
        self,
        proposal_id: int,
        knowledge_id: int,
        approval_token: str,
    ) -> Proposal | None:
        """标记提案已通过并回填知识 ID。"""
        self._require_ready()
        statement = (
            update(ProposalRow)
            .where(
                _P.c.id == proposal_id,
                _P.c.status == "approving",
                _P.c.approval_token == approval_token,
            )
            .values(
                status="approved",
                knowledge_id=knowledge_id,
                approved_at=func.now(),
                approval_token=None,
                approval_started_at=None,
                updated_at=func.now(),
            )
            .returning(ProposalRow)
        )
        session = _open_session()
        try:
            async with session.begin():
                row = (await session.execute(statement)).scalars().one_or_none()
        finally:
            await session.close()
        return self._row_to_proposal(row) if row is not None else None
