"""komari_custom 命令式编辑路径测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from komari_bot.common.content_budget import ContentValidationError
from komari_bot.plugins.komari_custom.models import SessionData
from komari_bot.plugins.komari_custom.proposal_repository import ProposalRepository
from komari_bot.plugins.komari_custom.publication_service import (
    ProposalPublicationDraft,
)
from komari_bot.plugins.komari_custom.session_manager import CustomSessionManager

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CUSTOM_INIT = PROJECT_ROOT / "komari_bot" / "plugins" / "komari_custom" / "__init__.py"


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, *, ex: int) -> bool:
        del ex
        self.values[key] = value
        return True

    async def delete(self, key: str) -> int:
        existed = key in self.values
        self.values.pop(key, None)
        return 1 if existed else 0

    async def execute_command(self, command: str, *args: object) -> int:
        assert command == "EVAL"
        script, key_count, key, expected, replacement, _ttl, expect_missing = args
        assert "custom_session_cas_v1" in str(script)
        assert int(str(key_count)) == 1
        key_text = str(key)
        current = self.values.get(key_text)
        if str(expect_missing) == "1":
            if current is not None:
                return 0
        elif current != str(expected):
            return 0
        self.values[key_text] = str(replacement)
        return 1

    async def close(self) -> None:
        return None


class _FakeConfigManager:
    def get(self) -> object:
        return SimpleNamespace(redis_db=0)


class _BarrierRedis(_FakeRedis):
    def __init__(self) -> None:
        super().__init__()
        self.barrier_enabled = False
        self._barrier_reads = 0
        self._barrier = asyncio.Event()

    async def get(self, key: str) -> str | None:
        captured = self.values.get(key)
        if self.barrier_enabled and self._barrier_reads < 2:
            self._barrier_reads += 1
            if self._barrier_reads == 2:
                self._barrier.set()
            await self._barrier.wait()
        return captured


class _FakeProposalConnection:
    def __init__(self) -> None:
        self.fetchrow_calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetchrow(self, query: str, *args: object) -> None:
        self.fetchrow_calls.append((query, args))


class _FakeProposalAcquire:
    def __init__(self, conn: _FakeProposalConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeProposalConnection:
        return self._conn

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb


class _FakeProposalPool:
    def __init__(self, conn: _FakeProposalConnection) -> None:
        self._conn = conn

    def acquire(self) -> _FakeProposalAcquire:
        return _FakeProposalAcquire(self._conn)


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    redis = _FakeRedis()
    monkeypatch.setattr(CustomSessionManager, "_redis_client", redis)
    return redis


@pytest.fixture
def manager(fake_redis: _FakeRedis) -> CustomSessionManager:
    del fake_redis
    return CustomSessionManager(_FakeConfigManager())


@pytest.mark.asyncio
async def test_new_with_title_creates_session_title(
    manager: CustomSessionManager,
) -> None:
    session = await manager.create_session(100, "200", title=" 标题 ")

    assert session.title == "标题"
    saved = await manager.get_session(100, "200")
    assert saved is not None
    assert saved.title == "标题"
    assert saved.phase == "title"
    assert saved.version == 1


@pytest.mark.asyncio
async def test_concurrent_appends_replay_on_latest_session_without_lost_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _BarrierRedis()
    monkeypatch.setattr(CustomSessionManager, "_redis_client", redis)
    first = CustomSessionManager(_FakeConfigManager())
    second = CustomSessionManager(_FakeConfigManager())
    await first.create_session(100, "200", title="原标题")
    redis.barrier_enabled = True

    await asyncio.gather(
        first.append_text(100, "200", "来自 worker A"),
        second.append_text(100, "200", "来自 worker B"),
    )

    saved = await first.get_session(100, "200")
    assert saved is not None
    assert set(saved.title.splitlines()) == {
        "原标题",
        "来自 worker A",
        "来自 worker B",
    }
    assert saved.version == 3
    assert len(saved.undo_stack) == 2


@pytest.mark.asyncio
async def test_new_without_title_then_append_title(
    manager: CustomSessionManager,
) -> None:
    await manager.create_session(100, "200")

    session = await manager.append_text(100, "200", "标题文本")

    assert session.title == "标题文本"
    assert session.content == ""
    assert session.undo_stack[-1].action == "append"
    assert session.undo_stack[-1].field == "title"


@pytest.mark.asyncio
async def test_confirm_to_content_then_append_content(
    manager: CustomSessionManager,
) -> None:
    await manager.create_session(100, "200", title="标题")
    session = await manager.set_phase(100, "200", "content")

    assert session.phase == "content"

    session = await manager.append_text(100, "200", "正文文本")

    assert session.title == "标题"
    assert session.content == "正文文本"
    assert session.undo_stack[-1].field == "content"


@pytest.mark.asyncio
async def test_append_empty_command_path_is_guarded_by_handler_source() -> None:
    source = CUSTOM_INIT.read_text(encoding="utf-8")

    assert "async def _handle_append" in source
    assert "if not text:" in source
    assert 'await custom_action.finish("❌ 请输入要追加的文本")' in source


@pytest.mark.asyncio
async def test_append_rejected_in_review_phase(
    manager: CustomSessionManager,
) -> None:
    await manager.create_session(100, "200", title="标题")
    await manager.set_phase(100, "200", "content")
    await manager.append_text(100, "200", "正文文本")
    await manager.set_phase(100, "200", "review")

    with pytest.raises(ValueError, match="当前阶段不能追加内容"):
        await manager.append_text(100, "200", "不能写入")

    saved = await manager.get_session(100, "200")
    assert saved is not None
    assert saved.content == "正文文本"


@pytest.mark.asyncio
async def test_oversized_append_keeps_saved_draft_unchanged(
    manager: CustomSessionManager,
) -> None:
    await manager.create_session(100, "200", title="原标题")

    with pytest.raises(ContentValidationError, match="提案标题超过"):
        await manager.append_text(100, "200", "新" * 129)

    saved = await manager.get_session(100, "200")
    assert saved is not None
    assert saved.title == "原标题"
    assert saved.undo_stack == []


@pytest.mark.asyncio
async def test_proposal_content_enforces_estimated_token_budget(
    manager: CustomSessionManager,
) -> None:
    await manager.create_session(100, "200", title="标题")
    await manager.set_phase(100, "200", "content")

    with pytest.raises(ContentValidationError, match="估算 token 上限"):
        await manager.append_text(100, "200", "测" * 4_097)

    saved = await manager.get_session(100, "200")
    assert saved is not None
    assert saved.content == ""


def test_session_model_validates_assignment_and_publication_draft() -> None:
    with pytest.raises(ValidationError, match="提案标题超过"):
        SessionData(title="题" * 129)

    session = SessionData(title="标题")
    with pytest.raises(ValidationError, match="提案正文超过"):
        session.content = "正" * 8_001

    with pytest.raises(ContentValidationError, match="提案标题超过"):
        ProposalPublicationDraft(
            publication_key="key",
            group_id=100,
            proposer_id=200,
            proposer_name="测试用户",
            title="题" * 129,
            content="正文",
            required_votes=3,
            expire_hours=2,
        )


@pytest.mark.asyncio
async def test_legacy_prompt_message_ids_are_ignored(
    fake_redis: _FakeRedis,
    manager: CustomSessionManager,
) -> None:
    key = manager._key(100, "200")
    fake_redis.values[key] = (
        '{"phase":"title","title":"标题","content":"",'
        '"prompt_message_ids":[123],"undo_stack":[],"created_at":"2026-06-08T00:00:00+08:00"}'
    )

    session = await manager.get_session(100, "200")

    assert session is not None
    assert session.title == "标题"
    assert not hasattr(session, "prompt_message_ids")


def test_no_reply_append_entry_or_prompt_tracking_api() -> None:
    source = CUSTOM_INIT.read_text(encoding="utf-8")

    assert "on_message(" not in source
    assert "reply_append" not in source
    assert "_is_custom_prompt_reply" not in source
    assert "remember_prompt_message" not in source
    assert "is_prompt_reply" not in source
    assert not hasattr(CustomSessionManager, "remember_prompt_message")
    assert not hasattr(CustomSessionManager, "is_prompt_reply")
    assert not hasattr(SessionData(), "prompt_message_ids")


def test_append_command_is_registered_and_documented() -> None:
    source = CUSTOM_INIT.read_text(encoding="utf-8")

    assert '("custom", "append")' in source
    assert ".custom append <文本> 向当前标题/正文追加内容" in source
    assert "请回复这条消息" not in source
    assert "回复引导消息" not in source


def test_custom_action_fallback_error_message_hides_exception_detail() -> None:
    source = CUSTOM_INIT.read_text(encoding="utf-8")

    assert 'await custom_action.finish("❌ 处理请求失败，请稍后再试")' in source
    assert 'await custom_action.finish(f"❌ 处理请求失败：{e}")' not in source
    assert "except ContentValidationError as exc:" in source
    assert 'await custom_action.finish(plain_text_message(f"❌ {exc}"))' in source


@pytest.mark.asyncio
async def test_find_proposal_by_keyword_escapes_like_wildcards() -> None:
    conn = _FakeProposalConnection()
    repository = ProposalRepository()
    repository._pool = _FakeProposalPool(conn)  # type: ignore[assignment]

    result = await repository.find_in_group_by_index_or_keyword(
        group_id=100,
        selector=r"100%_x\tag",
        limit_for_index=10,
    )

    query, args = conn.fetchrow_calls[0]
    assert result is None
    assert "title ILIKE $2 ESCAPE '\\'" in query
    assert args == (100, r"%100\%\_x\\tag%")


@pytest.mark.asyncio
async def test_claim_for_approval_is_atomic_and_lease_guarded() -> None:
    conn = _FakeProposalConnection()
    repository = ProposalRepository()
    repository._pool = _FakeProposalPool(conn)  # type: ignore[assignment]

    result = await repository.claim_for_approval(
        9,
        "claim-token",
        lease_seconds=300,
    )

    assert result is None
    query, args = conn.fetchrow_calls[0]
    assert "status = 'approving'" in query
    assert "vote_count >= required_votes" in query
    assert "approval_started_at <" in query
    assert args == (9, "claim-token", 300)


@pytest.mark.asyncio
async def test_publication_message_id_is_saved_in_editing_session(
    manager: CustomSessionManager,
) -> None:
    await manager.create_session(100, "200", title="标题")

    session = await manager.remember_publication_message_id(100, "200", 9988)

    assert session.publication_message_id == 9988
    saved = await manager.get_session(100, "200")
    assert saved is not None
    assert saved.publication_message_id == 9988
