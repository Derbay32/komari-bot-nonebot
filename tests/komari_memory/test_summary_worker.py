"""KomariMemory 总结 worker 编排测试。"""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nonebot.plugin

from komari_bot.plugins.komari_memory.config_schema import KomariMemoryConfigSchema
from komari_bot.plugins.komari_memory.services.profile_compaction import (
    count_profile_traits,
)
from komari_bot.plugins.komari_memory.services.redis_manager import MessageSchema

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = PROJECT_ROOT / "komari_bot/plugins/komari_memory/handlers/summary_worker.py"
PACKAGE_ROOT = PROJECT_ROOT / "komari_bot/plugins/komari_memory"


def _make_profile(trait_count: int) -> dict[str, Any]:
    return {
        "version": 1,
        "user_id": "10001",
        "display_name": "阿明",
        "traits": {
            f"特征{i:02d}": {
                "value": f"长期描述{i}",
                "category": "general",
                "importance": 4,
                "updated_at": f"2026-03-21T00:00:{i % 60:02d}+08:00",
            }
            for i in range(trait_count)
        },
    }


def _load_summary_worker_module(monkeypatch: Any) -> Any:
    def _fake_require(name: str) -> object:
        if name == "character_binding":
            return types.SimpleNamespace(
                get_character_name=lambda user_id, fallback_nickname="": fallback_nickname
                or user_id,
                refresh_if_file_updated=lambda: False,
            )
        if name == "llm_provider":
            return types.SimpleNamespace(generate_text=lambda **_kwargs: None)
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
        self.delete_buffer_calls: list[str] = []
        self.update_last_summary_calls: list[str] = []

    async def get_buffer(self, group_id: str, limit: int = 100) -> list[MessageSchema]:
        del group_id, limit
        return list(self._messages)

    async def delete_buffer(self, group_id: str) -> None:
        self.delete_buffer_calls.append(group_id)

    async def update_last_summary(self, group_id: str) -> None:
        self.update_last_summary_calls.append(group_id)


class _FakeMemory:
    def __init__(
        self,
        *,
        profiles: dict[tuple[str, str], dict[str, Any]] | None = None,
        interactions: dict[tuple[str, str], dict[str, Any]] | None = None,
    ) -> None:
        self._profiles = profiles or {}
        self._interactions = interactions or {}
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
    ) -> int:
        self.store_conversation_calls.append(
            {
                "group_id": group_id,
                "summary": summary,
                "participants": participants,
                "importance_initial": importance_initial,
            }
        )
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
) -> MessageSchema:
    return MessageSchema(
        user_id=user_id,
        user_nickname=user_nickname,
        group_id=group_id,
        content=content,
        timestamp=1.0,
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


def test_enforce_profile_trait_limit_uses_compacted_profile(monkeypatch: Any) -> None:
    module = _load_summary_worker_module(monkeypatch)

    async def _fake_compact_profile_with_llm(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        return _make_profile(20)

    monkeypatch.setattr(module, "compact_profile_with_llm", _fake_compact_profile_with_llm)

    result = asyncio.run(
        module._enforce_profile_trait_limit(
            group_id="114514",
            user_id="10001",
            base_profile=_make_profile(6),
            merged_profile=_make_profile(26),
            config=KomariMemoryConfigSchema(profile_trait_limit=20),
        )
    )

    assert count_profile_traits(result) == 20


def test_enforce_profile_trait_limit_falls_back_to_base_profile(monkeypatch: Any) -> None:
    module = _load_summary_worker_module(monkeypatch)

    async def _boom(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        raise RuntimeError("boom")

    base_profile = _make_profile(4)
    monkeypatch.setattr(module, "compact_profile_with_llm", _boom)

    result = asyncio.run(
        module._enforce_profile_trait_limit(
            group_id="114514",
            user_id="10001",
            base_profile=base_profile,
            merged_profile=_make_profile(25),
            config=KomariMemoryConfigSchema(profile_trait_limit=20),
        )
    )

    assert result == base_profile


def test_perform_summary_stores_multiple_memories_before_profile_agent(monkeypatch: Any) -> None:
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
    assert memory.upsert_interaction_history_calls == []
    assert redis.delete_buffer_calls == ["114514"]
    assert redis.update_last_summary_calls == ["114514"]


def test_perform_summary_continues_profile_agent_when_summary_fails(monkeypatch: Any) -> None:
    module = _load_summary_worker_module(monkeypatch)
    monkeypatch.setattr(
        module,
        "get_config",
        lambda: KomariMemoryConfigSchema(summary_max_buffer_size=100, profile_trait_limit=20),
    )
    events: list[str] = []

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

    asyncio.run(module.perform_summary("114514", redis, memory))

    assert events == ["summary", "profile_agent"]
    assert memory.store_conversation_calls == []
    assert redis.delete_buffer_calls == ["114514"]


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
