"""自定义知识提案投票同步测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

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

    async def call_api(self, _api: str, **_kwargs: object) -> object:
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
