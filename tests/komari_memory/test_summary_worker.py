"""KomariMemory 总结 worker 编排测试。"""

import asyncio
import importlib.util
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nonebot.plugin
import pytest

from komari_bot.plugins.komari_memory.config_schema import KomariMemoryConfigSchema
from komari_bot.plugins.komari_memory.services.redis_manager import MessageSchema

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = PROJECT_ROOT / "komari_bot/plugins/komari_memory/handlers/summary_worker.py"
PACKAGE_ROOT = PROJECT_ROOT / "komari_bot/plugins/komari_memory"


def _load_summary_worker_module(monkeypatch: Any) -> Any:
    async def _finalize_collector(*_args: object, **_kwargs: object) -> bool:
        return True

    def _fake_require(name: str) -> object:
        if name == "character_binding":
            return types.SimpleNamespace(
                get_character_name=lambda user_id, fallback_nickname="": fallback_nickname
                or user_id,
                refresh_if_file_updated=lambda: False,
            )
        if name == "llm_provider":
            return types.SimpleNamespace(generate_text=lambda **_kwargs: None)
        if name == "agent_run_logger":
            return types.SimpleNamespace(
                create_collector=lambda **_kwargs: None,
                finalize_collector=_finalize_collector,
            )
        return object()

    monkeypatch.setattr(nonebot.plugin, "require", _fake_require)

    memory_package = types.ModuleType("komari_bot.plugins.komari_memory")
    memory_package.__path__ = [str(PACKAGE_ROOT)]  # type: ignore[attr-defined]
    handlers_package = types.ModuleType("komari_bot.plugins.komari_memory.handlers")
    handlers_package.__path__ = [str(PACKAGE_ROOT / "handlers")]  # type: ignore[attr-defined]

    original_memory_package = sys.modules.get("komari_bot.plugins.komari_memory")
    original_handlers_package = sys.modules.get("komari_bot.plugins.komari_memory.handlers")
    original_module = sys.modules.get("komari_bot.plugins.komari_memory.handlers.summary_worker")

    sys.modules["komari_bot.plugins.komari_memory"] = memory_package
    sys.modules["komari_bot.plugins.komari_memory.handlers"] = handlers_package
    try:
        spec = importlib.util.spec_from_file_location(
            "komari_bot.plugins.komari_memory.handlers.summary_worker",
            MODULE_PATH,
        )
        if spec is None or spec.loader is None:
            raise AssertionError

        module = importlib.util.module_from_spec(spec)
        sys.modules["komari_bot.plugins.komari_memory.handlers.summary_worker"] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if original_module is not None:
            sys.modules["komari_bot.plugins.komari_memory.handlers.summary_worker"] = original_module
        else:
            sys.modules.pop("komari_bot.plugins.komari_memory.handlers.summary_worker", None)

        if original_handlers_package is not None:
            sys.modules["komari_bot.plugins.komari_memory.handlers"] = original_handlers_package
        else:
            sys.modules.pop("komari_bot.plugins.komari_memory.handlers", None)

        if original_memory_package is not None:
            sys.modules["komari_bot.plugins.komari_memory"] = original_memory_package
        else:
            sys.modules.pop("komari_bot.plugins.komari_memory", None)


