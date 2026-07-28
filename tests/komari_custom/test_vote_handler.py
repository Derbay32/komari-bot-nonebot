"""自定义知识提案投票同步测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

import pytest

from komari_bot.plugins.komari_custom import vote_handler

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import Bot, NoticeEvent

class _ConfigManager:
    @staticmethod
    def get() -> object:
        return SimpleNamespace(plugin_enable=True, vote_emoji_id="128077")


class _Bot:
    self_id = "669293859"

    def __init__(self, users: list[object]) -> None:
        self.users = users
        self.sent_messages: list[dict[str, object]] = []

    async def call_api(self, api: str, **kwargs: object) -> object:
        if api == "send_group_msg":
            self.sent_messages.append(kwargs)
            return {"message_id": 1}
        return {"users": self.users}


class _Repository:
    def __init__(self) -> None:
        self.replaced_votes: list[list[str]] = []
        self.proposal = SimpleNamespace(
            id=1,
            status="voting",
            proposer_id=10001,
            vote_count=1,
            required_votes=3,
        )

    async def initialize(self) -> None:
        return None

    async def find_by_vote_message_id(self, _message_id: int) -> object:
        return self.proposal

    async def get_by_id(self, _proposal_id: int) -> object:
        return self.proposal

    async def replace_votes(
        self,
        _proposal_id: int,
        voted_users: list[str],
    ) -> object:
        self.replaced_votes.append(voted_users)
        self.proposal = SimpleNamespace(
            **{
                **vars(self.proposal),
                "vote_count": len(voted_users),
                "voted_users": voted_users,
            }
        )
        return self.proposal


@pytest.mark.asyncio
async def test_fetch_votes_replaces_stale_count_with_empty_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    monkeypatch.setattr(vote_handler.state, "repository", repository)
    monkeypatch.setattr(vote_handler.state, "config_manager", _ConfigManager())

    result = await vote_handler.fetch_and_update_votes(
        cast("Bot", cast("Any", _Bot([]))),
        message_id=123,
        proposal_id=1,
    )

    assert result is not None
    assert repository.replaced_votes == [[]]
    assert result.vote_count == 0


@pytest.mark.asyncio
async def test_fetch_votes_excludes_proposer_bot_and_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    monkeypatch.setattr(vote_handler.state, "repository", repository)
    monkeypatch.setattr(vote_handler.state, "config_manager", _ConfigManager())
    bot = _Bot(
        [
            {"user_id": "10001"},
            {"user_id": "669293859"},
            {"user_id": "10002"},
            {"user_id": "10002"},
        ]
    )

    await vote_handler.fetch_and_update_votes(
        cast("Bot", cast("Any", bot)),
        message_id=123,
        proposal_id=1,
    )

    assert repository.replaced_votes == [["10002"]]


@pytest.mark.asyncio
async def test_emoji_notice_uses_authoritative_snapshot_without_speculative_vote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    monkeypatch.setattr(vote_handler.state, "repository", repository)
    monkeypatch.setattr(vote_handler.state, "config_manager", _ConfigManager())
    monkeypatch.setattr(vote_handler.state, "knowledge_plugin", None)
    event = SimpleNamespace(
        notice_type="group_msg_emoji_like",
        message_id=123,
        group_id=456,
        user_id=10002,
        sub_type="remove",
        emoji_id="128077",
    )

    await vote_handler.handle_emoji_like(
        cast("Bot", cast("Any", _Bot([]))),
        cast("NoticeEvent", event),
    )

    assert repository.replaced_votes == [[]]


@pytest.mark.asyncio
async def test_concurrent_approval_only_writes_one_knowledge_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = SimpleNamespace(
        id=9,
        status="voting",
        proposer_id=10001,
        title="并发提案",
        content="只能写入一次",
        vote_count=3,
        required_votes=3,
        group_id=456,
    )

    class _ApprovalRepository:
        def __init__(self) -> None:
            self.claimed = False
            self.mark_calls: list[tuple[int, int, str]] = []

        async def get_by_id(self, _proposal_id: int) -> object:
            return proposal

        async def claim_for_approval(
            self,
            _proposal_id: int,
            _approval_token: str,
            *,
            lease_seconds: int,
        ) -> object | None:
            assert lease_seconds == vote_handler.APPROVAL_LEASE_SECONDS
            if self.claimed:
                return None
            self.claimed = True
            return SimpleNamespace(**{**vars(proposal), "status": "approving"})

        async def mark_approved(
            self,
            proposal_id: int,
            knowledge_id: int,
            approval_token: str,
        ) -> object:
            self.mark_calls.append((proposal_id, knowledge_id, approval_token))
            return SimpleNamespace(**{**vars(proposal), "status": "approved"})

        async def release_approval(
            self,
            _proposal_id: int,
            _approval_token: str,
        ) -> None:
            return None

    class _KnowledgePlugin:
        def __init__(self) -> None:
            self.source_keys: list[str | None] = []

        async def add_knowledge(self, **kwargs: object) -> int:
            self.source_keys.append(cast("str | None", kwargs.get("source_key")))
            await asyncio.sleep(0)
            return 88

    repository = _ApprovalRepository()
    knowledge_plugin = _KnowledgePlugin()
    bot = _Bot([])
    monkeypatch.setattr(vote_handler.state, "repository", repository)
    monkeypatch.setattr(vote_handler.state, "config_manager", _ConfigManager())
    monkeypatch.setattr(vote_handler.state, "knowledge_plugin", knowledge_plugin)

    await asyncio.gather(
        vote_handler.approve_if_ready(cast("Bot", cast("Any", bot)), 9),
        vote_handler.approve_if_ready(cast("Bot", cast("Any", bot)), 9),
    )

    assert knowledge_plugin.source_keys == ["komari_custom:proposal:9"]
    assert len(repository.mark_calls) == 1
    assert len(bot.sent_messages) == 1


@pytest.mark.asyncio
async def test_failed_knowledge_write_releases_approval_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = SimpleNamespace(
        id=10,
        status="voting",
        proposer_id=10001,
        title="失败提案",
        content="等待重试",
        vote_count=3,
        required_votes=3,
        group_id=456,
    )
    repository = SimpleNamespace(
        get_by_id=AsyncMock(return_value=proposal),
        claim_for_approval=AsyncMock(
            return_value=SimpleNamespace(**{**vars(proposal), "status": "approving"})
        ),
        mark_approved=AsyncMock(),
        release_approval=AsyncMock(),
    )
    knowledge_plugin = SimpleNamespace(
        add_knowledge=AsyncMock(side_effect=RuntimeError("模拟知识写入失败"))
    )
    monkeypatch.setattr(vote_handler.state, "repository", repository)
    monkeypatch.setattr(vote_handler.state, "config_manager", _ConfigManager())
    monkeypatch.setattr(vote_handler.state, "knowledge_plugin", knowledge_plugin)

    with pytest.raises(RuntimeError, match="模拟知识写入失败"):
        await vote_handler.approve_if_ready(
            cast("Bot", cast("Any", _Bot([]))),
            10,
        )

    repository.release_approval.assert_awaited_once()
    repository.mark_approved.assert_not_awaited()


@pytest.mark.asyncio
async def test_periodic_recovery_processes_candidates_and_isolates_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statuses = {9: "approving", 10: "voting"}

    class _RecoveryRepository:
        async def initialize(self) -> None:
            return None

        async def list_approval_candidates(
            self,
            *,
            lease_seconds: int,
            limit: int,
        ) -> list[int]:
            assert lease_seconds == vote_handler.APPROVAL_LEASE_SECONDS
            assert limit == vote_handler.APPROVAL_RECOVERY_BATCH_SIZE
            return [9, 10]

        async def get_by_id(self, proposal_id: int) -> object:
            return SimpleNamespace(status=statuses[proposal_id])

    async def _fake_approve(_bot: object, proposal_id: int) -> None:
        if proposal_id == 10:
            raise RuntimeError("单条恢复失败")
        statuses[proposal_id] = "approved"

    bot = _Bot([])
    monkeypatch.setattr(vote_handler.state, "repository", _RecoveryRepository())
    monkeypatch.setattr(vote_handler.state, "config_manager", _ConfigManager())
    monkeypatch.setattr(vote_handler.state, "knowledge_plugin", object())
    monkeypatch.setattr(vote_handler, "get_bots", lambda: {bot.self_id: bot})
    monkeypatch.setattr(vote_handler, "approve_if_ready", _fake_approve)

    completed = await vote_handler.recover_pending_approvals()

    assert completed == 1
    assert statuses == {9: "approved", 10: "voting"}
