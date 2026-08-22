"""TSK-226 komari_custom 群归属生命周期准入验收（红基线）。

本文件驱动真实 komari_custom 服务对象，用可编程 AdmissionProbe（替换
``group_admission.runtime._runtime``）注入 restricted / admitted 与
effective revision，断言每个效果 seam 前必须消费顶层 ``adjudicate`` 并按裁
决收敛目标行为（休眠、不领取业务租约、不耗 retry、不进 dead-letter、交付
unknown 不自动重发、采纳通知在通知受限时跳过且不补发）。

当前生产 komari_custom 尚未接入统一准入：probe 不会被消费，目标行为不存在，
因此本文件用例按预期红基线失败（red）。测试文件本身可收集、Ruff/Pyright 零
错误；唯一合法 skip 是真实 PG/Redis 缺库的门控用例（见 ``*_gated`` 文件）。
manifest 登记锚点均落在本文件（acceptance_manifest.py 的 TSK-226 行）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from komari_bot.plugins.komari_custom import vote_handler
from komari_bot.plugins.komari_custom.models import Proposal
from komari_bot.plugins.komari_custom.publication_service import (
    ProposalPublicationDraft,
    ProposalPublicationError,
    ProposalPublicationReconciliationRequiredError,
    ProposalPublicationService,
)
from komari_bot.plugins.komari_custom.session_manager import CustomSessionManager
from tests.group_admission.custom_acceptance_support import (
    AdmissionProbe,
    FakeSessionRedis,
    install_admission_probe,
)

# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


def _published_proposal(**overrides: Any) -> Proposal:
    """构造按需覆盖字段的提案记录，供发布/投票/采纳效果断言。"""
    now = datetime.now().astimezone()
    values: dict[str, Any] = {
        "id": 1,
        "publication_key": "key-1",
        "group_id": 100,
        "proposer_id": 200,
        "proposer_name": "投递者",
        "title": "知识提案标题",
        "content": "知识提案正文",
        "status": "voting",
        "required_votes": 3,
        "vote_count": 0,
        "voted_users": [],
        "created_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    return Proposal.model_construct(**values)


class _MemoryPublicationRepository:
    """内存发布仓库：记录认领与 dead-letter，供业务租约断言。"""

    def __init__(self) -> None:
        self.proposal: Proposal | None = None
        self.claimed_ids: list[int] = []
        self.dead_letter: list[int] = []
        self._lock = asyncio.Lock()

    async def get_by_publication_key(self, publication_key: str) -> Proposal | None:
        proposal = self.proposal
        if proposal is None or proposal.publication_key != publication_key:
            return None
        return proposal

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
        del lease_seconds
        async with self._lock:
            now = datetime.now().astimezone()
            if self.proposal is None:
                self.proposal = Proposal(
                    id=1,
                    publication_key=publication_key,
                    publication_token=publication_token,
                    publication_started_at=now,
                    publication_attempts=1,
                    group_id=group_id,
                    proposer_id=proposer_id,
                    proposer_name=proposer_name,
                    title=title,
                    content=content,
                    status="publishing",
                    required_votes=required_votes,
                    created_at=now,
                    updated_at=now,
                    expired_at=now + timedelta(hours=expire_hours),
                )
            elif (
                self.proposal.status == "failed"
                and self.proposal.publication_error_code
                in {"send_rejected", "send_failed"}
            ):
                self.proposal = self.proposal.model_copy(
                    update={
                        "status": "publishing",
                        "publication_token": publication_token,
                        "publication_started_at": now,
                        "publication_attempts": (
                            self.proposal.publication_attempts + 1
                        ),
                        "publication_error_code": None,
                    }
                )
            else:
                return None
            created = self.proposal
            assert created is not None
            self.claimed_ids.append(created.id)
            return created

    async def complete_publication(
        self,
        proposal_id: int,
        message_id: int,
        publication_token: str,
    ) -> Proposal | None:
        if (
            self.proposal is None
            or self.proposal.id != proposal_id
            or self.proposal.status != "publishing"
            or self.proposal.publication_token != publication_token
        ):
            return None
        self.proposal = self.proposal.model_copy(
            update={
                "status": "voting",
                "vote_message_id": message_id,
                "publication_token": None,
                "publication_error_code": None,
            }
        )
        return self.proposal

    async def recover_publication(
        self,
        publication_key: str,
        message_id: int,
    ) -> Proposal | None:
        proposal = self.proposal
        if (
            proposal is None
            or proposal.publication_key != publication_key
            or proposal.status not in {"publishing", "failed"}
        ):
            return None
        self.proposal = proposal.model_copy(
            update={
                "status": "voting",
                "vote_message_id": message_id,
                "publication_token": None,
                "publication_error_code": None,
            }
        )
        return self.proposal

    async def mark_publication_failed(
        self,
        proposal_id: int,
        publication_token: str,
        error_code: str,
    ) -> Proposal | None:
        proposal = self.proposal
        if (
            proposal is None
            or proposal.id != proposal_id
            or proposal.publication_token != publication_token
        ):
            return None
        self.proposal = proposal.model_copy(
            update={
                "status": "failed",
                "publication_token": None,
                "publication_error_code": error_code,
            }
        )
        return self.proposal


def _draft(group_id: int = 100) -> ProposalPublicationDraft:
    """构造合法的发布草稿。"""
    return ProposalPublicationDraft(
        publication_key="stable-publication-key",
        group_id=group_id,
        proposer_id=200,
        proposer_name="投递者",
        title="测试提案",
        content="正文",
        required_votes=3,
        expire_hours=2,
    )


async def _ignore_message_id(_message_id: int) -> None:
    return None


class _KnowledgeSourceConflictError(RuntimeError):
    """知识库 source_key 冲突的测试哨兵异常。"""


class _KnowledgePlugin:
    """记录 add_knowledge 调用，可注入 source_key 冲突。"""

    def __init__(self, *, conflict: bool = False) -> None:
        self.conflict = conflict
        self.add_calls: list[str] = []
        self.next_id = 0

    async def add_knowledge(
        self,
        content: str,
        keywords: list[str],
        category: str,
        notes: str | None = None,
        *,
        source_key: str | None = None,
    ) -> int:
        del content, keywords, category, notes
        if self.conflict:
            raise _KnowledgeSourceConflictError("knowledge_source_conflict")
        self.add_calls.append(source_key or "")
        self.next_id += 1
        return self.next_id


class _VoteRepository:
    """内存提案仓库：为采纳通知与投票去重断言提供最小接口。"""

    def __init__(
        self,
        proposal: Proposal,
        *,
        mark_approval_fails_first: int = 0,
    ) -> None:
        self.proposal = proposal
        self.mark_approval_fails_first = mark_approval_fails_first
        self.replace_votes_calls: list[list[str]] = []

    async def initialize(self) -> None:
        return None

    async def get_by_id(self, proposal_id: int) -> Proposal | None:
        p = self.proposal
        if p is None or p.id != proposal_id:
            return None
        return p

    async def find_by_vote_message_id(self, message_id: int) -> Proposal | None:
        del message_id
        return self.proposal

    async def replace_votes(
        self,
        proposal_id: int,
        voted_users: list[str],
    ) -> Proposal | None:
        del proposal_id
        self.replace_votes_calls.append(voted_users)
        p = self.proposal
        if p is None:
            return None
        self.proposal = p.model_copy(
            update={"voted_users": voted_users, "vote_count": len(voted_users)}
        )
        return self.proposal

    async def claim_for_approval(
        self,
        proposal_id: int,
        approval_token: str,
        *,
        lease_seconds: int,
    ) -> Proposal | None:
        del lease_seconds
        p = self.proposal
        if p is None or p.id != proposal_id or p.status not in {"voting", "approving"}:
            return None
        self.proposal = p.model_copy(
            update={"status": "approving", "approval_token": approval_token}
        )
        return self.proposal

    async def release_approval(self, proposal_id: int, approval_token: str) -> None:
        p = self.proposal
        if (
            p is not None
            and p.id == proposal_id
            and p.status == "approving"
            and p.approval_token == approval_token
        ):
            self.proposal = p.model_copy(
                update={"status": "voting", "approval_token": None}
            )

    async def mark_approved(
        self,
        proposal_id: int,
        knowledge_id: int,
        approval_token: str,
    ) -> Proposal | None:
        p = self.proposal
        if (
            p is None
            or p.id != proposal_id
            or p.status != "approving"
            or p.approval_token != approval_token
        ):
            return None
        if self.mark_approval_fails_first > 0:
            self.mark_approval_fails_first -= 1
            return None
        self.proposal = p.model_copy(
            update={"status": "approved", "knowledge_id": knowledge_id}
        )
        return self.proposal

    async def mark_hold(
        self,
        proposal_id: int,
        approval_token: str,
        hold_code: str,
    ) -> Proposal | None:
        """镜像生产 ``mark_hold``：把认领中提案收敛为运维 closed hold。"""
        del hold_code
        p = self.proposal
        if (
            p is None
            or p.id != proposal_id
            or p.status != "approving"
            or p.approval_token != approval_token
        ):
            return None
        self.proposal = p.model_copy(
            update={"status": "hold", "approval_token": None}
        )
        return self.proposal


def _install_vote_state(
    monkeypatch: pytest.MonkeyPatch,
    *,
    repository: _VoteRepository,
    knowledge: _KnowledgePlugin,
    fetch_users: list[object] | None = None,
) -> _Bot:
    """把投票处理依赖注入真实 ``vote_handler.state`` 并返回新的 Bot 替身。"""

    class _ConfigManager:
        @staticmethod
        def get() -> SimpleNamespace:
            return SimpleNamespace(plugin_enable=True, vote_emoji_id="128077")

    bot = _Bot(fetch_users=fetch_users)
    monkeypatch.setattr(vote_handler.state, "repository", repository)
    monkeypatch.setattr(vote_handler.state, "knowledge_plugin", knowledge)
    monkeypatch.setattr(vote_handler.state, "config_manager", _ConfigManager())
    return bot


class _Bot:
    """记录平台读取/发送调用的最小 OneBot Bot 替身。"""

    self_id = "669293859"

    def __init__(self, fetch_users: list[object] | None = None) -> None:
        self.fetch_users = list(fetch_users or [])
        self.fetch_calls = 0
        self.sent_group_msg: list[dict[str, object]] = []
        self.emoji_like_calls = 0

    async def call_api(self, api: str, **kwargs: object) -> object:
        if api == "fetch_emoji_like":
            self.fetch_calls += 1
            return {"users": self.fetch_users}
        if api == "send_group_msg":
            self.sent_group_msg.append(kwargs)
            return {"message_id": 9900}
        if api == "set_msg_emoji_like":
            self.emoji_like_calls += 1
            return None
        return None


# ---------------------------------------------------------------------------
# AC2 — restricted 不领取业务租约、不耗 retry、不进 dead-letter
# ---------------------------------------------------------------------------


async def test_restricted_claim_does_not_acquire_business_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """受限组发布：proposal 必须先经业务裁决，受限时不领取业务租约。"""
    probe = AdmissionProbe(admitted=False)
    install_admission_probe(monkeypatch, probe)
    repository = _MemoryPublicationRepository()
    service = ProposalPublicationService(repository)
    send_calls = 0

    async def _send(_proposal: Proposal) -> object:
        nonlocal send_calls
        send_calls += 1
        return {"message_id": 1001}

    await service.publish(
        _draft(),
        remembered_message_id=None,
        send_message=_send,
        remember_message_id=_ignore_message_id,
    )

    assert probe.adjudicate_count >= 1, "发布认领前必须消费业务裁决"
    assert repository.proposal is None, "受限组不得落入业务认领"
    assert repository.claimed_ids == []
    assert repository.dead_letter == []
    assert send_calls == 0


# ---------------------------------------------------------------------------
# 平台发送：投票消息不得对受限组投递
# ---------------------------------------------------------------------------


async def test_restricted_vote_message_not_dispatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """受限组发布提案不得向平台发送任何投票消息。"""
    probe = AdmissionProbe(admitted=False)
    install_admission_probe(monkeypatch, probe)
    repository = _MemoryPublicationRepository()
    service = ProposalPublicationService(repository)
    send_calls = 0

    async def _send(_proposal: Proposal) -> object:
        nonlocal send_calls
        send_calls += 1
        return {"message_id": 1002}

    await service.publish(
        _draft(),
        remembered_message_id=None,
        send_message=_send,
        remember_message_id=_ignore_message_id,
    )

    assert send_calls == 0, "受限组不得投递投票消息"
    assert probe.adjudicate_count >= 1


# ---------------------------------------------------------------------------
# 平台读取：表情回应在受限组不触达
# ---------------------------------------------------------------------------


async def test_restricted_emoji_like_not_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """受限组不得主动拉取表情回应（平台读取是独立治理效果）。"""
    probe = AdmissionProbe(admitted=False)
    install_admission_probe(monkeypatch, probe)
    repository = _VoteRepository(_published_proposal())
    knowledge = _KnowledgePlugin()
    bot = _install_vote_state(
        monkeypatch, repository=repository, knowledge=knowledge
    )

    await vote_handler.fetch_and_update_votes(
        cast("Any", bot),
        message_id=555,
        proposal_id=1,
    )

    assert probe.business_calls, "表情读取前必须消费业务裁决"
    assert bot.fetch_calls == 0, "受限组不得拉取表情回应"


# ---------------------------------------------------------------------------
# AC3 — 编辑会话业务时钟冻结，PTTL 不续期
# ---------------------------------------------------------------------------


async def test_restricted_session_edit_session_state_is_frozen_and_no_pttl_renewal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """受限期间编辑会话不写入 Redis，业务存活时钟（PTTL）不续期。"""
    probe = AdmissionProbe(admitted=False)
    install_admission_probe(monkeypatch, probe)
    fake_redis = FakeSessionRedis()
    manager = CustomSessionManager(SimpleNamespace())

    async def _fake_get_client() -> FakeSessionRedis:
        return fake_redis

    monkeypatch.setattr(manager, "_get_client", _fake_get_client)

    session = await manager.create_session(100, "editor", title="受限草稿")

    assert probe.adjudicate_count >= 1, "编辑会话写入前必须消费业务裁决"
    assert session is None, "受限组不得持久化编辑会话"
    assert await fake_redis.pttl(CustomSessionManager._key(100, "editor")) == -2


# ---------------------------------------------------------------------------
# AC7 — publication absence 不推断未发送，unknown 永不自动重发
# ---------------------------------------------------------------------------


async def test_publication_absence_is_unknown_and_never_auto_resends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """发布证据缺失时按 unknown 对待，永不高配送、不自动重发。"""
    probe = AdmissionProbe(admitted=True)
    install_admission_probe(monkeypatch, probe)
    now = datetime.now().astimezone()
    repository = _MemoryPublicationRepository()
    repository.proposal = Proposal.model_construct(
        id=1,
        publication_key="stable-publication-key",
        group_id=100,
        proposer_id=200,
        title="测试提案",
        content="正文",
        status="failed",
        publication_error_code="delivery_unknown",
        vote_message_id=None,
        publication_attempts=1,
        required_votes=3,
        created_at=now,
        updated_at=now,
    )
    service = ProposalPublicationService(repository)
    send_calls = 0

    async def _send(_proposal: Proposal) -> object:
        nonlocal send_calls
        send_calls += 1
        return {"message_id": 123}

    with pytest.raises(ProposalPublicationReconciliationRequiredError):
        await service.publish(
            _draft(),
            remembered_message_id=None,
            send_message=_send,
            remember_message_id=_ignore_message_id,
        )

    assert send_calls == 0, "unknown 永不自动重发"
    assert probe.fact_calls, "publication absence 收尾必须经 fact_finalization 裁决"
    assert repository.dead_letter == []


# ---------------------------------------------------------------------------
# AC8 — migration/runtime 不补发采纳通知
# ---------------------------------------------------------------------------


async def test_no_approval_notification_resend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """受限组对已达标提案只做事实收尾，绝不让采纳通知发送或补发。"""
    probe = AdmissionProbe(admitted=False)
    install_admission_probe(monkeypatch, probe)
    proposal = _published_proposal(
        status="voting",
        vote_count=3,
        required_votes=3,
        group_id=100,
    )
    repository = _VoteRepository(proposal)
    knowledge = _KnowledgePlugin()
    bot = _install_vote_state(
        monkeypatch, repository=repository, knowledge=knowledge
    )

    await vote_handler.approve_if_ready(cast("Any", bot), proposal.id)

    assert probe.adjudicate_count >= 1, "采纳效果必须经业务裁决"
    assert bot.sent_group_msg == [], "受限组不得发送采纳通知，且不得补发"


# ---------------------------------------------------------------------------
# AC6 — knowledge row 一致则完成事实收尾而不重复 add/embedding
# ---------------------------------------------------------------------------


async def test_approved_fact_finalization_no_duplicate_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已提交 knowledge row 的 fact finalization 不得重复 add/embedding。"""
    probe = AdmissionProbe(admitted=True)
    install_admission_probe(monkeypatch, probe)
    proposal = _published_proposal(
        status="voting",
        vote_count=3,
        required_votes=3,
        group_id=100,
        vote_epoch=1,
    )
    repository = _VoteRepository(proposal, mark_approval_fails_first=1)
    knowledge = _KnowledgePlugin()
    bot = _install_vote_state(
        monkeypatch, repository=repository, knowledge=knowledge
    )

    await vote_handler.approve_if_ready(cast("Any", bot), proposal.id)
    await vote_handler.approve_if_ready(cast("Any", bot), proposal.id)

    assert knowledge.add_calls == ["komari_custom:proposal:1"], (
        "同一 source_key 不得重复 add/embedding"
    )