class _FakeRedis:
    def __init__(self, messages: list[MessageSchema]) -> None:
        self._messages = messages
        self.redis = object()
        self.config = KomariMemoryConfigSchema(
            summary_max_buffer_size=100,
            conversation_processing_lease_seconds=120,
        )
        self.snapshot_calls: list[dict[str, str]] = []
        self.delete_processing_calls: list[dict[str, str]] = []
        self.restore_processing_calls: list[dict[str, str]] = []
        self.dead_letter_calls: list[dict[str, Any]] = []
        self.update_last_summary_calls: list[str] = []
        self.chunk_state: dict[str, str] = {}
        self.current_owner: str | None = None

    async def claim_conversation_buffer(
        self,
        group_id: str,
        owner_token: str,
        token: str,
    ) -> SimpleNamespace:
        self.snapshot_calls.append({"group_id": group_id, "token": token})
        if not self._messages:
            return SimpleNamespace(status="empty", processing_key=None)
        self.current_owner = owner_token
        return SimpleNamespace(
            status="claimed",
            processing_key=f"komari_memory:buffer:processing:{group_id}:{token}",
        )

    async def get_processing_conversation_buffer(
        self,
        group_id: str,
        processing_key: str,
        owner_token: str,
    ) -> list[MessageSchema]:
        del group_id, processing_key
        assert owner_token == self.current_owner
        return list(self._messages)

    async def renew_processing_conversation_lease(
        self,
        group_id: str,
        processing_key: str,
        owner_token: str,
    ) -> bool:
        del group_id, processing_key
        return owner_token == self.current_owner

    async def ack_processing_conversation_buffer(
        self,
        group_id: str,
        processing_key: str,
        owner_token: str,
    ) -> bool:
        if owner_token != self.current_owner:
            return False
        self.delete_processing_calls.append({"group_id": group_id, "processing_key": processing_key})
        self.current_owner = None
        self.chunk_state.clear()
        return True

    async def restore_processing_conversation_buffer(
        self,
        group_id: str,
        processing_key: str,
        owner_token: str,
    ) -> bool:
        if owner_token != self.current_owner:
            return False
        self.restore_processing_calls.append({"group_id": group_id, "processing_key": processing_key})
        self.current_owner = None
        self.chunk_state.clear()
        return True

    async def dead_letter_processing_conversation_buffer(
        self,
        group_id: str,
        processing_key: str,
        owner_token: str,
        *,
        failure_code: str,
        attempt_count: int,
    ) -> bool:
        if owner_token != self.current_owner:
            return False
        self.dead_letter_calls.append(
            {
                "group_id": group_id,
                "processing_key": processing_key,
                "failure_code": failure_code,
                "attempt_count": attempt_count,
            }
        )
        self.current_owner = None
        return True

    async def initialize_conversation_chunk_manifest(
        self,
        *,
        group_id: str,
        processing_key: str,
        owner_token: str,
        manifest_json: str,
    ) -> str:
        del group_id, processing_key
        assert owner_token == self.current_owner
        return self.chunk_state.setdefault("manifest", manifest_json)

    async def get_conversation_chunk_state(
        self,
        *,
        group_id: str,
        processing_key: str,
        owner_token: str,
        field: str,
    ) -> str | None:
        del group_id, processing_key
        assert owner_token == self.current_owner
        return self.chunk_state.get(field)

    async def set_conversation_chunk_state(
        self,
        *,
        group_id: str,
        processing_key: str,
        owner_token: str,
        field: str,
        value: str,
    ) -> None:
        del group_id, processing_key
        assert owner_token == self.current_owner
        self.chunk_state[field] = value

    async def update_last_summary(self, group_id: str) -> None:
        self.update_last_summary_calls.append(group_id)


