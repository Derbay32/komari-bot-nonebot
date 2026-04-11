"""KomariMemory 总结 worker 画像压缩测试。"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import nonebot.plugin

from komari_bot.plugins.komari_memory.config_schema import KomariMemoryConfigSchema
from komari_bot.plugins.komari_memory.services.profile_compaction import (
    count_profile_traits,
)
from komari_bot.plugins.komari_memory.services.redis_manager import MessageSchema

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = (
    PROJECT_ROOT / "komari_bot/plugins/komari_memory/handlers/summary_worker.py"
)
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
                get_character_name=lambda user_id, fallback_nickname="": (
                    fallback_nickname or user_id
                )
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
    original_handlers_package = sys.modules.get(
        "komari_bot.plugins.komari_memory.handlers"
    )
    original_module = sys.modules.get(
        "komari_bot.plugins.komari_memory.handlers.summary_worker"
    )

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
            sys.modules["komari_bot.plugins.komari_memory.handlers.summary_worker"] = (
                original_module
            )
        else:
            sys.modules.pop(
                "komari_bot.plugins.komari_memory.handlers.summary_worker",
                None,
            )

        if original_handlers_package is not None:
            sys.modules["komari_bot.plugins.komari_memory.handlers"] = (
                original_handlers_package
            )
        else:
            sys.modules.pop("komari_bot.plugins.komari_memory.handlers", None)

        if original_memory_package is not None:
            sys.modules["komari_bot.plugins.komari_memory"] = original_memory_package
        else:
            sys.modules.pop("komari_bot.plugins.komari_memory", None)


class _FakeRedis:
    def __init__(self, messages: list[MessageSchema]) -> None:
        self._messages = messages
        self.reset_message_count_calls: list[str] = []
        self.reset_tokens_calls: list[str] = []
        self.delete_buffer_calls: list[str] = []
        self.update_last_summary_calls: list[str] = []

    async def get_buffer(self, group_id: str, limit: int = 100) -> list[MessageSchema]:
        del group_id, limit
        return list(self._messages)

    async def reset_message_count(self, group_id: str) -> None:
        self.reset_message_count_calls.append(group_id)

    async def reset_tokens(self, group_id: str) -> None:
        self.reset_tokens_calls.append(group_id)

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
        return 99

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
) -> MessageSchema:
    return MessageSchema(
        user_id=user_id,
        user_nickname=user_nickname,
        group_id=group_id,
        content=content,
        timestamp=1.0,
        message_id=f"msg-{user_id}",
        is_bot=False,
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


def test_enforce_profile_trait_limit_falls_back_to_base_profile(
    monkeypatch: Any,
) -> None:
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


def test_perform_summary_appends_interaction_records_and_preserves_metadata(
    monkeypatch: Any,
) -> None:
    module = _load_summary_worker_module(monkeypatch)
    monkeypatch.setattr(
        module,
        "get_config",
        lambda: KomariMemoryConfigSchema(summary_max_messages=50, profile_trait_limit=20),
    )

    async def _fake_summarize_conversation(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {
            "summary": "新的群聊总结",
            "user_profile_operations": [],
            "user_interaction_operations": [
                {
                    "user_id": "10001",
                    "display_name": "阿明",
                    "operations": [
                        {"op": "replace", "field": "summary", "value": "新的整体评价"},
                        {
                            "op": "add",
                            "field": "record",
                            "value": {
                                "event": "新事件1",
                                "result": "新反应1",
                                "emotion": "新情绪1",
                            },
                        },
                        {
                            "op": "add",
                            "field": "record",
                            "value": {
                                "event": "新事件2",
                                "result": "新反应2",
                                "emotion": "新情绪2",
                            },
                        },
                        {
                            "op": "add",
                            "field": "record",
                            "value": {
                                "event": "新事件3",
                                "result": "新反应3",
                                "emotion": "新情绪3",
                            },
                        },
                        {
                            "op": "add",
                            "field": "record",
                            "value": {
                                "event": "新事件4",
                                "result": "新反应4",
                                "emotion": "新情绪4",
                            },
                        },
                    ],
                }
            ],
            "importance": 4,
        }

    monkeypatch.setattr(module, "summarize_conversation", _fake_summarize_conversation)

    redis = _FakeRedis([_make_message()])
    memory = _FakeMemory(
        interactions={
            (
                "114514",
                "10001",
            ): {
                "version": 1,
                "user_id": "10001",
                "display_name": "阿明",
                "file_type": "旧文件类型",
                "description": "旧描述",
                "records": [
                    {"event": "旧事件1", "result": "旧反应1", "emotion": "旧情绪1"},
                    {"event": "旧事件2", "result": "旧反应2", "emotion": "旧情绪2"},
                    {"event": "旧事件3", "result": "旧反应3", "emotion": "旧情绪3"},
                    {"event": "旧事件4", "result": "旧反应4", "emotion": "旧情绪4"},
                ],
                "summary": "旧评价",
            }
        }
    )

    asyncio.run(module.perform_summary("114514", redis, memory))

    interaction = memory.upsert_interaction_history_calls[0]["interaction"]
    assert interaction["file_type"] == "旧文件类型"
    assert interaction["description"] == "旧描述"
    assert interaction["summary"] == "新的整体评价"
    assert interaction["records"] == [
        {"event": "旧事件3", "result": "旧反应3", "emotion": "旧情绪3"},
        {"event": "旧事件4", "result": "旧反应4", "emotion": "旧情绪4"},
        {"event": "新事件1", "result": "新反应1", "emotion": "新情绪1"},
        {"event": "新事件2", "result": "新反应2", "emotion": "新情绪2"},
        {"event": "新事件3", "result": "新反应3", "emotion": "新情绪3"},
        {"event": "新事件4", "result": "新反应4", "emotion": "新情绪4"},
    ]


def test_perform_summary_keeps_existing_interaction_when_model_returns_no_update(
    monkeypatch: Any,
) -> None:
    module = _load_summary_worker_module(monkeypatch)
    monkeypatch.setattr(
        module,
        "get_config",
        lambda: KomariMemoryConfigSchema(summary_max_messages=50, profile_trait_limit=20),
    )

    async def _fake_summarize_conversation(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {
            "summary": "新的群聊总结",
            "user_profile_operations": [],
            "user_interaction_operations": [],
            "importance": 4,
        }

    monkeypatch.setattr(module, "summarize_conversation", _fake_summarize_conversation)

    existing_interaction = {
        "version": 1,
        "user_id": "10001",
        "display_name": "阿明",
        "file_type": "旧文件类型",
        "description": "旧描述",
        "records": [{"event": "旧事件", "result": "旧反应", "emotion": "旧情绪"}],
        "summary": "旧评价",
    }
    redis = _FakeRedis([_make_message()])
    memory = _FakeMemory(interactions={("114514", "10001"): existing_interaction})

    asyncio.run(module.perform_summary("114514", redis, memory))

    interaction = memory.upsert_interaction_history_calls[0]["interaction"]
    assert interaction["file_type"] == "旧文件类型"
    assert interaction["description"] == "旧描述"
    assert interaction["summary"] == "旧评价"
    assert interaction["records"] == existing_interaction["records"]


def test_perform_summary_remaps_hallucinated_uid_by_display_name_and_drops_unknown_uid(
    monkeypatch: Any,
) -> None:
    module = _load_summary_worker_module(monkeypatch)
    monkeypatch.setattr(
        module,
        "get_config",
        lambda: KomariMemoryConfigSchema(summary_max_messages=50, profile_trait_limit=20),
    )

    async def _fake_summarize_conversation(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {
            "summary": "新的群聊总结",
            "user_profile_operations": [
                {
                    "user_id": "99999",
                    "display_name": "阿明",
                    "operations": [
                        {
                            "op": "replace",
                            "field": "trait",
                            "key": "喜欢的食物",
                            "value": "布丁",
                            "category": "preference",
                            "importance": 4,
                        }
                    ],
                },
                {
                    "user_id": "88888",
                    "display_name": "路人甲",
                    "operations": [
                        {
                            "op": "replace",
                            "field": "trait",
                            "key": "无效画像",
                            "value": "不该入库",
                            "category": "general",
                            "importance": 1,
                        }
                    ],
                },
            ],
            "user_interaction_operations": [
                {
                    "user_id": "99999",
                    "display_name": "阿明",
                    "operations": [
                        {
                            "op": "add",
                            "field": "record",
                            "value": {
                                "event": "递上布丁",
                                "result": "立刻靠近",
                                "emotion": "开心",
                            },
                        },
                        {
                            "op": "replace",
                            "field": "summary",
                            "value": "会被布丁钓走",
                        },
                    ],
                },
                {
                    "user_id": "77777",
                    "display_name": "路人乙",
                    "operations": [
                        {
                            "op": "add",
                            "field": "record",
                            "value": {
                                "event": "无效事件",
                                "result": "无效反应",
                                "emotion": "无效情绪",
                            },
                        }
                    ],
                },
            ],
            "importance": 4,
        }

    monkeypatch.setattr(module, "summarize_conversation", _fake_summarize_conversation)

    redis = _FakeRedis([_make_message(user_id="10001", user_nickname="阿明")])
    memory = _FakeMemory()

    asyncio.run(module.perform_summary("114514", redis, memory))

    assert [call["user_id"] for call in memory.upsert_user_profile_calls] == ["10001"]
    assert [call["user_id"] for call in memory.upsert_interaction_history_calls] == [
        "10001"
    ]
    profile = memory.upsert_user_profile_calls[0]["profile"]
    assert profile["traits"]["喜欢的食物"]["value"] == "布丁"
    interaction = memory.upsert_interaction_history_calls[0]["interaction"]
    assert interaction["summary"] == "会被布丁钓走"
    assert interaction["records"] == [
        {"event": "递上布丁", "result": "立刻靠近", "emotion": "开心"}
    ]


def test_apply_profile_operations_supports_add_replace_delete(monkeypatch: Any) -> None:
    module = _load_summary_worker_module(monkeypatch)

    result = module._apply_profile_operations(
        {
            "version": 1,
            "user_id": "10001",
            "display_name": "旧名字",
            "traits": {
                "旧特征": {
                    "value": "会过时",
                    "category": "general",
                    "importance": 2,
                    "updated_at": "2026-04-01T00:00:00+08:00",
                },
                "保留特征": {
                    "value": "旧值",
                    "category": "general",
                    "importance": 3,
                    "updated_at": "2026-04-01T00:00:00+08:00",
                },
            },
        },
        user_id="10001",
        display_name="阿明",
        operations=[
            {"op": "add", "field": "trait", "key": "新增特征", "value": "新值"},
            {
                "op": "replace",
                "field": "trait",
                "key": "保留特征",
                "value": "改成新值",
                "category": "fact",
                "importance": 5,
            },
            {"op": "delete", "field": "trait", "key": "旧特征"},
            {"op": "replace", "field": "display_name", "value": "新名字"},
        ],
    )

    assert result["display_name"] == "新名字"
    assert "旧特征" not in result["traits"]
    assert result["traits"]["新增特征"]["value"] == "新值"
    assert result["traits"]["保留特征"]["value"] == "改成新值"
    assert result["traits"]["保留特征"]["category"] == "fact"