async def test_knowledge_source_conflict_enters_closed_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """knowledge source_key 冲突进入 closed hold 并阻断后续采纳。"""
    probe = AdmissionProbe(admitted=True)
    install_admission_probe(monkeypatch, probe)
    proposal = _published_proposal(
        status="voting",
        vote_count=3,
        required_votes=3,
        group_id=100,
        vote_epoch=1,
    )
    repository = _VoteRepository(proposal)
    knowledge = _KnowledgePlugin(conflict=True)
    bot = _install_vote_state(
        monkeypatch, repository=repository, knowledge=knowledge
    )

    with pytest.raises(_KnowledgeSourceConflictError):
        await vote_handler.approve_if_ready(cast("Any", bot), proposal.id)

    assert repository.proposal.status not in {"voting", "approving"}, (
        "知识冲突必须收敛到 closed hold，而不是可重试的评议状态"
    )


# ---------------------------------------------------------------------------
# AC4 — 同 revision 不重复领取租约，仅 revision 变化才重新裁决（生产路径）
# ---------------------------------------------------------------------------


async def test_publish_no_reclaim_same_revision_and_re_adjudicates_on_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """驱动真实 ProposalPublicationService.publish：同 revision 短截、新 revision 重裁决。

    probe 序列：revision=1 admitted → 同 revision=1 → revision=2 restricted。
    可观察断言：认领次数（repository.claimed_ids）只增长在「新 revision 且
    admitted」的分支上；同 revision 的重复调用不新增认领；revision 变化才触发
    新一轮裁决。
    """
    probe = AdmissionProbe(admitted=True, revision=1)
    install_admission_probe(monkeypatch, probe)
    repository = _MemoryPublicationRepository()
    service = ProposalPublicationService(repository)

    async def _send(_proposal: Proposal) -> object:
        msg = "模拟平台发送失败"
        raise RuntimeError(msg)

    def _definitive(_exc: object) -> bool:
        return True

    async def _attempt_publish() -> None:
        with pytest.raises(ProposalPublicationError):
            await service.publish(
                _draft(),
                remembered_message_id=None,
                send_message=_send,
                remember_message_id=_ignore_message_id,
                is_definitive_send_failure=_definitive,
            )

    # revision=1 admitted：首次认领并进入 failed/send_rejected（可重领）
    await _attempt_publish()
    first_claims = len(repository.claimed_ids)
    adjudications_after_first = probe.adjudicate_count
    assert first_claims == 1
    assert adjudications_after_first >= 1

    # 同 revision（仍为 1，admitted）：不得重复认领业务租约、不得重复裁决
    await _attempt_publish()
    assert len(repository.claimed_ids) == first_claims, "同 revision 不重复领取"
    assert probe.adjudicate_count == adjudications_after_first, (
        "同 revision 不重复触发裁决副作用"
    )

    # revision 变化为 2 且受限：重新裁决为受限，且不认领新租约
    probe.set_admitted(admitted=False, revision=2)
    await _attempt_publish()
    assert probe.adjudicate_count == adjudications_after_first + 1, (
        "新 revision 才重新裁决"
    )
    assert len(repository.claimed_ids) == first_claims, (
        "受限新 revision 不得认领业务租约"
    )


