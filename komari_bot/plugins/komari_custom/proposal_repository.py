"""komari_custom 提案 PostgreSQL 访问层。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from komari_bot.common.database_config import get_shared_database_config
from komari_bot.common.postgres import create_postgres_pool

from .models import Proposal

if TYPE_CHECKING:
    import asyncpg


class ProposalRepository:
    """知识库提案 DAO。"""

    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None

    async def initialize(self) -> None:
        """初始化连接池并创建表结构。"""
        if self._pool is not None:
            return
        db_config = get_shared_database_config()
        self._pool = await create_postgres_pool(db_config, command_timeout=30)
        await self._create_schema()

    async def close(self) -> None:
        """关闭连接池。"""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _create_schema(self) -> None:
        """执行插件内置 DDL。"""
        assert self._pool is not None
        sql = Path(__file__).with_name("init_db.sql").read_text(encoding="utf-8")
        async with self._pool.acquire() as conn:
            await conn.execute(sql)

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            msg = "komari_custom 数据库尚未初始化"
            raise RuntimeError(msg)
        return self._pool

    @staticmethod
    def _row_to_proposal(row: asyncpg.Record) -> Proposal:
        return Proposal(
            id=int(row["id"]),
            group_id=int(row["group_id"]),
            proposer_id=int(row["proposer_id"]),
            proposer_name=(
                str(row["proposer_name"])
                if row["proposer_name"] is not None
                else None
            ),
            title=str(row["title"]),
            content=str(row["content"]),
            status=row["status"],
            vote_message_id=(
                int(row["vote_message_id"])
                if row["vote_message_id"] is not None
                else None
            ),
            vote_count=int(row["vote_count"]),
            required_votes=int(row["required_votes"]),
            voted_users=[str(item) for item in row["voted_users"]],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            approved_at=row["approved_at"],
            knowledge_id=int(row["knowledge_id"]) if row["knowledge_id"] is not None else None,
            expired_at=row["expired_at"],
        )

    async def cleanup_expired(self) -> int:
        """删除已过期且未通过的提案，返回删除数量。"""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM komari_custom_proposals
                WHERE status = 'voting' AND expired_at IS NOT NULL AND expired_at < NOW()
                """
            )
        return int(result.rsplit(" ", maxsplit=1)[-1])

    async def count_active_by_user(self, group_id: int, proposer_id: int) -> int:
        """统计用户在本群仍在投票期内的提案数量。"""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM komari_custom_proposals
                WHERE group_id = $1
                  AND proposer_id = $2
                  AND status = 'voting'
                  AND (expired_at IS NULL OR expired_at > NOW())
                """,
                group_id,
                proposer_id,
            )
        return int(value or 0)

    async def create_proposal(
        self,
        *,
        group_id: int,
        proposer_id: int,
        proposer_name: str | None,
        title: str,
        content: str,
        required_votes: int,
        expire_hours: int,
    ) -> Proposal:
        """创建投票中提案。"""
        pool = self._require_pool()
        expired_at = datetime.now().astimezone() + timedelta(hours=expire_hours)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO komari_custom_proposals (
                    group_id, proposer_id, proposer_name, title, content,
                    required_votes, expired_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING *
                """,
                group_id,
                proposer_id,
                proposer_name,
                title,
                content,
                required_votes,
                expired_at,
            )
        if row is None:
            msg = "创建提案失败"
            raise RuntimeError(msg)
        return self._row_to_proposal(row)

    async def set_vote_message_id(self, proposal_id: int, message_id: int) -> None:
        """回填投票消息 ID。"""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE komari_custom_proposals
                SET vote_message_id = $2, updated_at = NOW()
                WHERE id = $1
                """,
                proposal_id,
                message_id,
            )

    async def find_by_vote_message_id(self, message_id: int) -> Proposal | None:
        """通过投票消息 ID 查找提案。"""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM komari_custom_proposals
                WHERE vote_message_id = $1
                """,
                message_id,
            )
        return self._row_to_proposal(row) if row is not None else None

    async def get_by_id(self, proposal_id: int, group_id: int | None = None) -> Proposal | None:
        """按 ID 获取提案。"""
        pool = self._require_pool()
        where_group = "AND group_id = $2" if group_id is not None else ""
        params = (proposal_id, group_id) if group_id is not None else (proposal_id,)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT * FROM komari_custom_proposals
                WHERE id = $1 {where_group}
                """,
                *params,
            )
        return self._row_to_proposal(row) if row is not None else None

    async def list_proposals(
        self,
        *,
        group_id: int,
        limit: int,
        offset: int,
    ) -> tuple[list[Proposal], int]:
        """分页列出本群有效提案。"""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM komari_custom_proposals
                WHERE group_id = $1
                  AND (status = 'approved' OR (status = 'voting' AND (expired_at IS NULL OR expired_at > NOW())))
                ORDER BY id DESC
                LIMIT $2 OFFSET $3
                """,
                group_id,
                limit,
                offset,
            )
            total = await conn.fetchval(
                """
                SELECT COUNT(*) FROM komari_custom_proposals
                WHERE group_id = $1
                  AND (status = 'approved' OR (status = 'voting' AND (expired_at IS NULL OR expired_at > NOW())))
                """,
                group_id,
            )
        return [self._row_to_proposal(row) for row in rows], int(total or 0)

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
            return proposals[index - 1] if index <= total and index <= len(proposals) else None

        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM komari_custom_proposals
                WHERE group_id = $1
                  AND title ILIKE $2
                  AND (status = 'approved' OR (status = 'voting' AND (expired_at IS NULL OR expired_at > NOW())))
                ORDER BY id DESC
                LIMIT 1
                """,
                group_id,
                f"%{selector}%",
            )
        return self._row_to_proposal(row) if row is not None else None

    async def add_vote(self, proposal_id: int, user_id: str) -> Proposal | None:
        """为提案添加一个去重后的有效投票。"""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE komari_custom_proposals
                SET voted_users = array_append(voted_users, $2),
                    vote_count = vote_count + 1,
                    updated_at = NOW()
                WHERE id = $1
                  AND status = 'voting'
                  AND proposer_id::TEXT <> $2
                  AND NOT ($2 = ANY(voted_users))
                  AND (expired_at IS NULL OR expired_at > NOW())
                RETURNING *
                """,
                proposal_id,
                user_id,
            )
        return self._row_to_proposal(row) if row is not None else None

    async def replace_votes(self, proposal_id: int, voted_users: list[str]) -> Proposal | None:
        """用主动拉取结果覆盖投票用户集合。"""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE komari_custom_proposals
                SET voted_users = $2::TEXT[],
                    vote_count = cardinality($2::TEXT[]),
                    updated_at = NOW()
                WHERE id = $1
                  AND status = 'voting'
                  AND (expired_at IS NULL OR expired_at > NOW())
                RETURNING *
                """,
                proposal_id,
                voted_users,
            )
        return self._row_to_proposal(row) if row is not None else None

    async def mark_approved(self, proposal_id: int, knowledge_id: int) -> Proposal | None:
        """标记提案已通过并回填知识 ID。"""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE komari_custom_proposals
                SET status = 'approved',
                    knowledge_id = $2,
                    approved_at = NOW(),
                    updated_at = NOW()
                WHERE id = $1
                RETURNING *
                """,
                proposal_id,
                knowledge_id,
            )
        return self._row_to_proposal(row) if row is not None else None