class _FakeMemory:
    def __init__(
        self,
        *,
        profiles: dict[tuple[str, str], dict[str, Any]] | None = None,
        interactions: dict[tuple[str, str], dict[str, Any]] | None = None,
        duplicate_dedup_keys: set[str] | None = None,
    ) -> None:
        self._profiles = profiles or {}
        self._interactions = interactions or {}
        self._duplicate_dedup_keys = duplicate_dedup_keys or set()
        self.store_conversation_calls: list[dict[str, Any]] = []
        self.upsert_user_profile_calls: list[dict[str, Any]] = []
        self.upsert_interaction_history_calls: list[dict[str, Any]] = []

    async def get_user_profile(self, *, user_id: str, group_id: str) -> dict[str, Any] | None:
        return self._profiles.get((group_id, user_id))

    async def get_interaction_history(
        self,
        *,
        user_id: str,
        group_id: str,
    ) -> dict[str, Any] | None:
        return self._interactions.get((group_id, user_id))

    async def store_conversation(
        self,
        *,
        group_id: str,
        summary: str,
        participants: list[str],
        importance_initial: int = 3,
        dedup_key: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> int | None:
        self.store_conversation_calls.append(
            {
                "group_id": group_id,
                "summary": summary,
                "participants": participants,
                "importance_initial": importance_initial,
                "dedup_key": dedup_key,
                "start_time": start_time,
                "end_time": end_time,
            }
        )
        if dedup_key in self._duplicate_dedup_keys:
            return None
        return len(self.store_conversation_calls)

    async def upsert_user_profile(
        self,
        *,
        user_id: str,
        group_id: str,
        profile: dict[str, Any],
        importance: int = 4,
    ) -> None:
        self.upsert_user_profile_calls.append(
            {
                "user_id": user_id,
                "group_id": group_id,
                "profile": profile,
                "importance": importance,
            }
        )

    async def upsert_interaction_history(
        self,
        *,
        user_id: str,
        group_id: str,
        interaction: dict[str, Any],
        importance: int = 5,
    ) -> None:
        self.upsert_interaction_history_calls.append(
            {
                "user_id": user_id,
                "group_id": group_id,
                "interaction": interaction,
                "importance": importance,
            }
        )


def _make_message(
    *,
    content: str = "今天一起吃拉面吧",
    user_id: str = "10001",
    user_nickname: str = "阿明",
    group_id: str = "114514",
    is_bot: bool = False,
    timestamp: float = 1.0,
) -> MessageSchema:
    return MessageSchema(
        user_id=user_id,
        user_nickname=user_nickname,
        group_id=group_id,
        content=content,
        timestamp=timestamp,
        message_id=f"msg-{user_id}",
        is_bot=is_bot,
    )


def _profile_agent_result(*, changed_user_ids: set[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        committed_count=len(changed_user_ids or set()),
        staged_count=len(changed_user_ids or set()),
        summary="画像 Agent 完成",
        status="committed",
        changed_user_ids=changed_user_ids or set(),
    )


def test_perform_summary_runs_profile_agent_before_storing_memories(monkeypatch: Any) -> None:
    module = _load_summary_worker_module(monkeypatch)
    monkeypatch.setattr(
        module,
        "get_config",
        lambda: KomariMemoryConfigSchema(summary_max_buffer_size=100, profile_trait_limit=20),
    )
    events: list[str] = []

    async def _fake_summarize_conversation(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args
        events.append("summary")
        assert kwargs["participants"] == ["10001"]
        assert kwargs["display_name_map"] == {"10001": "阿明"}
        return {
            "memories": [
                {"content": "大家讨论了周末吃拉面。", "importance": 4},
                {"content": "阿明提到最近在追新番。", "importance": 3},
            ]
        }

    async def _fake_run_profile_agent(**kwargs: Any) -> SimpleNamespace:
        del kwargs
        events.append("profile_agent")
        return _profile_agent_result()

    monkeypatch.setattr(module, "summarize_conversation", _fake_summarize_conversation)
    monkeypatch.setattr(module, "run_profile_agent", _fake_run_profile_agent)

    redis = _FakeRedis([_make_message()])
    memory = _FakeMemory()

    asyncio.run(module.perform_summary("114514", redis, memory))

    assert events == ["summary", "profile_agent"]
    assert [call["summary"] for call in memory.store_conversation_calls] == [
        "大家讨论了周末吃拉面。",
        "阿明提到最近在追新番。",
    ]
    assert [call["importance_initial"] for call in memory.store_conversation_calls] == [4, 3]
    assert all(call["dedup_key"] for call in memory.store_conversation_calls)
    assert len({call["dedup_key"] for call in memory.store_conversation_calls}) == 2
    assert memory.upsert_interaction_history_calls == []
    assert redis.delete_processing_calls[0]["group_id"] == "114514"
    assert redis.update_last_summary_calls == ["114514"]


def test_perform_summary_persists_real_message_time_range(monkeypatch: Any) -> None:
    module = _load_summary_worker_module(monkeypatch)
    monkeypatch.setattr(
        module,
        "get_config",
        lambda: KomariMemoryConfigSchema(
            summary_max_buffer_size=100,
            profile_trait_limit=20,
        ),
    )

    async def _fake_summarize_conversation(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {"memories": [{"content": "真实时间范围摘要。", "importance": 4}]}

    async def _fake_run_profile_agent(**kwargs: Any) -> SimpleNamespace:
        del kwargs
        return _profile_agent_result()

    monkeypatch.setattr(module, "summarize_conversation", _fake_summarize_conversation)
    monkeypatch.setattr(module, "run_profile_agent", _fake_run_profile_agent)
    redis = _FakeRedis(
        [
            _make_message(timestamp=1_700_003_600.0),
            _make_message(user_id="10002", timestamp=1_700_000_000.0),
        ]
    )
    memory = _FakeMemory()

    asyncio.run(module.perform_summary("114514", redis, memory))

    assert memory.store_conversation_calls[0]["start_time"] == datetime.fromtimestamp(
        1_700_000_000.0,
        UTC,
    ).replace(tzinfo=None)
    assert memory.store_conversation_calls[0]["end_time"] == datetime.fromtimestamp(
        1_700_003_600.0,
        UTC,
    ).replace(tzinfo=None)


def test_summary_dedup_key_is_stable_for_same_processing_snapshot(monkeypatch: Any) -> None:
    module = _load_summary_worker_module(monkeypatch)
    messages = [_make_message(), _make_message(content="晚上看新番", user_id="10002")]

    first_fingerprint = module._build_processing_snapshot_fingerprint("114514", messages)
    second_fingerprint = module._build_processing_snapshot_fingerprint("114514", list(messages))

    assert first_fingerprint == second_fingerprint
    assert len(first_fingerprint) == 64
    assert module._build_summary_dedup_key(
        "114514",
        first_fingerprint,
        0,
    ) == module._build_summary_dedup_key(
        "114514",
        second_fingerprint,
        0,
    )
    assert module._build_summary_dedup_key(
        "114514",
        first_fingerprint,
        0,
    ) != module._build_summary_dedup_key(
        "114514",
        first_fingerprint,
        1,
    )


def test_perform_summary_skips_duplicate_conversation_ids(monkeypatch: Any) -> None:
    module = _load_summary_worker_module(monkeypatch)
    monkeypatch.setattr(
        module,
        "get_config",
        lambda: KomariMemoryConfigSchema(summary_max_buffer_size=100, profile_trait_limit=20),
    )
    messages = [_make_message()]
    snapshot_fingerprint = module._build_processing_snapshot_fingerprint("114514", messages)
    duplicate_key = module._build_summary_dedup_key("114514", snapshot_fingerprint, 0)

    async def _fake_summarize_conversation(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {
            "memories": [
                {"content": "第一条在之前尝试中已经落库。", "importance": 4},
                {"content": "第二条本次继续落库。", "importance": 3},
            ]
        }

    async def _fake_run_profile_agent(**kwargs: Any) -> SimpleNamespace:
        del kwargs
        return _profile_agent_result()

    monkeypatch.setattr(module, "summarize_conversation", _fake_summarize_conversation)
    monkeypatch.setattr(module, "run_profile_agent", _fake_run_profile_agent)

    redis = _FakeRedis(messages)
    memory = _FakeMemory(duplicate_dedup_keys={duplicate_key})

    asyncio.run(module.perform_summary("114514", redis, memory))

    assert [call["summary"] for call in memory.store_conversation_calls] == [
        "第一条在之前尝试中已经落库。",
        "第二条本次继续落库。",
    ]
    assert memory.store_conversation_calls[0]["dedup_key"] == duplicate_key
    assert redis.delete_processing_calls[0]["group_id"] == "114514"
    assert redis.update_last_summary_calls == ["114514"]


def test_processing_retry_uses_same_dedup_keys_for_partial_success(
    monkeypatch: Any,
) -> None:
    module = _load_summary_worker_module(monkeypatch)
    monkeypatch.setattr(
        module,
        "get_config",
        lambda: KomariMemoryConfigSchema(summary_max_buffer_size=100, profile_trait_limit=20),
    )

    async def _fake_sleep(_delay: float) -> None:
        return None

    retry_module = sys.modules["komari_bot.plugins.komari_memory.core.retry"]
    monkeypatch.setattr(retry_module.asyncio, "sleep", _fake_sleep)

    async def _fake_summarize_conversation(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {
            "memories": [
                {"content": "A 已写入。", "importance": 4},
                {"content": "B 已写入。", "importance": 4},
                {"content": "C 首次失败后重试写入。", "importance": 4},
            ]
        }

    async def _fake_run_profile_agent(**kwargs: Any) -> SimpleNamespace:
        del kwargs
        return _profile_agent_result()

    class _RetryMemory(_FakeMemory):
        def __init__(self) -> None:
            super().__init__()
            self._seen_dedup_keys: set[str] = set()
            self._failed_once = False

        async def store_conversation(
            self,
            *,
            group_id: str,
            summary: str,
            participants: list[str],
            importance_initial: int = 3,
            dedup_key: str | None = None,
            start_time: datetime | None = None,
            end_time: datetime | None = None,
        ) -> int | None:
            self.store_conversation_calls.append(
                {
                    "group_id": group_id,
                    "summary": summary,
                    "participants": participants,
                    "importance_initial": importance_initial,
                    "dedup_key": dedup_key,
                    "start_time": start_time,
                    "end_time": end_time,
                }
            )
            if dedup_key in self._seen_dedup_keys:
                return None
            self._seen_dedup_keys.add(str(dedup_key))
            if summary == "C 首次失败后重试写入。" and not self._failed_once:
                self._failed_once = True
                raise RuntimeError("模拟部分写入后的失败")
            return len(self._seen_dedup_keys)

    monkeypatch.setattr(module, "summarize_conversation", _fake_summarize_conversation)
    monkeypatch.setattr(module, "run_profile_agent", _fake_run_profile_agent)

    redis = _FakeRedis([_make_message()])
    memory = _RetryMemory()

    asyncio.run(module.perform_summary("114514", redis, memory))

    assert [call["summary"] for call in memory.store_conversation_calls] == [
        "A 已写入。",
        "B 已写入。",
        "C 首次失败后重试写入。",
        "A 已写入。",
        "B 已写入。",
        "C 首次失败后重试写入。",
    ]
    assert memory.store_conversation_calls[0]["dedup_key"] == memory.store_conversation_calls[3][
        "dedup_key"
    ]
    assert memory.store_conversation_calls[1]["dedup_key"] == memory.store_conversation_calls[4][
        "dedup_key"
    ]
    assert len(redis.delete_processing_calls) == 1
    assert redis.delete_processing_calls[0]["group_id"] == "114514"


def test_perform_summary_dead_letters_snapshot_when_summary_fails(
    monkeypatch: Any,
) -> None:
    module = _load_summary_worker_module(monkeypatch)
    monkeypatch.setattr(
        module,
        "get_config",
        lambda: KomariMemoryConfigSchema(summary_max_buffer_size=100, profile_trait_limit=20),
    )
    events: list[str] = []

    async def _fake_sleep(_delay: float) -> None:
        return None

    retry_module = sys.modules["komari_bot.plugins.komari_memory.core.retry"]
    monkeypatch.setattr(retry_module.asyncio, "sleep", _fake_sleep)

    async def _fake_summarize_conversation(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        events.append("summary")
        raise RuntimeError("总结失败")

    async def _fake_run_profile_agent(**kwargs: Any) -> SimpleNamespace:
        del kwargs
        events.append("profile_agent")
        return _profile_agent_result()

    monkeypatch.setattr(module, "summarize_conversation", _fake_summarize_conversation)
    monkeypatch.setattr(module, "run_profile_agent", _fake_run_profile_agent)

    redis = _FakeRedis([_make_message()])
    memory = _FakeMemory()

    with pytest.raises(RuntimeError, match="总结失败"):
        asyncio.run(module.perform_summary("114514", redis, memory))

    assert events == ["summary", "summary", "summary"]
    assert memory.store_conversation_calls == []
    assert redis.delete_processing_calls == []
    assert redis.update_last_summary_calls == []
    assert redis.restore_processing_calls == []
    assert len(redis.dead_letter_calls) == 1
    assert redis.dead_letter_calls[0]["group_id"] == "114514"
    assert redis.dead_letter_calls[0]["failure_code"] == "RuntimeError"
    assert redis.dead_letter_calls[0]["attempt_count"] == 3


def test_retry_logs_only_error_type(monkeypatch: Any) -> None:
    _load_summary_worker_module(monkeypatch)
    retry_module = sys.modules["komari_bot.plugins.komari_memory.core.retry"]
    logs: list[tuple[object, tuple[object, ...]]] = []

    async def _fake_sleep(_delay: float) -> None:
        return None

    async def _always_fail() -> None:
        raise RuntimeError("绝不能进入日志的用户私密正文")

    monkeypatch.setattr(retry_module.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(
        retry_module.logger,
        "warning",
        lambda message, *args: logs.append((message, args)),
    )
    monkeypatch.setattr(
        retry_module.logger,
        "error",
        lambda message, *args: logs.append((message, args)),
    )
    retried = retry_module.retry_async(max_attempts=2, base_delay=0)(_always_fail)

    with pytest.raises(RuntimeError, match="用户私密正文"):
        asyncio.run(retried())

    serialized_logs = repr(logs)
    assert "RuntimeError" in serialized_logs
    assert "绝不能进入日志的用户私密正文" not in serialized_logs


def test_perform_summary_dead_letters_snapshot_when_summary_has_no_valid_memory(
    monkeypatch: Any,
) -> None:
    module = _load_summary_worker_module(monkeypatch)
    monkeypatch.setattr(
        module,
        "get_config",
        lambda: KomariMemoryConfigSchema(
            summary_max_buffer_size=100,
            profile_trait_limit=20,
        ),
    )
    summary_calls = 0
    profile_calls = 0

    async def _fake_sleep(_delay: float) -> None:
        return None

    retry_module = sys.modules["komari_bot.plugins.komari_memory.core.retry"]
    monkeypatch.setattr(retry_module.asyncio, "sleep", _fake_sleep)

    async def _fake_summarize_conversation(
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        nonlocal summary_calls
        del args, kwargs
        summary_calls += 1
        return {"memories": [{"content": "   "}, "无效条目"]}

    async def _fake_run_profile_agent(**kwargs: Any) -> SimpleNamespace:
        nonlocal profile_calls
        del kwargs
        profile_calls += 1
        return _profile_agent_result()

    monkeypatch.setattr(module, "summarize_conversation", _fake_summarize_conversation)
    monkeypatch.setattr(module, "run_profile_agent", _fake_run_profile_agent)

    redis = _FakeRedis([_make_message()])
    memory = _FakeMemory()

    with pytest.raises(module.InvalidSummaryResultError):
        asyncio.run(module.perform_summary("114514", redis, memory))

    assert summary_calls == 3
    assert profile_calls == 0
    assert memory.store_conversation_calls == []
    assert redis.delete_processing_calls == []
    assert redis.update_last_summary_calls == []
    assert redis.restore_processing_calls == []
    assert redis.dead_letter_calls[0]["failure_code"] == "InvalidSummaryResultError"
    assert redis.dead_letter_calls[0]["attempt_count"] == 3


def test_perform_summary_dead_letters_snapshot_when_profile_agent_discards(
    monkeypatch: Any,
) -> None:
    module = _load_summary_worker_module(monkeypatch)
    monkeypatch.setattr(
        module,
        "get_config",
        lambda: KomariMemoryConfigSchema(
            summary_max_buffer_size=100,
            profile_trait_limit=20,
        ),
    )
    summary_calls = 0
    profile_calls = 0

    async def _fake_sleep(_delay: float) -> None:
        return None

    retry_module = sys.modules["komari_bot.plugins.komari_memory.core.retry"]
    monkeypatch.setattr(retry_module.asyncio, "sleep", _fake_sleep)

    async def _fake_summarize_conversation(
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        nonlocal summary_calls
        del args, kwargs
        summary_calls += 1
        return {"memories": [{"content": "有效摘要", "importance": 3}]}

    async def _fake_run_profile_agent(**kwargs: Any) -> SimpleNamespace:
        nonlocal profile_calls
        del kwargs
        profile_calls += 1
        return SimpleNamespace(
            committed_count=0,
            staged_count=1,
            summary="未调用提交工具",
            status="discarded",
            changed_user_ids=set(),
        )

    monkeypatch.setattr(module, "summarize_conversation", _fake_summarize_conversation)
    monkeypatch.setattr(module, "run_profile_agent", _fake_run_profile_agent)

    redis = _FakeRedis([_make_message()])
    memory = _FakeMemory()

    with pytest.raises(module.IncompleteProfileAgentError, match="discarded"):
        asyncio.run(module.perform_summary("114514", redis, memory))

    assert summary_calls == 1
    assert profile_calls == 3
    assert memory.store_conversation_calls == []
    assert redis.delete_processing_calls == []
    assert redis.update_last_summary_calls == []
    assert redis.restore_processing_calls == []
    assert redis.dead_letter_calls[0]["failure_code"] == "IncompleteProfileAgentError"
    assert redis.dead_letter_calls[0]["attempt_count"] == 3


def test_perform_summary_restores_snapshot_when_cancelled(monkeypatch: Any) -> None:
    module = _load_summary_worker_module(monkeypatch)
    monkeypatch.setattr(
        module,
        "get_config",
        lambda: KomariMemoryConfigSchema(
            summary_max_buffer_size=100,
            profile_trait_limit=20,
        ),
    )
    summary_started = asyncio.Event()
    never_finished = asyncio.Event()

    async def _fake_summarize_conversation(
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del args, kwargs
        summary_started.set()
        await never_finished.wait()
        raise AssertionError("取消后不应继续生成总结")

    monkeypatch.setattr(module, "summarize_conversation", _fake_summarize_conversation)
    redis = _FakeRedis([_make_message()])
    memory = _FakeMemory()

    async def _run() -> None:
        task = asyncio.create_task(module.perform_summary("114514", redis, memory))
        await summary_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())

    assert redis.dead_letter_calls == []
    assert len(redis.restore_processing_calls) == 1
    assert redis.restore_processing_calls[0]["group_id"] == "114514"
    assert redis.delete_processing_calls == []
    assert redis.update_last_summary_calls == []


def test_perform_summary_does_not_mutate_snapshot_after_lease_loss(
    monkeypatch: Any,
) -> None:
    module = _load_summary_worker_module(monkeypatch)
    monkeypatch.setattr(
        module,
        "get_config",
        lambda: KomariMemoryConfigSchema(
            summary_max_buffer_size=100,
            profile_trait_limit=20,
        ),
    )

    async def _fake_summarize_conversation(
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del args, kwargs
        return {"memories": [{"content": "有效摘要", "importance": 3}]}

    async def _fake_run_profile_agent(**kwargs: Any) -> SimpleNamespace:
        del kwargs
        return _profile_agent_result()

    class _LeaseLostRedis(_FakeRedis):
        async def renew_processing_conversation_lease(
            self,
            group_id: str,
            processing_key: str,
            owner_token: str,
        ) -> bool:
            del group_id, processing_key, owner_token
            self.current_owner = None
            return False

    monkeypatch.setattr(module, "summarize_conversation", _fake_summarize_conversation)
    monkeypatch.setattr(module, "run_profile_agent", _fake_run_profile_agent)
    redis = _LeaseLostRedis([_make_message()])
    memory = _FakeMemory()

    with pytest.raises(module.ConversationLeaseLostError):
        asyncio.run(module.perform_summary("114514", redis, memory))

    assert redis.dead_letter_calls == []
    assert redis.restore_processing_calls == []
    assert redis.delete_processing_calls == []
    assert redis.update_last_summary_calls == []


def test_perform_summary_refreshes_binding_before_summary_and_profile_agent(
    monkeypatch: Any,
) -> None:
    module = _load_summary_worker_module(monkeypatch)
    monkeypatch.setattr(
        module,
        "get_config",
        lambda: KomariMemoryConfigSchema(summary_max_buffer_size=100, profile_trait_limit=20),
    )

    class _FakeBinding:
        def __init__(self) -> None:
            self.refresh_calls = 0

        async def refresh_if_file_updated(self) -> bool:
            self.refresh_calls += 1
            return True

        def get_character_name(self, user_id: str, fallback_nickname: str = "") -> str:
            if user_id == "10001":
                return "绑定新名"
            return fallback_nickname or user_id

    fake_binding = _FakeBinding()
    monkeypatch.setattr(module, "character_binding", fake_binding)
    observed: dict[str, Any] = {}

    async def _fake_summarize_conversation(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args
        observed["summary_refresh_calls"] = fake_binding.refresh_calls
        observed["summary_display_name_map"] = kwargs["display_name_map"]
        return {"memories": [{"content": "绑定刷新后生成总结。", "importance": 3}]}

    async def _fake_run_profile_agent(**kwargs: Any) -> SimpleNamespace:
        observed["profile_conversation_text"] = kwargs["conversation_text"]
        observed["profile_display_name_map"] = kwargs["display_name_map"]
        return _profile_agent_result(changed_user_ids={"10001"})

    monkeypatch.setattr(module, "summarize_conversation", _fake_summarize_conversation)
    monkeypatch.setattr(module, "run_profile_agent", _fake_run_profile_agent)

    redis = _FakeRedis([_make_message(user_id="10001", user_nickname="旧昵称")])
    memory = _FakeMemory(
        profiles={
            ("114514", "10001"): {
                "version": 1,
                "user_id": "10001",
                "display_name": "旧画像名",
                "traits": {},
            }
        }
    )

    asyncio.run(module.perform_summary("114514", redis, memory))

    assert observed["summary_refresh_calls"] == 1
    assert observed["summary_display_name_map"] == {"10001": "旧昵称"}
    assert observed["profile_display_name_map"] == {"10001": "旧昵称"}
    assert "[user_id:10001] 旧昵称: 今天一起吃拉面吧" in observed[
        "profile_conversation_text"
    ]
    assert memory.store_conversation_calls[0]["summary"] == "绑定刷新后生成总结。"


def test_perform_summary_does_not_write_interaction_history(monkeypatch: Any) -> None:
    module = _load_summary_worker_module(monkeypatch)
    monkeypatch.setattr(
        module,
        "get_config",
        lambda: KomariMemoryConfigSchema(summary_max_buffer_size=100, profile_trait_limit=20),
    )

    async def _fake_summarize_conversation(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {"memories": [{"content": "只写入对话记忆，不处理互动历史。", "importance": 4}]}

    async def _fake_run_profile_agent(**kwargs: Any) -> SimpleNamespace:
        del kwargs
        return _profile_agent_result()

    monkeypatch.setattr(module, "summarize_conversation", _fake_summarize_conversation)
    monkeypatch.setattr(module, "run_profile_agent", _fake_run_profile_agent)

    redis = _FakeRedis([_make_message()])
    memory = _FakeMemory(
        interactions={
            ("114514", "10001"): {
                "version": 1,
                "user_id": "10001",
                "display_name": "阿明",
                "records": [{"event": "旧事件", "result": "旧反应", "emotion": "旧情绪"}],
            }
        }
    )

    asyncio.run(module.perform_summary("114514", redis, memory))

    assert len(memory.store_conversation_calls) == 1
    assert memory.upsert_interaction_history_calls == []


def test_perform_summary_processes_every_chunk_before_ack(monkeypatch: Any) -> None:
    module = _load_summary_worker_module(monkeypatch)
    monkeypatch.setattr(
        module,
        "get_config",
        lambda: KomariMemoryConfigSchema(
            summary_max_buffer_size=100,
            profile_trait_limit=20,
        ),
    )
    production_chunker = module.chunk_messages_for_memory_processing

    def _small_chunker(*args: Any, **kwargs: Any) -> Any:
        return production_chunker(
            *args,
            **kwargs,
            max_utf8_bytes=460,
            max_estimated_tokens=154,
        )

    monkeypatch.setattr(module, "chunk_messages_for_memory_processing", _small_chunker)
    messages = [
        _make_message(
            user_id=f"1000{index}",
            user_nickname=f"用户{index}",
            content=(
                f"第{index}条-" + "内容" * 55 + ("尾部分块金丝雀" if index == 5 else "")
            ),
        )
        for index in range(6)
    ]
    summary_inputs: list[str] = []
    profile_inputs: list[str] = []

    async def _fake_summarize_conversation(
        chunk_messages: list[MessageSchema],
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del args, kwargs
        summary_inputs.append("\n".join(message.content for message in chunk_messages))
        return {
            "memories": [
                {"content": f"第{len(summary_inputs)}块的有效摘要", "importance": 3}
            ]
        }

    async def _fake_run_profile_agent(**kwargs: Any) -> SimpleNamespace:
        profile_inputs.append(kwargs["conversation_text"])
        return _profile_agent_result()

    monkeypatch.setattr(module, "summarize_conversation", _fake_summarize_conversation)
    monkeypatch.setattr(module, "run_profile_agent", _fake_run_profile_agent)
    redis = _FakeRedis(messages)
    memory = _FakeMemory()

    asyncio.run(module.perform_summary("114514", redis, memory))

    assert len(summary_inputs) > 1
    assert len(profile_inputs) == len(summary_inputs)
    assert "尾部分块金丝雀" in summary_inputs[-1]
    assert "尾部分块金丝雀" in profile_inputs[-1]
    assert len(memory.store_conversation_calls) == len(summary_inputs)
    assert len(redis.delete_processing_calls) == 1


def test_processing_chunk_ledger_resumes_after_interruption(monkeypatch: Any) -> None:
    module = _load_summary_worker_module(monkeypatch)
    monkeypatch.setattr(
        module,
        "get_config",
        lambda: KomariMemoryConfigSchema(
            summary_max_buffer_size=100,
            profile_trait_limit=20,
        ),
    )
    production_chunker = module.chunk_messages_for_memory_processing

    def _small_chunker(*args: Any, **kwargs: Any) -> Any:
        return production_chunker(
            *args,
            **kwargs,
            max_utf8_bytes=520,
            max_estimated_tokens=174,
        )

    monkeypatch.setattr(module, "chunk_messages_for_memory_processing", _small_chunker)
    messages = [
        _make_message(user_id="10001", content="第一块" + "甲" * 90),
        _make_message(user_id="10002", content="第二块" + "乙" * 90),
    ]
    summary_calls: list[str] = []
    profile_calls: list[str] = []
    second_profile_failed = False

    async def _fake_summarize_conversation(
        chunk_messages: list[MessageSchema],
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del args, kwargs
        marker = chunk_messages[0].content[:3]
        summary_calls.append(marker)
        return {"memories": [{"content": f"{marker}的有效摘要", "importance": 3}]}

    async def _fake_run_profile_agent(**kwargs: Any) -> SimpleNamespace:
        nonlocal second_profile_failed
        marker = str(kwargs["conversation_text"])
        profile_calls.append(marker)
        if "第二块" in marker and not second_profile_failed:
            second_profile_failed = True
            raise RuntimeError("模拟第二块处理时进程中断")
        return _profile_agent_result()

    monkeypatch.setattr(module, "summarize_conversation", _fake_summarize_conversation)
    monkeypatch.setattr(module, "run_profile_agent", _fake_run_profile_agent)
    redis = _FakeRedis(messages)
    redis.current_owner = "owner-1"
    memory = _FakeMemory()
    run_once = module._perform_summary_from_processing.__wrapped__

    with pytest.raises(RuntimeError, match="进程中断"):
        asyncio.run(
            run_once(
                "114514",
                redis,
                memory,
                "processing-key",
                "owner-1",
            )
        )
    asyncio.run(
        run_once(
            "114514",
            redis,
            memory,
            "processing-key",
            "owner-1",
        )
    )

    assert summary_calls.count("第一块") == 1
    assert summary_calls.count("第二块") == 1
    assert sum("第一块" in call for call in profile_calls) == 1
    assert sum("第二块" in call for call in profile_calls) == 2
    assert len(memory.store_conversation_calls) == 2