# ---------------------------------------------------------------------------
# AC5 — vote_epoch 生产路径红基线（真实 vote_handler，非自证）
# ---------------------------------------------------------------------------


async def test_restore_rotates_vote_epoch_so_stale_dormant_votes_no_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """恢复后轮换 vote_epoch：休眠期平台累积旧票不得跨入新 epoch 触达采纳。

    已激活轮次（vote_epoch=1）的 voting 提案受限期休眠；恢复准入后全量拉取
    （脚本化 bot 返回休眠期间攒的票）直接覆盖计数。当前生产无轮换触发点，旧票
    直接刷进同一 epoch 并达标采纳 → 红：恢复后旧轮票不得成为新轮达标依据。
    """
    probe = AdmissionProbe(admitted=True)
    install_admission_probe(monkeypatch, probe)
    proposal = _published_proposal(
        status="voting",
        required_votes=2,
        vote_count=1,
        voted_users=["201"],
        vote_epoch=1,
    )
    repository = _VoteRepository(proposal)
    knowledge = _KnowledgePlugin()
    # 注入状态；bot 每次按相位单独构造（脚本化平台回应）。
    _install_vote_state(monkeypatch, repository=repository, knowledge=knowledge)

    # 休眠（受限）期一次业务处理：不触达平台、不刷新投票。
    probe.set_admitted(admitted=False)
    dormant_bot = _Bot(fetch_users=["201", "202", "203"])
    await vote_handler.fetch_and_update_votes(cast("Any", dormant_bot), message_id=1, proposal_id=1)
    assert dormant_bot.fetch_calls == 0, "休眠期不得读取表情回应"

    # 恢复准入：全量拉取会把休眠期间平台累积的旧票刷进同一 epoch（当前即红点）。
    probe.set_admitted(admitted=True)
    restored_bot = _Bot(fetch_users=["201", "202", "203"])
    await vote_handler.fetch_and_update_votes(cast("Any", restored_bot), message_id=1, proposal_id=1)
    assert restored_bot.fetch_calls == 1
    assert repository.proposal.vote_count == 3

    await vote_handler.approve_if_ready(cast("Any", restored_bot), proposal.id)

    assert repository.proposal.status != "approved", (
        "恢复轮换后，休眠期累积旧票不得成为新轮达标依据自动触发采纳"
    )
    assert knowledge.add_calls == [], "休眠期累积旧票不得触发重复 add_knowledge"


