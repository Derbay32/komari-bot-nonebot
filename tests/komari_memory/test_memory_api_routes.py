"""Komari Memory API 路由测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI

from komari_bot.plugins.komari_memory.api import API_PREFIX, register_memory_api

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


def _build_app(service: _FakeMemoryService | None) -> FastAPI:
    api_app = FastAPI()
    register_memory_api(
        api_app,
        api_token="secret-token",
        allowed_origins=["https://ui.example.com"],
        service_getter=lambda: service,
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
            headers={"Authorization": "Bearer secret-token"},
        )

    assert response.status_code == 503
    assert "服务未初始化" in response.json()["detail"]


@pytest.mark.asyncio
async def test_conversation_routes_forward_filters_and_support_crud(app: App) -> None:
    service = _FakeMemoryService()
    headers = {"Authorization": "Bearer secret-token"}

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
async def test_entity_routes_list_get_upsert_delete_and_validate(app: App) -> None:
    service = _FakeMemoryService()
    headers = {"Authorization": "Bearer secret-token"}

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
        histories = await client.get(
            _with_query(
                f"{API_PREFIX}/interaction-histories",
                group_id="g1",
                user_id="u1",
                q="聊天",
                offset=1,
            ),
            headers=headers,
        )
        profile_detail = await client.get(
            f"{API_PREFIX}/user-profiles/g1/u1",
            headers=headers,
        )
        history_detail = await client.get(
            f"{API_PREFIX}/interaction-histories/g1/u1",
            headers=headers,
        )
        profile_put = await client.put(
            f"{API_PREFIX}/user-profiles/g1/u2",
            json={
                "user_id": "u2",
                "display_name": "小李",
                "traits": {"爱好": {"value": "游戏"}},
            },
            headers=headers,
        )
        history_put = await client.put(
            f"{API_PREFIX}/interaction-histories/g1/u2",
            json={"user_id": "u2", "summary": "最近常聊游戏", "records": []},
            headers=headers,
        )
        mismatch = await client.put(
            f"{API_PREFIX}/user-profiles/g1/u3",
            json={"user_id": "u4"},
            headers=headers,
        )
        bad_body = await client.put(
            f"{API_PREFIX}/interaction-histories/g1/u3",
            json=["not-an-object"],
            headers=headers,
        )
        deleted_profile = await client.delete(
            f"{API_PREFIX}/user-profiles/g1/u1",
            headers=headers,
        )
        deleted_history = await client.delete(
            f"{API_PREFIX}/interaction-histories/g1/u1",
            headers=headers,
        )

    assert profiles.status_code == 200
    assert profiles.json()["total"] == 1
    assert histories.status_code == 200
    assert histories.json()["items"][0]["key"] == "interaction_history"
    assert service.list_profile_calls == [
        {
            "limit": 3,
            "offset": 0,
            "group_id": "g1",
            "user_id": "u1",
            "query": "布丁",
        }
    ]
    assert service.list_history_calls == [
        {
            "limit": 20,
            "offset": 1,
            "group_id": "g1",
            "user_id": "u1",
            "query": "聊天",
        }
    ]
    assert profile_detail.status_code == 200
    assert history_detail.status_code == 200
    assert profile_put.status_code == 200
    assert profile_put.json()["value"]["display_name"] == "小李"
    assert history_put.status_code == 200
    assert history_put.json()["value"]["summary"] == "最近常聊游戏"
    assert mismatch.status_code == 422
    assert "user_id" in mismatch.json()["detail"]
    assert bad_body.status_code == 422
    assert deleted_profile.status_code == 204
    assert deleted_history.status_code == 204


@pytest.mark.asyncio
async def test_entity_routes_return_404_for_missing_rows(app: App) -> None:
    headers = {"Authorization": "Bearer secret-token"}

    async with app.test_server(asgi=cast("Any", _build_app(_FakeMemoryService()))) as ctx:
        client = ctx.get_client()
        missing_profile = await client.get(
            f"{API_PREFIX}/user-profiles/g1/u9",
            headers=headers,
        )
        missing_history = await client.delete(
            f"{API_PREFIX}/interaction-histories/g1/u9",
            headers=headers,
        )

    assert missing_profile.status_code == 404
    assert missing_history.status_code == 404
