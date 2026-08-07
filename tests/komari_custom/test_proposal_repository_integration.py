"""ProposalRepository ORM 化后的 PostgreSQL 集成测试。

依赖已执行 ``alembic upgrade head`` 的迁移管理 schema（``KOMARI_TEST_POSTGRES_URL``
门控）。本文件取代旧 ``test_proposal_repository.py`` 中与 asyncpg SQL 字符串
耦合的 fake 单测：行为契约（幂等键发布/重试复用同一 proposal、平台消息 ID 回填、
投票去重、采纳租约认领/释放/标记通过、过期清理、分页与关键词查询）全部通过
仓储公共接口在真实库上断言，行为覆盖不减少。数据准备与清理走同一套 SQLModel
表模型。
"""

from __future__ import annotations

import os
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from komari_bot.plugins.komari_custom.orm_models import ProposalRow
from komari_bot.plugins.komari_custom.proposal_repository import (
    ProposalRepository,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from komari_bot.plugins.komari_custom.models import Proposal

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")

_P = ProposalRow.__table__


def _configured_database_url() -> str:
    from nonebot import get_driver

    return str(
        getattr(get_driver().config, "sqlalchemy_database_url", "") or ""
    )


def _same_database(left: str, right: str) -> bool:
    left_parsed = urlparse(left)
    right_parsed = urlparse(right)
    return (
        left_parsed.hostname == right_parsed.hostname
        and (left_parsed.port or 5432) == (right_parsed.port or 5432)
        and left_parsed.path == right_parsed.path
    )


async def _reset_shared_orm_engine() -> None:
    """清空 nonebot-plugin-orm 共享引擎连接池（每个测试独立事件循环）。"""
    from nonebot import require

    require("nonebot_plugin_orm")
    import nonebot_plugin_orm as orm_module

    engines = getattr(orm_module, "_engines", None)
    if not engines:
        return
    for engine in list(engines.values()):
        with suppress(Exception):
            await engine.dispose()


def _open_session() -> "AsyncSession":
    from nonebot_plugin_orm import get_session

    return get_session(expire_on_commit=False)


def _now() -> datetime:
    return datetime.now(UTC)


def _make_key(tag: str) -> str:
    return f"{tag}-{uuid4().hex}"


def _row(
    *,
    group_id: int = 100,
    proposer_id: int = 200,
    proposer_name: str | None = "集成测试用户",
    title: str = "集成测试标题",
    content: str = "集成测试正文",
    status: str = "publishing",
    publication_key: str | None = None,
    publication_token: str | None = None,
    publication_started_at: datetime | None = None,
    publication_attempts: int = 1,
    publication_error_code: str | None = None,
    approval_token: str | None = None,
    approval_started_at: datetime | None = None,
    vote_message_id: int | None = None,
    vote_count: int = 0,
    required_votes: int = 3,
    voted_users: list[str] | None = None,
    approved_at: datetime | None = None,
    knowledge_id: int | None = None,
    expired_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> ProposalRow:
    now = _now()
    # 主键 id 由数据库 SERIAL 生成，构造时缺省属预期（pyright 静态告警忽略）
    return ProposalRow(  # pyright: ignore[reportCallIssue]
        group_id=group_id,
        proposer_id=proposer_id,
        proposer_name=proposer_name,
        title=title,
        content=content,
        status=status,
        publication_key=publication_key or _make_key("pub"),
        publication_token=publication_token,
        publication_started_at=publication_started_at,
        publication_attempts=publication_attempts,
        publication_error_code=publication_error_code,
        approval_token=approval_token,
        approval_started_at=approval_started_at,
        vote_message_id=vote_message_id,
        vote_count=vote_count,
        required_votes=required_votes,
        voted_users=voted_users or [],
        created_at=now,
        updated_at=updated_at or now,
        approved_at=approved_at,
        knowledge_id=knowledge_id,
        expired_at=expired_at,
    )


async def _insert_rows(rows: list[ProposalRow]) -> list[int]:
    session = _open_session()
    try:
        session.add_all(rows)
        await session.commit()
        ids = [int(row.id) for row in rows]
    finally:
        await session.close()
    return ids


async def _fetch_row(proposal_id: int) -> ProposalRow | None:
    session = _open_session()
    try:
        return (
            await session.execute(
                select(ProposalRow).where(_P.c.id == proposal_id)
            )
        ).scalar_one_or_none()
    finally:
        await session.close()


async def _cleanup(proposal_ids: list[int]) -> None:
    session = _open_session()
    try:
        if proposal_ids:
            await session.execute(
                delete(ProposalRow).where(_P.c.id.in_(proposal_ids))
            )
        await session.commit()
    finally:
        await session.close()


def _assert_proposal_matches(proposal: Proposal, row: ProposalRow) -> None:
    assert proposal.id == int(row.id)
    assert proposal.group_id == int(row.group_id)
    assert proposal.proposer_id == int(row.proposer_id)
    assert proposal.proposer_name == row.proposer_name
    assert proposal.title == row.title
    assert proposal.content == row.content
    assert proposal.status == row.status
    assert proposal.publication_key == row.publication_key
    assert proposal.publication_token == row.publication_token
    assert proposal.publication_attempts == int(row.publication_attempts)
    assert proposal.publication_error_code == row.publication_error_code
    assert proposal.vote_message_id == row.vote_message_id
    assert proposal.vote_count == int(row.vote_count)
    assert proposal.required_votes == int(row.required_votes)
    assert proposal.voted_users == list(row.voted_users)
    assert proposal.knowledge_id == row.knowledge_id
    assert proposal.approval_token == row.approval_token
    assert proposal.approved_at == row.approved_at


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_initialize_is_concurrency_safe_and_close_resets() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repository = ProposalRepository()
    try:
        import asyncio

        await asyncio.gather(repository.initialize(), repository.initialize())
        assert repository._ready is True

        await repository.close()
        assert repository._ready is False
        with pytest.raises(RuntimeError, match="komari_custom 数据库尚未初始化"):
            await repository.get_by_id(1)
    finally:
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_claim_publication_creates_publishing_proposal_with_attempts() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repository = ProposalRepository()
    await repository.initialize()
    publication_key = _make_key("stable")
    proposal: Proposal | None = None
    try:
        proposal = await repository.claim_publication(
            publication_key=publication_key,
            publication_token="claim-token",
            group_id=100,
            proposer_id=200,
            proposer_name="测试用户",
            title="标题",
            content="正文",
            required_votes=3,
            expire_hours=2,
            lease_seconds=300,
        )
        assert proposal is not None
        assert proposal.status == "publishing"
        assert proposal.publication_attempts == 1
        assert proposal.publication_token == "claim-token"
        assert proposal.publication_started_at is not None
        assert proposal.expired_at is not None
        assert proposal.voted_users == []

        row = await _fetch_row(proposal.id)
        assert row is not None
        _assert_proposal_matches(proposal, row)

        # 活跃发布（publishing 等非 failed 状态）的并发 confirm 必须被拒绝
        refused = await repository.claim_publication(
            publication_key=publication_key,
            publication_token="claim-token-2",
            group_id=100,
            proposer_id=200,
            proposer_name="测试用户",
            title="标题",
            content="正文",
            required_votes=3,
            expire_hours=2,
            lease_seconds=300,
        )
        assert refused is None
        assert (await _fetch_row(proposal.id)) is not None
    finally:
        await _cleanup([proposal.id] if proposal is not None else [])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_failed_publication_retry_reuses_same_proposal_and_bumps_attempts() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repository = ProposalRepository()
    await repository.initialize()
    publication_key = _make_key("retry")
    first: Proposal | None = None
    try:
        first = await repository.claim_publication(
            publication_key=publication_key,
            publication_token="token-1",
            group_id=100,
            proposer_id=200,
            proposer_name="测试用户",
            title="标题",
            content="正文",
            required_votes=3,
            expire_hours=2,
            lease_seconds=300,
        )
        assert first is not None
        failed = await repository.mark_publication_failed(
            first.id, "token-1", "send_rejected"
        )
        assert failed is not None
        assert failed.status == "failed"
        assert failed.publication_error_code == "send_rejected"
        assert failed.publication_token is None

        retried = await repository.claim_publication(
            publication_key=publication_key,
            publication_token="token-2",
            group_id=100,
            proposer_id=200,
            proposer_name="测试用户",
            title="标题",
            content="正文",
            required_votes=3,
            expire_hours=2,
            lease_seconds=300,
        )
        assert retried is not None
        assert retried.id == first.id
        assert retried.publication_attempts == 2
        assert retried.publication_token == "token-2"
        assert retried.publication_error_code is None
        assert retried.vote_message_id is None

        # 不可重试错误码（delivery_unknown）不允许复用同一 proposal
        failed_unknown = await repository.mark_publication_failed(
            retried.id, "token-2", "delivery_unknown"
        )
        assert failed_unknown is not None
        refused = await repository.claim_publication(
            publication_key=publication_key,
            publication_token="token-3",
            group_id=100,
            proposer_id=200,
            proposer_name="测试用户",
            title="标题",
            content="正文",
            required_votes=3,
            expire_hours=2,
            lease_seconds=300,
        )
        assert refused is None
    finally:
        await _cleanup([first.id] if first is not None else [])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_complete_publication_requires_current_claim_token_and_backfills_id() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repository = ProposalRepository()
    await repository.initialize()
    publication_key = _make_key("complete")
    proposal: Proposal | None = None
    try:
        proposal = await repository.claim_publication(
            publication_key=publication_key,
            publication_token="claim-token",
            group_id=100,
            proposer_id=200,
            proposer_name="测试用户",
            title="标题",
            content="正文",
            required_votes=3,
            expire_hours=2,
            lease_seconds=300,
        )
        assert proposal is not None

        wrong_token = await repository.complete_publication(
            proposal.id, 7788, "other-token"
        )
        assert wrong_token is None

        completed = await repository.complete_publication(
            proposal.id, 7788, "claim-token"
        )
        assert completed is not None
        assert completed.status == "voting"
        assert completed.vote_message_id == 7788
        assert completed.publication_token is None
        assert completed.publication_error_code is None

        # 已完成（voting）后再回填必须失败
        duplicate = await repository.complete_publication(
            proposal.id, 7788, "claim-token"
        )
        assert duplicate is None

        assert await repository.find_by_vote_message_id(7788) is not None
        assert await repository.find_by_vote_message_id(7799) is None
    finally:
        await _cleanup([proposal.id] if proposal is not None else [])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_recover_publication_restores_interrupted_commit() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repository = ProposalRepository()
    await repository.initialize()
    publishing_id = 0
    failed_id = 0
    voting_id = 0
    try:
        (publishing_id,) = await _insert_rows([_row(status="publishing")])
        (failed_id,) = await _insert_rows(
            [_row(status="failed", publication_error_code="delivery_unknown")]
        )
        (voting_id,) = await _insert_rows([_row(status="voting")])

        publishing = await _fetch_row(publishing_id)
        assert publishing is not None
        recovered = await repository.recover_publication(
            publishing.publication_key, 8899
        )
        assert recovered is not None
        assert recovered.id == publishing_id
        assert recovered.status == "voting"
        assert recovered.vote_message_id == 8899
        assert recovered.publication_token is None

        failed = await _fetch_row(failed_id)
        assert failed is not None
        recovered_failed = await repository.recover_publication(
            failed.publication_key, 8899
        )
        assert recovered_failed is not None
        assert recovered_failed.id == failed_id
        assert recovered_failed.status == "voting"

        # 已在投票态的提案不允许再次恢复
        voting = await _fetch_row(voting_id)
        assert voting is not None
        assert (
            await repository.recover_publication(voting.publication_key, 8899)
            is None
        )
    finally:
        await _cleanup([publishing_id, failed_id, voting_id])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_get_by_publication_key_and_get_by_id_with_group() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repository = ProposalRepository()
    await repository.initialize()
    proposal_id = 0
    try:
        row = _row(status="voting", group_id=101, proposer_id=201)
        (proposal_id,) = await _insert_rows([row])
        by_key = await repository.get_by_publication_key(row.publication_key)
        assert by_key is not None
        assert by_key.id == proposal_id
        assert await repository.get_by_publication_key("not-exist-key") is None

        by_id = await repository.get_by_id(proposal_id)
        assert by_id is not None
        assert by_id.group_id == 101
        scoped = await repository.get_by_id(proposal_id, group_id=101)
        assert scoped is not None
        assert await repository.get_by_id(proposal_id, group_id=999) is None
        assert await repository.get_by_id(999_999_999) is None
    finally:
        await _cleanup([proposal_id])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_count_active_by_user_only_counts_in_progress() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repository = ProposalRepository()
    await repository.initialize()
    ids: list[int] = []
    try:
        ids = await _insert_rows(
            [
                _row(status="publishing", group_id=100, proposer_id=200),
                _row(status="voting", group_id=100, proposer_id=200),
                _row(status="approving", group_id=100, proposer_id=200),
                _row(
                    status="voting",
                    group_id=100,
                    proposer_id=200,
                    expired_at=_now() - timedelta(seconds=1),
                ),
                _row(status="approved", group_id=100, proposer_id=200),
                _row(status="failed", group_id=100, proposer_id=200),
                _row(status="voting", group_id=300, proposer_id=200),
                _row(status="voting", group_id=100, proposer_id=400),
            ]
        )
        assert await repository.count_active_by_user(100, 200) == 3
        assert await repository.count_active_by_user(300, 200) == 1
        assert await repository.count_active_by_user(100, 400) == 1
        assert await repository.count_active_by_user(999, 200) == 0
    finally:
        await _cleanup(ids)
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_list_proposals_pages_and_filters_active_only() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repository = ProposalRepository()
    await repository.initialize()
    ids: list[int] = []
    try:
        approved = _row(status="approved", title="已通过提案", group_id=100)
        approving = _row(status="approving", title="采纳中提案", group_id=100)
        voting = _row(status="voting", title="投票中提案", group_id=100)
        expired_voting = _row(
            status="voting",
            title="过期投票",
            group_id=100,
            expired_at=_now() - timedelta(seconds=1),
        )
        publishing = _row(status="publishing", title="发布中", group_id=100)
        failed = _row(status="failed", title="失败提案", group_id=100)
        other_group = _row(status="voting", title="他群提案", group_id=500)
        ids = await _insert_rows(
            [
                approved,
                approving,
                voting,
                expired_voting,
                publishing,
                failed,
                other_group,
            ]
        )

        proposals, total = await repository.list_proposals(
            group_id=100, limit=10, offset=0
        )
        assert total == 3
        assert [proposal.title for proposal in proposals] == [
            "投票中提案",
            "采纳中提案",
            "已通过提案",
        ]
        assert all(proposal.group_id == 100 for proposal in proposals)

        page_one, total_page = await repository.list_proposals(
            group_id=100, limit=2, offset=0
        )
        assert total_page == 3
        assert [proposal.title for proposal in page_one] == [
            "投票中提案",
            "采纳中提案",
        ]
        page_two, _ = await repository.list_proposals(
            group_id=100, limit=2, offset=2
        )
        assert [proposal.title for proposal in page_two] == ["已通过提案"]
    finally:
        await _cleanup(ids)
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_find_in_group_by_index_or_keyword() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repository = ProposalRepository()
    await repository.initialize()
    ids: list[int] = []
    try:
        ids = await _insert_rows(
            [
                _row(status="approved", title="第一个 100%_x 完整", group_id=100),
                _row(status="voting", title="第二个标题", group_id=100),
                _row(status="voting", title="隐藏_下划线 提案", group_id=100),
                _row(status="approved", title="其他群标题", group_id=500),
            ]
        )

        by_index = await repository.find_in_group_by_index_or_keyword(
            group_id=100, selector="2", limit_for_index=10
        )
        assert by_index is not None
        assert by_index.title == "第二个标题"

        out_of_range = await repository.find_in_group_by_index_or_keyword(
            group_id=100, selector="99", limit_for_index=10
        )
        assert out_of_range is None

        by_keyword = await repository.find_in_group_by_index_or_keyword(
            group_id=100, selector="第二个", limit_for_index=10
        )
        assert by_keyword is not None
        assert by_keyword.id == ids[1]

        # 通配符转义：% 与 _ 均按字面匹配，不参与模糊匹配
        wildcard = await repository.find_in_group_by_index_or_keyword(
            group_id=100, selector="100%_x", limit_for_index=10
        )
        assert wildcard is not None
        assert wildcard.title == "第一个 100%_x 完整"

        underscore = await repository.find_in_group_by_index_or_keyword(
            group_id=100, selector="隐藏_下划线", limit_for_index=10
        )
        assert underscore is not None
        assert underscore.title == "隐藏_下划线 提案"

        # 若 _ 未转义，`隐藏X下划线` 会把 X 当作单字符通配而误命中
        escaped = await repository.find_in_group_by_index_or_keyword(
            group_id=100, selector="隐藏X下划线", limit_for_index=10
        )
        assert escaped is None

        not_found = await repository.find_in_group_by_index_or_keyword(
            group_id=100, selector="不存在的关键词", limit_for_index=10
        )
        assert not_found is None

        # 仅限本群
        other_group = await repository.find_in_group_by_index_or_keyword(
            group_id=100, selector="其他群标题", limit_for_index=10
        )
        assert other_group is None
    finally:
        await _cleanup(ids)
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_add_vote_deduplicates_and_rejects_proposer_and_expired() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repository = ProposalRepository()
    await repository.initialize()
    voting_id = 0
    expired_id = 0
    approved_id = 0
    try:
        (voting_id,) = await _insert_rows(
            [
                _row(
                    status="voting",
                    proposer_id=200,
                    voted_users=["111"],
                    vote_count=1,
                )
            ]
        )
        (expired_id,) = await _insert_rows(
            [
                _row(
                    status="voting",
                    expired_at=_now() - timedelta(seconds=1),
                    voted_users=[],
                    vote_count=0,
                )
            ]
        )
        (approved_id,) = await _insert_rows([_row(status="approved")])

        first_vote = await repository.add_vote(voting_id, "222")
        assert first_vote is not None
        assert first_vote.vote_count == 2
        assert sorted(first_vote.voted_users) == ["111", "222"]

        duplicate = await repository.add_vote(voting_id, "222")
        assert duplicate is None

        proposer = await repository.add_vote(voting_id, "200")
        assert proposer is None

        expired = await repository.add_vote(expired_id, "333")
        assert expired is None

        wrong_status = await repository.add_vote(approved_id, "333")
        assert wrong_status is None

        # 去重与计数以数据库实际内容为准
        row = await _fetch_row(voting_id)
        assert row is not None
        assert int(row.vote_count) == 2
        assert sorted(row.voted_users) == ["111", "222"]
    finally:
        await _cleanup([voting_id, expired_id, approved_id])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_replace_votes_overwrites_collection_and_count() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repository = ProposalRepository()
    await repository.initialize()
    voting_id = 0
    try:
        (voting_id,) = await _insert_rows(
            [
                _row(
                    status="voting",
                    voted_users=["111", "222"],
                    vote_count=2,
                )
            ]
        )
        replaced = await repository.replace_votes(
            voting_id, ["333", "444", "555"]
        )
        assert replaced is not None
        assert replaced.vote_count == 3
        assert sorted(replaced.voted_users) == ["333", "444", "555"]

        # 空集合覆盖
        cleared = await repository.replace_votes(voting_id, [])
        assert cleared is not None
        assert cleared.vote_count == 0
        assert cleared.voted_users == []

        wrong_status = await repository.replace_votes(999_999_999, ["111"])
        assert wrong_status is None
    finally:
        await _cleanup([voting_id])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_claim_for_approval_requires_votes_and_allows_stale_takeover() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repository = ProposalRepository()
    await repository.initialize()
    ready_id = 0
    short_id = 0
    stale_id = 0
    fresh_id = 0
    expired_id = 0
    try:
        (ready_id,) = await _insert_rows(
            [
                _row(
                    status="voting",
                    vote_count=3,
                    required_votes=3,
                    voted_users=["111", "222", "333"],
                )
            ]
        )
        (short_id,) = await _insert_rows(
            [
                _row(
                    status="voting",
                    vote_count=2,
                    required_votes=3,
                    voted_users=["111", "222"],
                )
            ]
        )
        (stale_id,) = await _insert_rows(
            [
                _row(
                    status="approving",
                    approval_token="old-token",
                    approval_started_at=_now() - timedelta(seconds=600),
                    vote_count=3,
                    required_votes=3,
                )
            ]
        )
        (fresh_id,) = await _insert_rows(
            [
                _row(
                    status="approving",
                    approval_token="fresh-token",
                    approval_started_at=_now(),
                    vote_count=3,
                    required_votes=3,
                )
            ]
        )
        (expired_id,) = await _insert_rows(
            [
                _row(
                    status="voting",
                    expired_at=_now() - timedelta(seconds=1),
                    vote_count=3,
                    required_votes=3,
                )
            ]
        )

        claimed = await repository.claim_for_approval(
            ready_id, "approval-token", lease_seconds=300
        )
        assert claimed is not None
        assert claimed.status == "approving"
        assert claimed.approval_token == "approval-token"
        assert claimed.approval_started_at is not None

        # 未达票数不可认领
        assert (
            await repository.claim_for_approval(
                short_id, "token-short", lease_seconds=300
            )
            is None
        )
        # 过期投票不可认领
        assert (
            await repository.claim_for_approval(
                expired_id, "token-expired", lease_seconds=300
            )
            is None
        )
        # 租约未过期的 approving 不可接管
        assert (
            await repository.claim_for_approval(
                fresh_id, "token-fresh", lease_seconds=300
            )
            is None
        )
        # 租约已过期的 approving 可被接管
        taken_over = await repository.claim_for_approval(
            stale_id, "takeover-token", lease_seconds=300
        )
        assert taken_over is not None
        assert taken_over.approval_token == "takeover-token"
    finally:
        await _cleanup(
            [ready_id, short_id, stale_id, fresh_id, expired_id]
        )
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_list_approval_candidates_includes_stale_claims_and_missed_votes() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repository = ProposalRepository()
    await repository.initialize()
    ids: list[int] = []
    try:
        ready = _row(
            status="voting",
            vote_count=3,
            required_votes=3,
            updated_at=_now() - timedelta(minutes=5),
        )
        stale_approving = _row(
            status="approving",
            approval_started_at=_now() - timedelta(minutes=5),
            vote_count=3,
            required_votes=3,
            updated_at=_now() - timedelta(minutes=5),
        )
        null_started_approving = _row(
            status="approving",
            approval_started_at=None,
            vote_count=3,
            required_votes=3,
            updated_at=_now() - timedelta(minutes=5),
        )
        fresh_approving = _row(
            status="approving",
            approval_started_at=_now(),
            vote_count=3,
            required_votes=3,
            updated_at=_now() - timedelta(minutes=5),
        )
        short_votes = _row(
            status="voting",
            vote_count=2,
            required_votes=3,
            updated_at=_now() - timedelta(minutes=5),
        )
        expired = _row(
            status="voting",
            vote_count=3,
            required_votes=3,
            expired_at=_now() - timedelta(seconds=1),
            updated_at=_now() - timedelta(minutes=5),
        )
        ids = await _insert_rows(
            [
                ready,
                stale_approving,
                null_started_approving,
                fresh_approving,
                short_votes,
                expired,
            ]
        )

        candidates = await repository.list_approval_candidates(
            lease_seconds=300, limit=50
        )
        # 期望：ready、stale_approving、null_started_approving；
        # 排除 fresh_approving（租约有效）、short_votes（票数不足）、expired（已过期）
        assert set(candidates) == {ids[0], ids[1], ids[2]}
    finally:
        await _cleanup(ids)
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_release_approval_and_mark_approved_are_token_scoped() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repository = ProposalRepository()
    await repository.initialize()
    approving_id = 0
    try:
        (approving_id,) = await _insert_rows(
            [
                _row(
                    status="approving",
                    approval_token="approval-token",
                    approval_started_at=_now(),
                    vote_count=3,
                    required_votes=3,
                )
            ]
        )
        await repository.release_approval(approving_id, "wrong-token")
        row = await _fetch_row(approving_id)
        assert row is not None
        assert row.status == "approving"

        await repository.release_approval(approving_id, "approval-token")
        row = await _fetch_row(approving_id)
        assert row is not None
        assert row.status == "voting"
        assert row.approval_token is None
        assert row.approval_started_at is None

        re_claimed = await repository.claim_for_approval(
            approving_id, "approval-token-2", lease_seconds=300
        )
        assert re_claimed is not None

        wrong_token = await repository.mark_approved(
            approving_id, 42, "wrong-token"
        )
        assert wrong_token is None

        approved = await repository.mark_approved(
            approving_id, 42, "approval-token-2"
        )
        assert approved is not None
        assert approved.status == "approved"
        assert approved.knowledge_id == 42
        assert approved.approved_at is not None
        assert approved.approval_token is None
        assert approved.approval_started_at is None

        again = await repository.mark_approved(
            approving_id, 42, "approval-token-2"
        )
        assert again is None
    finally:
        await _cleanup([approving_id])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_cleanup_expired_removes_expired_and_stale_records() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repository = ProposalRepository()
    await repository.initialize()
    ids: list[int] = []
    try:
        expired_voting = _row(
            status="voting",
            expired_at=_now() - timedelta(seconds=1),
        )
        active_voting = _row(
            status="voting",
            expired_at=_now() + timedelta(hours=2),
        )
        stale_publishing = _row(
            status="publishing",
            updated_at=_now() - timedelta(days=8),
        )
        recent_publishing = _row(
            status="publishing",
            updated_at=_now() - timedelta(days=1),
        )
        stale_failed = _row(
            status="failed",
            publication_error_code="delivery_unknown",
            updated_at=_now() - timedelta(days=30),
        )
        approved = _row(
            status="approved", updated_at=_now() - timedelta(days=30)
        )
        ids = await _insert_rows(
            [
                expired_voting,
                active_voting,
                stale_publishing,
                recent_publishing,
                stale_failed,
                approved,
            ]
        )

        deleted = await repository.cleanup_expired()
        assert deleted == 3

        remaining = await _fetch_row(ids[1])
        assert remaining is not None
        assert remaining.status == "voting"
        assert await _fetch_row(ids[3]) is not None
        assert await _fetch_row(ids[5]) is not None
        assert await _fetch_row(ids[0]) is None
        assert await _fetch_row(ids[2]) is None
        assert await _fetch_row(ids[4]) is None

        assert await repository.cleanup_expired() == 0
    finally:
        await _cleanup(ids)
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_full_state_machine_flow_via_public_api() -> None:
    """publishing -> failed -> publishing -> voting -> approved 全链路。"""
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repository = ProposalRepository()
    await repository.initialize()
    publication_key = _make_key("flow")
    proposal: Proposal | None = None
    try:
        proposal = await repository.claim_publication(
            publication_key=publication_key,
            publication_token="flow-token-1",
            group_id=100,
            proposer_id=200,
            proposer_name="测试用户",
            title="全链路标题",
            content="全链路正文",
            required_votes=1,
            expire_hours=2,
            lease_seconds=300,
        )
        assert proposal is not None
        assert await repository.mark_publication_failed(
            proposal.id, "flow-token-1", "send_failed"
        ) is not None
        retried = await repository.claim_publication(
            publication_key=publication_key,
            publication_token="flow-token-2",
            group_id=100,
            proposer_id=200,
            proposer_name="测试用户",
            title="全链路标题",
            content="全链路正文",
            required_votes=1,
            expire_hours=2,
            lease_seconds=300,
        )
        assert retried is not None
        assert retried.id == proposal.id
        assert retried.publication_attempts == 2

        completed = await repository.complete_publication(
            proposal.id, 12345, "flow-token-2"
        )
        assert completed is not None
        assert completed.status == "voting"

        voted = await repository.add_vote(proposal.id, "555")
        assert voted is not None
        assert voted.vote_count == 1

        claimed = await repository.claim_for_approval(
            proposal.id, "flow-approval", lease_seconds=300
        )
        assert claimed is not None
        approved = await repository.mark_approved(
            proposal.id, 7, "flow-approval"
        )
        assert approved is not None
        assert approved.status == "approved"
        assert approved.knowledge_id == 7

        listed, total = await repository.list_proposals(
            group_id=100, limit=10, offset=0
        )
        assert total == 1
        assert listed[0].id == proposal.id
        assert listed[0].knowledge_id == 7
    finally:
        await _cleanup([proposal.id] if proposal is not None else [])
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_uninitialized_methods_raise_runtime_error() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repository = ProposalRepository()
    try:
        with pytest.raises(RuntimeError, match="komari_custom 数据库尚未初始化"):
            await repository.cleanup_expired()
        with pytest.raises(RuntimeError, match="komari_custom 数据库尚未初始化"):
            await repository.list_proposals(group_id=100, limit=10, offset=0)
    finally:
        await _reset_shared_orm_engine()


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
@pytest.mark.asyncio
async def test_updated_at_order_in_list_approval_candidates() -> None:
    if not _same_database(POSTGRES_URL, _configured_database_url()):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 nonebot sqlalchemy_database_url 不一致")
    await _reset_shared_orm_engine()
    repository = ProposalRepository()
    await repository.initialize()
    ids: list[int] = []
    try:
        older = _row(
            status="voting",
            vote_count=3,
            required_votes=3,
            updated_at=_now() - timedelta(minutes=10),
        )
        newer = _row(
            status="voting",
            vote_count=3,
            required_votes=3,
            updated_at=_now() - timedelta(minutes=1),
        )
        ids = await _insert_rows([older, newer])
        candidates = await repository.list_approval_candidates(
            lease_seconds=300, limit=50
        )
        assert ids[0] in candidates and ids[1] in candidates
        assert candidates.index(ids[0]) < candidates.index(ids[1])
    finally:
        await _cleanup(ids)
        await _reset_shared_orm_engine()