async def test_dormancy_epoch_skips_platform_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """休眠（受限）期间表情读取不触达平台、不刷新投票。"""
    probe = AdmissionProbe(admitted=False)
    install_admission_probe(monkeypatch, probe)
    repository = _VoteRepository(_published_proposal())
    knowledge = _KnowledgePlugin()
    bot = _install_vote_state(
        monkeypatch, repository=repository, knowledge=knowledge
    )

    await vote_handler.fetch_and_update_votes(cast("Any", bot), message_id=1, proposal_id=1)

    assert bot.fetch_calls == 0, "受限（休眠）不得读取表情回应"
    assert repository.replace_votes_calls == []


async def test_same_epoch_voter_dedup_through_production_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一 voter 在同一 epoch 重复回应只计一票（经真实 fetch_and_update_votes）。"""
    probe = AdmissionProbe(admitted=True)
    install_admission_probe(monkeypatch, probe)
    repository = _VoteRepository(_published_proposal())
    knowledge = _KnowledgePlugin()
    bot = _install_vote_state(
        monkeypatch,
        repository=repository,
        knowledge=knowledge,
        fetch_users=["101", "101", "102"],
    )

    await vote_handler.fetch_and_update_votes(cast("Any", bot), message_id=1, proposal_id=1)

    assert repository.replace_votes_calls == [["101", "102"]]
    assert repository.proposal.vote_count == 2


async def test_cross_epoch_voter_reeligible_through_production_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上次用 epoch 投过票的 voter，新的 epoch 仍可重新计入（去重按 epoch 作用域）。"""
    probe = AdmissionProbe(admitted=True)
    install_admission_probe(monkeypatch, probe)
    repository = _VoteRepository(
        _published_proposal(voted_users=["201"], vote_count=1)
    )
    knowledge = _KnowledgePlugin()
    bot = _install_vote_state(
        monkeypatch,
        repository=repository,
        knowledge=knowledge,
        fetch_users=["201", "202"],
    )

    await vote_handler.fetch_and_update_votes(cast("Any", bot), message_id=1, proposal_id=1)

    assert repository.replace_votes_calls == [["201", "202"]]
    assert repository.proposal.vote_count == 2
