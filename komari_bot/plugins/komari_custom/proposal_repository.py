"""komari_custom 提案 PostgreSQL 访问层。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from komari_bot.common.database_config import get_shared_database_config
from komari_bot.common.postgres import create_postgres_pool
from komari_bot.common.sql_like_utils import escape_like_pattern

from .models import Proposal

if TYPE_CHECKING:
    import asyncpg


class ProposalRepository:
    """知识库提案 DAO。"""

    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None
        self._initialize_lock: asyncio.Lock | None = None
        self._initialize_lock_loop: asyncio.AbstractEventLoop | None = None

    def _get_initialize_lock(self) -> asyncio.Lock:
        """获取绑定到当前事件循环的初始化锁。"""
        loop = asyncio.get_running_loop()
        if self._initialize_lock is None or self._initialize_lock_loop is not loop:
            self._initialize_lock = asyncio.Lock()
            self._initialize_lock_loop = loop
        return self._initialize_lock

    async def initialize(self) -> None:
        """初始化连接池并创建表结构。"""
        if self._pool is not None:
            return
        async with self._get_initialize_lock():
            if self._pool is not None:
                return
            db_config = get_shared_database_config()
            pool = await create_postgres_pool(db_config, command_timeout=30)
            try:
                await self._create_schema(pool)
            except Exception:
                await pool.close()
                raise
            self._pool = pool

    async def close(self) -> None:
        """关闭连接池。"""
        async with self._get_initialize_lock():
            pool = self._pool
            self._pool = None
            if pool is not None:
                await pool.close()

    async def _create_schema(self, pool: asyncpg.Pool) -> None:
        """执行插件内置 DDL。"""
        sql = Path(__file__).with_name("init_db.sql").read_text(encoding="utf-8")
        async with pool.acquire() as conn:
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
            publication_key=str(row["publication_key"]),
            publication_token=row["publication_token"],
            publication_started_at=row["publication_started_at"],
            publication_attempts=int(row["publication_attempts"]),
            publication_error_code=row["publication_error_code"],
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
            approval_token=row["approval_token"],
            approval_started_at=row["approval_started_at"],
        )

    async def cleanup_expired(self) -> int:
        """删除过期投票和超过会话恢复窗口的遗留发布记录。"""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM komari_custom_proposals
                WHERE (
                    status = 'voting'
                    AND expired_at IS NOT NULL
                    AND expired_at < NOW()
                ) OR (
                    status IN ('publishing', 'failed')
                    AND updated_at < NOW() - INTERVAL '7 days'
                )
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
                  AND status IN ('publishing', 'voting', 'approving')
                  AND (expired_at IS NULL OR expired_at > NOW())
                """,
                group_id,
                proposer_id,
            )
        return int(value or 0)

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
        pool = self._require_pool()
        expired_at = datetime.now().astimezone() + timedelta(hours=expire_hours)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO komari_custom_proposals (
                    publication_key, publication_token, publication_started_at,
                    publication_attempts, group_id, proposer_id, proposer_name,
                    title, content, status, required_votes, expired_at
                )
                VALUES ($1, $2, NOW(), 1, $3, $4, $5, $6, $7,
                        'publishing', $8, $9)
                ON CONFLICT (publication_key) DO UPDATE
                SET publication_token = EXCLUDED.publication_token,
                    publication_started_at = NOW(),
                    publication_attempts =
                        komari_custom_proposals.publication_attempts + 1,
                    publication_error_code = NULL,
                    vote_message_id = NULL,
                    group_id = EXCLUDED.group_id,
                    proposer_id = EXCLUDED.proposer_id,
                    proposer_name = EXCLUDED.proposer_name,
                    title = EXCLUDED.title,
                    content = EXCLUDED.content,
                    status = 'publishing',
                    required_votes = EXCLUDED.required_votes,
                    expired_at = EXCLUDED.expired_at,
                    updated_at = NOW()
                WHERE komari_custom_proposals.status = 'failed'
                   OR (
                       komari_custom_proposals.status = 'publishing'
                       AND (
                           komari_custom_proposals.publication_started_at IS NULL
                           OR komari_custom_proposals.publication_started_at
                               < NOW() - ($10 * INTERVAL '1 second')
                       )
                   )
                RETURNING *
                """,
                publication_key,
                publication_token,
                group_id,
                proposer_id,
                proposer_name,
                title,
                content,
                required_votes,
                expired_at,
                lease_seconds,
            )
        return self._row_to_proposal(row) if row is not None else None

    async def get_by_publication_key(self, publication_key: str) -> Proposal | None:
        """按发布幂等键读取提案。"""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM komari_custom_proposals
                WHERE publication_key = $1
                """,
                publication_key,
            )
        return self._row_to_proposal(row) if row is not None else None

    async def complete_publication(
        self,
        proposal_id: int,
        message_id: int,
        publication_token: str,
    ) -> Proposal | None:
        """由当前发布认领者原子回填消息 ID 并进入投票态。"""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE komari_custom_proposals
                SET status = 'voting',
                    vote_message_id = $2,
                    publication_token = NULL,
                    publication_error_code = NULL,
                    updated_at = NOW()
                WHERE id = $1
                  AND status = 'publishing'
                  AND publication_token = $3
                  AND (vote_message_id IS NULL OR vote_message_id = $2)
                RETURNING *
                """,
                proposal_id,
                message_id,
                publication_token,
            )
        return self._row_to_proposal(row) if row is not None else None

    async def recover_publication(
        self,
        publication_key: str,
        message_id: int,
    ) -> Proposal | None:
        """用编辑会话已记录的消息 ID 恢复中断的发布提交。"""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE komari_custom_proposals
                SET status = 'voting',
                    vote_message_id = $2,
                    publication_token = NULL,
                    publication_error_code = NULL,
                    updated_at = NOW()
                WHERE publication_key = $1
                  AND status IN ('publishing', 'failed')
                  AND (vote_message_id IS NULL OR vote_message_id = $2)
                RETURNING *
                """,
                publication_key,
                message_id,
            )
        return self._row_to_proposal(row) if row is not None else None

    async def mark_publication_failed(
        self,
        proposal_id: int,
        publication_token: str,
        error_code: str,
    ) -> Proposal | None:
        """由当前认领者把发送失败记录为可重试状态。"""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE komari_custom_proposals
                SET status = 'failed',
                    publication_token = NULL,
                    publication_error_code = $3,
                    updated_at = NOW()
                WHERE id = $1
                  AND status = 'publishing'
                  AND publication_token = $2
                RETURNING *
                """,
                proposal_id,
                publication_token,
                error_code,
            )
        return self._row_to_proposal(row) if row is not None else None

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
                  AND (
                      status IN ('approved', 'approving')
                      OR (status = 'voting' AND (expired_at IS NULL OR expired_at > NOW()))
                  )
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
                  AND (
                      status IN ('approved', 'approving')
                      OR (status = 'voting' AND (expired_at IS NULL OR expired_at > NOW()))
                  )
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
        keyword_pattern = f"%{escape_like_pattern(selector)}%"
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM komari_custom_proposals
                WHERE group_id = $1
                  AND title ILIKE $2 ESCAPE '\\'
                  AND (
                      status IN ('approved', 'approving')
                      OR (status = 'voting' AND (expired_at IS NULL OR expired_at > NOW()))
                  )
                ORDER BY id DESC
                LIMIT 1
                """,
                group_id,
                keyword_pattern,
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

    async def claim_for_approval(
        self,
        proposal_id: int,
        approval_token: str,
        *,
        lease_seconds: int,
    ) -> Proposal | None:
        """原子认领达到票数的提案，并允许接管已过期的认领。"""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE komari_custom_proposals
                SET status = 'approving',
                    approval_token = $2,
                    approval_started_at = NOW(),
                    updated_at = NOW()
                WHERE id = $1
                  AND vote_count >= required_votes
                  AND (
                      (
                          status = 'voting'
                          AND (expired_at IS NULL OR expired_at > NOW())
                      )
                      OR (
                          status = 'approving'
                          AND approval_started_at < NOW() - ($3 * INTERVAL '1 second')
                      )
                  )
                RETURNING *
                """,
                proposal_id,
                approval_token,
                lease_seconds,
            )
        return self._row_to_proposal(row) if row is not None else None

    async def release_approval(self, proposal_id: int, approval_token: str) -> None:
        """采纳失败时释放当前认领，让后续通知可以重试。"""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE komari_custom_proposals
                SET status = 'voting',
                    approval_token = NULL,
                    approval_started_at = NULL,
                    updated_at = NOW()
                WHERE id = $1
                  AND status = 'approving'
                  AND approval_token = $2
                """,
                proposal_id,
                approval_token,
            )

    async def mark_approved(
        self,
        proposal_id: int,
        knowledge_id: int,
        approval_token: str,
    ) -> Proposal | None:
        """标记提案已通过并回填知识 ID。"""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE komari_custom_proposals
                SET status = 'approved',
                    knowledge_id = $2,
                    approved_at = NOW(),
                    approval_token = NULL,
                    approval_started_at = NULL,
                    updated_at = NOW()
                WHERE id = $1
                  AND status = 'approving'
                  AND approval_token = $3
                RETURNING *
                """,
                proposal_id,
                knowledge_id,
                approval_token,
            )
        return self._row_to_proposal(row) if row is not None else None
