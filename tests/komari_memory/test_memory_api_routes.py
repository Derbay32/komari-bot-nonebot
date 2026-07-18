"""Komari Memory API 路由测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI

from komari_bot.plugins.komari_memory.api import API_PREFIX, register_memory_api
from komari_bot.plugins.komari_memory.services.conversation_processing import (
    ConversationDeadLetter,
)

if TYPE_CHECKING:
    from nonebug import App


def _with_query(path: str, **params: object) -> str:
    query = "&".join(
        f"{key}={value}" for key, value in params.items() if value is not None
    )
    return f"{path}?{query}" if query else path


def _conversation_entry(
    *,
    conversation_id: int = 1,
    summary: str = "一起聊了布丁",
) -> dict[str, object]:
    timestamp = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)
    return {
        "id": conversation_id,
        "group_id": "g1",
        "summary": summary,
        "participants": ["u1", "u2"],
        "start_time": timestamp,
        "end_time": timestamp,
        "importance_initial": 4,
        "importance_current": 4,
        "last_accessed": timestamp,
        "created_at": timestamp,
    }


def _entity_entry(*, key: str, user_id: str = "u1") -> dict[str, object]:
    timestamp = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)
    return {
        "user_id": user_id,
        "group_id": "g1",
        "key": key,
        "category": "profile_json" if key == "user_profile" else "interaction_history",
        "importance": 4 if key == "user_profile" else 5,
        "access_count": 2,
        "last_accessed": timestamp,
        "value": {
            "user_id": user_id,
            "display_name": "阿明",
            "summary": "最近常聊天" if key == "interaction_history" else "",
            "traits": {"喜欢的食物": {"value": "布丁"}}
            if key == "user_profile"
            else {},
            "records": [],
        },
    }


def _interaction_event_entry(*, event_id: int = 1) -> dict[str, object]:
    timestamp = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)
    return {
        "id": event_id,
        "user_id": "u1",
        "display_name": "阿明",
        "event_summary": "阿明经常和小鞠聊游戏并喜欢轻松吐槽。",
        "source_message_count": 20,
        "first_seen_at": timestamp,
        "last_seen_at": timestamp,
        "importance": 4,
        "importance_initial": 4,
        "importance_current": 4,
        "last_accessed": timestamp,
        "is_fuzzy": False,
        "created_at": timestamp,
    }


def _interaction_event_entity(*, event_id: int = 1) -> dict[str, object]:
    event = _interaction_event_entry(event_id=event_id)
    return {
        "user_id": event["user_id"],
        "group_id": "",
        "key": f"interaction_event:{event_id}",
        "category": "interaction_event",
        "importance": event["importance"],
        "access_count": 0,
        "last_accessed": event["last_accessed"],
        "value": event,
    }


class _FakeMemoryService:
    def __init__(self) -> None:
        self.conversations = {
            1: _conversation_entry(),
            2: _conversation_entry(conversation_id=2, summary="一起聊了游戏"),
        }
        self.user_profiles = {("g1", "u1"): _entity_entry(key="user_profile")}
        self.interaction_histories = {
            ("g1", "u1"): _entity_entry(key="interaction_history")
        }
        self.interaction_events = {1: _interaction_event_entry()}
        self.list_conversation_calls: list[dict[str, object]] = []
        self.update_conversation_calls: list[tuple[int, dict[str, object]]] = []
        self.list_profile_calls: list[dict[str, object]] = []
        self.list_history_calls: list[dict[str, object]] = []

    async def list_conversations(
        self, **kwargs: object
    ) -> tuple[list[dict[str, object]], int]:
        self.list_conversation_calls.append(dict(kwargs))
        return [self.conversations[1]], len(self.conversations)

    async def get_conversation_entry(
        self, conversation_id: int
    ) -> dict[str, object] | None:
        return self.conversations.get(conversation_id)

    async def create_conversation_entry(self, **kwargs: object) -> dict[str, object]:
        created = _conversation_entry(conversation_id=3, summary=str(kwargs["summary"]))
        self.conversations[3] = created
        return created

    async def update_conversation_entry(
        self,
        conversation_id: int,
        **kwargs: object,
    ) -> dict[str, object] | None:
        self.update_conversation_calls.append((conversation_id, dict(kwargs)))
        current = self.conversations.get(conversation_id)
        if current is None:
            return None
        updated = dict(current)
        updated.update(
            {key: value for key, value in kwargs.items() if value is not None}
        )
        self.conversations[conversation_id] = updated
        return updated

    async def delete_conversation_entry(self, conversation_id: int) -> bool:
        return self.conversations.pop(conversation_id, None) is not None

    async def list_user_profile_rows(
        self, **kwargs: object
    ) -> tuple[list[dict[str, object]], int]:
        self.list_profile_calls.append(dict(kwargs))
        return [self.user_profiles[("g1", "u1")]], len(self.user_profiles)

    async def list_interaction_history_rows(
        self,
        **kwargs: object,
    ) -> tuple[list[dict[str, object]], int]:
        self.list_history_calls.append(dict(kwargs))
        if kwargs.get("group_id") is None:
            return [_interaction_event_entity()], len(self.interaction_events)
        return [self.interaction_histories[("g1", "u1")]], len(self.interaction_histories)

    async def get_user_profile_row(
        self,
        *,
        user_id: str,
        group_id: str,
    ) -> dict[str, object] | None:
        return self.user_profiles.get((group_id, user_id))

    async def get_interaction_history_row(
        self,
        *,
        user_id: str,
        group_id: str,
    ) -> dict[str, object] | None:
        return self.interaction_histories.get((group_id, user_id))

    async def upsert_user_profile_row(
        self,
        *,
        user_id: str,
        group_id: str,
        profile: dict[str, object],
        importance: int = 4,
    ) -> dict[str, object]:
        entry = {
            **_entity_entry(key="user_profile", user_id=user_id),
            "group_id": group_id,
            "importance": importance,
            "value": dict(profile),
        }
        self.user_profiles[(group_id, user_id)] = entry
        return entry

    async def upsert_interaction_history_row(
        self,
        *,
        user_id: str,
        group_id: str,
        interaction: dict[str, object],
        importance: int = 5,
    ) -> dict[str, object]:
        entry = {
            **_entity_entry(key="interaction_history", user_id=user_id),
            "group_id": group_id,
            "importance": importance,
            "value": dict(interaction),
        }
        self.interaction_histories[(group_id, user_id)] = entry
        return entry

    async def delete_user_profile(self, *, user_id: str, group_id: str) -> bool:
        return self.user_profiles.pop((group_id, user_id), None) is not None

    async def delete_interaction_history(self, *, user_id: str, group_id: str) -> bool:
        return self.interaction_histories.pop((group_id, user_id), None) is not None

    async def get_interaction_event_entry(
        self,
        event_id: int,
    ) -> dict[str, object] | None:
        return self.interaction_events.get(event_id)

    async def update_interaction_event_entry(
        self,
        event_id: int,
        **kwargs: object,
    ) -> dict[str, object] | None:
        event = self.interaction_events.get(event_id)
        if event is None:
            return None
        updated = dict(event)
        updated.update({key: value for key, value in kwargs.items() if value is not None})
        self.interaction_events[event_id] = updated
        return updated

    async def delete_interaction_event_entry(self, event_id: int) -> bool:
        return self.interaction_events.pop(event_id, None) is not None


class _FakeDeadLetterManager:
    def __init__(self) -> None:
        self.items = [
            ConversationDeadLetter(
                group_id="g1",
                snapshot_id="snapshot-1",
                failure_code="RuntimeError",
                attempt_count=3,
                failed_at_ms=1_000,
                message_count=2,
                chunk_state_count=4,
            )
        ]
        self.list_limits: list[int] = []
        self.requeue_calls: list[tuple[str, str]] = []

    async def list_conversation_dead_letters(
        self,
        *,
        limit: int = 100,
    ) -> list[ConversationDeadLetter]:
        self.list_limits.append(limit)
        return self.items[:limit]

    async def requeue_conversation_dead_letter(
        self,
        *,
        group_id: str,
        snapshot_id: str,
    ) -> int | None:
        self.requeue_calls.append((group_id, snapshot_id))
        for index, item in enumerate(self.items):
            if item.group_id == group_id and item.snapshot_id == snapshot_id:
                self.items.pop(index)
                return item.message_count
        return None


def _build_app(
    service: _FakeMemoryService | None,
    redis_manager: _FakeDeadLetterManager | None = None,
    *,
    api_token: Any = "secret-token-00000000",
) -> FastAPI:
    api_app = FastAPI()
    register_memory_api(
        api_app,
        api_token=api_token,
        allowed_origins=["https://ui.example.com"],
        service_getter=lambda: service,
        redis_getter=lambda: redis_manager,
    )
    return api_app


@pytest.mark.asyncio
async def test_memory_routes_require_token_and_handle_cors(app: App) -> None:
    async with app.test_server(asgi=cast("Any", _build_app(_FakeMemoryService()))) as ctx:
        client = ctx.get_client()
        unauthorized = await client.get(f"{API_PREFIX}/conversations")
        assert unauthorized.status_code == 401

        preflight = await client.options(
            f"{API_PREFIX}/conversations",
            headers={
                "Origin": "https://ui.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert preflight.status_code == 200
        assert (
            preflight.headers["access-control-allow-origin"] == "https://ui.example.com"
        )


@pytest.mark.asyncio
async def test_memory_routes_return_503_when_service_unavailable(app: App) -> None:
    async with app.test_server(asgi=cast("Any", _build_app(None))) as ctx:
        client = ctx.get_client()
        response = await client.get(
            f"{API_PREFIX}/conversations",
            headers={"Authorization": "Bearer secret-token-00000000"},
        )

    assert response.status_code == 503
    assert "服务未初始化" in response.json()["detail"]


@pytest.mark.asyncio
async def test_conversation_dead_letter_routes_query_and_requeue_without_body(
    app: App,
) -> None:
    redis_manager = _FakeDeadLetterManager()
    headers = {"Authorization": "Bearer secret-token-00000000"}

    async with app.test_server(
        asgi=cast("Any", _build_app(_FakeMemoryService(), redis_manager))
    ) as ctx:
        client = ctx.get_client()
        listed = await client.get(
            _with_query(f"{API_PREFIX}/conversation-dead-letters", limit=5),
            headers=headers,
        )
        requeued = await client.post(
            f"{API_PREFIX}/conversation-dead-letters/g1/snapshot-1/requeue",
            headers=headers,
        )
        missing = await client.post(
            f"{API_PREFIX}/conversation-dead-letters/g1/snapshot-1/requeue",
            headers=headers,
        )

    assert listed.status_code == 200
    assert redis_manager.list_limits == [5]
    assert listed.json() == {
        "items": [
            {
                "group_id": "g1",
                "snapshot_id": "snapshot-1",
                "failure_code": "RuntimeError",
                "attempt_count": 3,
                "failed_at_ms": 1_000,
                "message_count": 2,
                "chunk_state_count": 4,
            }
        ],
        "limit": 5,
    }
    assert requeued.status_code == 200
    assert requeued.json()["restored_message_count"] == 2
    assert redis_manager.requeue_calls == [
        ("g1", "snapshot-1"),
        ("g1", "snapshot-1"),
    ]
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_conversation_dead_letter_routes_require_redis_and_write_permission(
    app: App,
) -> None:
    credentials = [
        {
            "credential_id": "memory-reader",
            "token": "memory-reader-token-12345",
            "permissions": ["memory:read"],
        }
    ]
    headers = {"Authorization": "Bearer memory-reader-token-12345"}

    async with app.test_server(
        asgi=cast(
            "Any",
            _build_app(
                _FakeMemoryService(),
                None,
                api_token=credentials,
            ),
        )
    ) as ctx:
        client = ctx.get_client()
        unavailable = await client.get(
            f"{API_PREFIX}/conversation-dead-letters",
            headers=headers,
        )
        forbidden = await client.post(
            f"{API_PREFIX}/conversation-dead-letters/g1/snapshot-1/requeue",
            headers=headers,
        )

    assert unavailable.status_code == 503
    assert "dead-letter" in unavailable.json()["detail"]
    assert forbidden.status_code == 403


@pytest.mark.asyncio
async def test_conversation_routes_forward_filters_and_support_crud(app: App) -> None:
    service = _FakeMemoryService()
    headers = {"Authorization": "Bearer secret-token-00000000"}

    async with app.test_server(asgi=cast("Any", _build_app(service))) as ctx:
        client = ctx.get_client()
        listed = await client.get(
            _with_query(
                f"{API_PREFIX}/conversations",
                group_id="g1",
                participant="u1",
                q="布丁",
                limit=5,
                offset=2,
            ),
            headers=headers,
        )
        detail = await client.get(f"{API_PREFIX}/conversations/1", headers=headers)
        created = await client.post(
            f"{API_PREFIX}/conversations",
            json={
                "group_id": "g1",
                "summary": "  新记忆  ",
                "participants": ["u1", " u2 "],
                "importance_initial": 5,
            },
            headers=headers,
        )
        updated = await client.patch(
            f"{API_PREFIX}/conversations/1",
            json={"summary": "改过的记忆", "importance_current": 4},
            headers=headers,
        )
        missing_patch = await client.patch(
            f"{API_PREFIX}/conversations/999",
            json={"summary": "不存在"},
            headers=headers,
        )
        deleted = await client.delete(f"{API_PREFIX}/conversations/2", headers=headers)

    assert listed.status_code == 200
    assert listed.json()["total"] == 2
    assert service.list_conversation_calls == [
        {
            "limit": 5,
            "offset": 2,
            "group_id": "g1",
            "participant": "u1",
            "query": "布丁",
        }
    ]
    assert detail.status_code == 200
    assert detail.json()["id"] == 1
    assert created.status_code == 201
    assert created.json()["summary"] == "新记忆"
    assert updated.status_code == 200
    assert updated.json()["importance_current"] == 4
    assert service.update_conversation_calls[0][1]["summary"] == "改过的记忆"
    assert missing_patch.status_code == 404
    assert deleted.status_code == 204


@pytest.mark.asyncio
async def test_profile_routes_list_get_upsert_delete_and_validate(app: App) -> None:
    service = _FakeMemoryService()
    headers = {"Authorization": "Bearer secret-token-00000000"}

    async with app.test_server(asgi=cast("Any", _build_app(service))) as ctx:
        client = ctx.get_client()
        profiles = await client.get(
            _with_query(
                f"{API_PREFIX}/user-profiles",
                group_id="g1",
                user_id="u1",
                q="布丁",
                limit=3,
            ),
            headers=headers,
        )
        profile_detail = await client.get(
            f"{API_PREFIX}/user-profiles/g1/u1",
            headers=headers,
        )
        profile_put = await client.put(
            f"{API_PREFIX}/user-profiles/g1/u2",
            json={
                "user_id": "u2",
                "display_name": "小李",
                "traits": {" 爱好 ": {"value": "游戏"}},
            },
            headers=headers,
        )
        mismatch = await client.put(
            f"{API_PREFIX}/user-profiles/g1/u3",
            json={"user_id": "u4"},
            headers=headers,
        )
        bad_body = await client.put(
            f"{API_PREFIX}/user-profiles/g1/u3",
            json=["not-an-object"],
            headers=headers,
        )
        deleted_profile = await client.delete(
            f"{API_PREFIX}/user-profiles/g1/u1",
            headers=headers,
        )

    assert profiles.status_code == 200
    assert profiles.json()["total"] == 1
    assert service.list_profile_calls == [
        {
            "limit": 3,
            "offset": 0,
            "group_id": "g1",
            "user_id": "u1",
            "query": "布丁",
        }
    ]
    assert profile_detail.status_code == 200
    assert profile_put.status_code == 200
    assert profile_put.json()["value"]["display_name"] == "小李"
    assert profile_put.json()["value"]["traits"] == {"爱好": {"value": "游戏"}}
    assert mismatch.status_code == 422
    assert "user_id" in mismatch.json()["detail"]
    assert bad_body.status_code == 422
    assert deleted_profile.status_code == 204


@pytest.mark.asyncio
async def test_legacy_interaction_history_routes_return_migration_410(app: App) -> None:
    service = _FakeMemoryService()
    headers = {"Authorization": "Bearer secret-token-00000000"}

    async with app.test_server(asgi=cast("Any", _build_app(service))) as ctx:
        client = ctx.get_client()
        responses = [
            await client.get(f"{API_PREFIX}/interaction-histories", headers=headers),
            await client.get(
                f"{API_PREFIX}/interaction-histories/g1/u1",
                headers=headers,
            ),
            await client.put(
                f"{API_PREFIX}/interaction-histories/g1/u1",
                json={"user_id": "u1", "records": []},
                headers=headers,
            ),
            await client.delete(
                f"{API_PREFIX}/interaction-histories/g1/u1",
                headers=headers,
            ),
        ]

    assert {response.status_code for response in responses} == {410}
    for response in responses:
        detail = response.json()["detail"]
        assert detail["code"] == "interaction_histories_retired"
        assert detail["replacement"] == f"{API_PREFIX}/interactions"
    assert service.list_history_calls == []


@pytest.mark.asyncio
async def test_memory_api_rejects_oversized_or_deep_management_input(app: App) -> None:
    service = _FakeMemoryService()
    headers = {"Authorization": "Bearer secret-token-00000000"}
    nested: object = "leaf"
    for _ in range(6):
        nested = [nested]

    async with app.test_server(asgi=cast("Any", _build_app(service))) as ctx:
        client = ctx.get_client()
        oversized_query = await client.get(
            _with_query(f"{API_PREFIX}/conversations", q="q" * 513),
            headers=headers,
        )
        oversized_identifier = await client.get(
            _with_query(f"{API_PREFIX}/user-profiles", group_id="g" * 129),
            headers=headers,
        )
        too_many_participants = await client.post(
            f"{API_PREFIX}/conversations",
            json={
                "group_id": "g1",
                "summary": "摘要",
                "participants": [f"u{index}" for index in range(101)],
            },
            headers=headers,
        )
        too_many_traits = await client.put(
            f"{API_PREFIX}/user-profiles/g1/u1",
            json={
                "traits": {
                    f"trait-{index}": {"value": "x"} for index in range(101)
                }
            },
            headers=headers,
        )
        deep_profile = await client.put(
            f"{API_PREFIX}/user-profiles/g1/u1",
            json={"traits": {"deep": {"value": nested}}},
            headers=headers,
        )
        oversized_event_summary = await client.patch(
            f"{API_PREFIX}/interactions/1",
            json={"event_summary": "s" * 12_001},
            headers=headers,
        )

    assert oversized_query.status_code == 422
    assert "查询文本" in oversized_query.json()["detail"]
    assert oversized_identifier.status_code == 422
    assert "群组 ID" in oversized_identifier.json()["detail"]
    assert too_many_participants.status_code == 422
    assert "参与者数量超过上限" in str(too_many_participants.json())
    assert too_many_traits.status_code == 422
    assert "trait 数量超过上限" in str(too_many_traits.json())
    assert deep_profile.status_code == 422
    assert "嵌套深度超过上限" in str(deep_profile.json())
    assert oversized_event_summary.status_code == 422
    assert "对话摘要" not in str(oversized_event_summary.json())
    assert service.list_conversation_calls == []
    assert service.list_profile_calls == []


@pytest.mark.asyncio
async def test_interaction_event_routes_support_event_id_crud(app: App) -> None:
    service = _FakeMemoryService()
    headers = {"Authorization": "Bearer secret-token-00000000"}

    async with app.test_server(asgi=cast("Any", _build_app(service))) as ctx:
        client = ctx.get_client()
        listed = await client.get(
            _with_query(f"{API_PREFIX}/interactions", user_id="u1", q="游戏", limit=5),
            headers=headers,
        )
        detail = await client.get(f"{API_PREFIX}/interactions/1", headers=headers)
        updated = await client.patch(
            f"{API_PREFIX}/interactions/1",
            json={"event_summary": "阿明喜欢和小鞠聊游戏。", "importance_current": 5},
            headers=headers,
        )
        missing_update = await client.patch(
            f"{API_PREFIX}/interactions/999",
            json={"event_summary": "不存在"},
            headers=headers,
        )
        empty_patch = await client.patch(
            f"{API_PREFIX}/interactions/1",
            json={},
            headers=headers,
        )
        deleted = await client.delete(f"{API_PREFIX}/interactions/1", headers=headers)
        missing_delete = await client.delete(
            f"{API_PREFIX}/interactions/1",
            headers=headers,
        )

    assert listed.status_code == 200
    assert listed.json()["items"][0]["event_summary"].startswith("阿明经常")
    assert service.list_history_calls[-1] == {
        "limit": 5,
        "offset": 0,
        "user_id": "u1",
        "query": "游戏",
    }
    assert detail.status_code == 200
    assert detail.json()["id"] == 1
    assert updated.status_code == 200
    assert updated.json()["importance_current"] == 5
    assert updated.json()["event_summary"] == "阿明喜欢和小鞠聊游戏。"
    assert missing_update.status_code == 404
    assert empty_patch.status_code == 422
    assert deleted.status_code == 204
    assert missing_delete.status_code == 404


@pytest.mark.asyncio
async def test_entity_routes_return_404_for_missing_rows(app: App) -> None:
    headers = {"Authorization": "Bearer secret-token-00000000"}

    async with app.test_server(asgi=cast("Any", _build_app(_FakeMemoryService()))) as ctx:
        client = ctx.get_client()
        missing_profile = await client.get(
            f"{API_PREFIX}/user-profiles/g1/u9",
            headers=headers,
        )

    assert missing_profile.status_code == 404
