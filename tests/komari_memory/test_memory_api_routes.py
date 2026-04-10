"""Komari Memory API 路由测试。"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from komari_bot.plugins.komari_memory.api import API_PREFIX, register_memory_api


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
        "importance_current": 4.5,
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
            "traits": {"喜欢的食物": {"value": "布丁"}} if key == "user_profile" else {},
            "records": [],
        },
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
        self.list_conversation_calls: list[dict[str, object]] = []
        self.update_conversation_calls: list[tuple[int, dict[str, object]]] = []
        self.list_profile_calls: list[dict[str, object]] = []
        self.list_history_calls: list[dict[str, object]] = []

    async def list_conversations(self, **kwargs: object) -> tuple[list[dict[str, object]], int]:
        self.list_conversation_calls.append(dict(kwargs))
        return [self.conversations[1]], len(self.conversations)

    async def get_conversation_entry(self, conversation_id: int) -> dict[str, object] | None:
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
        updated.update({key: value for key, value in kwargs.items() if value is not None})
        self.conversations[conversation_id] = updated
        return updated

    async def delete_conversation_entry(self, conversation_id: int) -> bool:
        return self.conversations.pop(conversation_id, None) is not None

    async def list_user_profile_rows(self, **kwargs: object) -> tuple[list[dict[str, object]], int]:
        self.list_profile_calls.append(dict(kwargs))
        return [self.user_profiles[("g1", "u1")]], len(self.user_profiles)

    async def list_interaction_history_rows(
        self,
        **kwargs: object,
    ) -> tuple[list[dict[str, object]], int]:
        self.list_history_calls.append(dict(kwargs))
        return [self.interaction_histories[("g1", "u1")]], len(
            self.interaction_histories
        )

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


def _build_client(service: _FakeMemoryService | None) -> TestClient:
    app = FastAPI()
    register_memory_api(
        app,
        api_token="secret-token",
        allowed_origins=["https://ui.example.com"],
        service_getter=lambda: service,
    )
    return TestClient(app)


def test_memory_routes_require_token_and_handle_cors() -> None:
    client = _build_client(_FakeMemoryService())

    unauthorized = client.get(f"{API_PREFIX}/conversations")
    assert unauthorized.status_code == 401

    preflight = client.options(
        f"{API_PREFIX}/conversations",
        headers={
            "Origin": "https://ui.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "https://ui.example.com"


def test_memory_routes_return_503_when_service_unavailable() -> None:
    client = _build_client(None)

    response = client.get(
        f"{API_PREFIX}/conversations",
        headers={"Authorization": "Bearer secret-token"},
    )

    assert response.status_code == 503
    assert "服务未初始化" in response.json()["detail"]


def test_conversation_routes_forward_filters_and_support_crud() -> None:
    service = _FakeMemoryService()
    client = _build_client(service)
    headers = {"Authorization": "Bearer secret-token"}

    listed = client.get(
        f"{API_PREFIX}/conversations",
        params={"group_id": "g1", "participant": "u1", "q": "布丁", "limit": 5, "offset": 2},
        headers=headers,
    )
    detail = client.get(f"{API_PREFIX}/conversations/1", headers=headers)
    created = client.post(
        f"{API_PREFIX}/conversations",
        json={
            "group_id": "g1",
            "summary": "  新记忆  ",
            "participants": ["u1", " u2 "],
            "importance_initial": 5,
        },
        headers=headers,
    )
    updated = client.patch(
        f"{API_PREFIX}/conversations/1",
        json={"summary": "改过的记忆", "importance_current": 4.2},
        headers=headers,
    )
    missing_patch = client.patch(
        f"{API_PREFIX}/conversations/999",
        json={"summary": "不存在"},
        headers=headers,
    )
    deleted = client.delete(f"{API_PREFIX}/conversations/2", headers=headers)

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
    assert updated.json()["importance_current"] == 4.2
    assert service.update_conversation_calls[0][1]["summary"] == "改过的记忆"
    assert missing_patch.status_code == 404
    assert deleted.status_code == 204


def test_entity_routes_support_list_get_put_delete_and_validate_user_id_conflict() -> None:
    service = _FakeMemoryService()
    client = _build_client(service)
    headers = {"Authorization": "Bearer secret-token"}

    listed_profiles = client.get(
        f"{API_PREFIX}/user-profiles",
        params={"group_id": "g1", "user_id": "u1", "q": "阿明", "limit": 5, "offset": 1},
        headers=headers,
    )
    listed_histories = client.get(
        f"{API_PREFIX}/interaction-histories",
        params={"group_id": "g1", "user_id": "u1", "q": "聊天"},
        headers=headers,
    )
    detail = client.get(f"{API_PREFIX}/user-profiles/g1/u1", headers=headers)
    put_profile = client.put(
        f"{API_PREFIX}/user-profiles/g1/u1",
        params={"importance": 5},
        json={"user_id": "u1", "display_name": "阿明", "traits": {"喜欢的食物": {"value": "布丁"}}},
        headers=headers,
    )
    conflict = client.put(
        f"{API_PREFIX}/interaction-histories/g1/u1",
        json={"user_id": "u2", "summary": "冲突"},
        headers=headers,
    )
    put_history = client.put(
        f"{API_PREFIX}/interaction-histories/g1/u1",
        json={"user_id": "u1", "summary": "一起打游戏", "records": []},
        headers=headers,
    )
    deleted_profile = client.delete(f"{API_PREFIX}/user-profiles/g1/u1", headers=headers)
    deleted_history = client.delete(
        f"{API_PREFIX}/interaction-histories/g1/u1",
        headers=headers,
    )

    assert listed_profiles.status_code == 200
    assert listed_profiles.json()["items"][0]["key"] == "user_profile"
    assert service.list_profile_calls == [
        {
            "limit": 5,
            "offset": 1,
            "group_id": "g1",
            "user_id": "u1",
            "query": "阿明",
        }
    ]
    assert listed_histories.status_code == 200
    assert service.list_history_calls == [
        {
            "limit": 20,
            "offset": 0,
            "group_id": "g1",
            "user_id": "u1",
            "query": "聊天",
        }
    ]
    assert detail.status_code == 200
    assert put_profile.status_code == 200
    assert put_profile.json()["importance"] == 5
    assert conflict.status_code == 422
    assert "user_id" in conflict.json()["detail"]
    assert put_history.status_code == 200
    assert put_history.json()["value"]["summary"] == "一起打游戏"
    assert deleted_profile.status_code == 204
    assert deleted_history.status_code == 204
