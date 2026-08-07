"""ProfileStaging 画像提交可靠性测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

import pytest

from komari_bot.memory.profile_operations import ProfileOperation
from komari_bot.plugins.komari_memory.agent.redis_staging import ProfileStaging
from komari_bot.plugins.komari_memory.repositories.entity_repository import (
    UserProfileBatchUpsertError,
    UserProfileBatchUpsertResult,
    UserProfileConcurrentUpdateError,
    UserProfileConflict,
    UserProfileRow,
    UserProfileUpsertError,
)


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.deleted: list[str] = []

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, *, ex: int) -> bool:
        del ex
        self.values[key] = value
        return True

    async def delete(self, key: str) -> int:
        self.deleted.append(key)
        existed = key in self.values or key in self.hashes
        self.values.pop(key, None)
        self.hashes.pop(key, None)
        return 1 if existed else 0

    async def hget(self, key: str, field: str) -> str | None:
        return self.hashes.get(key, {}).get(field)

    async def hmget(self, key: str, fields: list[str]) -> list[str | None]:
        data = self.hashes.get(key, {})
        return [data.get(field) for field in fields]


class _FakeMemory:
    def __init__(self) -> None:
        self.profiles: dict[str, dict[str, Any]] = {}
        self.batch_calls: list[list[dict[str, Any]]] = []
        self.raise_on_batch = False
        self.conflict_on_batch = False
        self.partial_conflict_user_ids: set[str] = set()
        self.partial_error_user_ids: set[str] = set()

    async def get_user_profile(self, *, user_id: str, group_id: str) -> dict[str, Any] | None:
        del group_id
        return self.profiles.get(user_id)

    async def batch_upsert_user_profiles(
        self, payloads: list[dict[str, Any]]
    ) -> UserProfileBatchUpsertResult:
        self.batch_calls.append(payloads)
        if self.conflict_on_batch:
            raise UserProfileConcurrentUpdateError(user_id="u1", group_id="g1")
        if self.raise_on_batch:
            msg = "批量写入失败"
            raise RuntimeError(msg)
        upserted: list[UserProfileRow] = []
        conflicts: list[UserProfileConflict] = []
        errors: list[UserProfileUpsertError] = []
        for payload in payloads:
            if payload["user_id"] in self.partial_conflict_user_ids:
                conflicts.append(
                    UserProfileConflict(
                        user_id=payload["user_id"],
                        group_id=payload["group_id"],
                        snapshot_updated_at=payload.get("snapshot_updated_at"),
                    )
                )
            elif payload["user_id"] in self.partial_error_user_ids:
                errors.append(
                    UserProfileUpsertError(
                        user_id=payload["user_id"],
                        group_id=payload["group_id"],
                        message="模拟单条写入失败",
                    )
                )
            else:
                upserted.append(
                    UserProfileRow(
                        user_id=payload["user_id"],
                        group_id=payload["group_id"],
                        version=1,
                        traits=dict(payload.get("set_traits") or {}),
                        updated_at=datetime.fromisoformat(payload["updated_at"]),
                    )
                )
        result = UserProfileBatchUpsertResult(
            upserted=upserted,
            conflicts=conflicts,
            errors=errors,
        )
        if errors:
            raise UserProfileBatchUpsertError(result)
        return result


def _profile(*, display_name: str, value: str, updated_at: str) -> dict[str, Any]:
    return {
        "version": 1,
        "user_id": "u1",
        "display_name": display_name,
        "traits": {
            "喜欢的游戏": {
                "value": value,
                "category": "preference",
                "importance": 4,
                "updated_at": updated_at,
            }
        },
        "updated_at": updated_at,
    }


def test_commit_conflict_returns_compacted_pg_current_traits() -> None:
    redis = _FakeRedis()
    memory = _FakeMemory()
    snapshot_key = "snapshot:g1:t1"
    redis.hashes[snapshot_key] = {
        "u1": json.dumps(
            _profile(
                display_name="阿明",
                value="塞尔达传说",
                updated_at="2026-06-03T01:00:00+00:00",
            ),
            ensure_ascii=False,
        )
    }
    memory.profiles["u1"] = _profile(
        display_name="阿明",
        value="艾尔登法环",
        updated_at="2026-06-03T01:05:00+00:00",
    )
    staging = ProfileStaging(
        redis,  # type: ignore[arg-type]
        "session-1",
        "g1",
        memory,  # type: ignore[arg-type]
        ttl_seconds=3600,
        snapshot_key=snapshot_key,
    )

    asyncio.run(
        staging.stage(
            [
                ProfileOperation(
                    op="set",
                    user_id="u1",
                    key="喜欢的游戏",
                    value="星之卡比",
                    category="preference",
                    importance=4,
                )
            ]
        )
    )
    result = asyncio.run(staging.commit())

    assert result.status == "conflict"
    assert result.conflicts[0]["pg_current_traits"] == {
        "喜欢的游戏": {
            "value": "艾尔登法环",
            "category": "preference",
            "importance": 4,
        }
    }
    assert memory.batch_calls == []
    assert redis.values


def test_commit_uses_single_batch_call_for_multiple_users() -> None:
    redis = _FakeRedis()
    memory = _FakeMemory()
    staging = ProfileStaging(
        redis,  # type: ignore[arg-type]
        "session-1",
        "g1",
        memory,  # type: ignore[arg-type]
        ttl_seconds=3600,
    )

    asyncio.run(
        staging.stage(
            [
                ProfileOperation(op="add", user_id="u1", key="特征1", value="喜欢 RPG"),
                ProfileOperation(op="add", user_id="u2", key="特征2", value="喜欢 STG"),
            ]
        )
    )
    result = asyncio.run(staging.commit())

    assert result.status == "committed"
    assert result.changed_user_ids == {"u1", "u2"}
    assert len(memory.batch_calls) == 1
    assert [payload["user_id"] for payload in memory.batch_calls[0]] == ["u1", "u2"]
    assert memory.batch_calls[0][0]["set_traits"]["特征1"]["value"] == "喜欢 RPG"
    assert "profile" not in memory.batch_calls[0][0]
    assert redis.values == {}


def test_commit_keeps_staging_when_batch_fails() -> None:
    redis = _FakeRedis()
    memory = _FakeMemory()
    memory.raise_on_batch = True
    staging = ProfileStaging(
        redis,  # type: ignore[arg-type]
        "session-1",
        "g1",
        memory,  # type: ignore[arg-type]
        ttl_seconds=3600,
    )

    asyncio.run(
        staging.stage(
            [ProfileOperation(op="add", user_id="u1", key="特征1", value="喜欢 RPG")]
        )
    )

    with pytest.raises(RuntimeError, match="批量写入失败"):
        asyncio.run(staging.commit())

    assert len(memory.batch_calls) == 1
    assert redis.values


def test_commit_repository_conflict_keeps_staging() -> None:
    redis = _FakeRedis()
    memory = _FakeMemory()
    memory.conflict_on_batch = True
    staging = ProfileStaging(
        redis,  # type: ignore[arg-type]
        "session-1",
        "g1",
        memory,  # type: ignore[arg-type]
        ttl_seconds=3600,
    )

    asyncio.run(
        staging.stage(
            [ProfileOperation(op="add", user_id="u1", key="特征1", value="喜欢 RPG")]
        )
    )
    result = asyncio.run(staging.commit())

    assert result.status == "conflict"
    assert result.committed_count == 0
    assert redis.values


def test_commit_partial_repository_conflict_clears_committed_user_staging() -> None:
    redis = _FakeRedis()
    memory = _FakeMemory()
    memory.partial_conflict_user_ids = {"u2"}
    staging = ProfileStaging(
        redis,  # type: ignore[arg-type]
        "session-1",
        "g1",
        memory,  # type: ignore[arg-type]
        ttl_seconds=3600,
    )

    asyncio.run(
        staging.stage(
            [
                ProfileOperation(op="add", user_id="u1", key="特征1", value="喜欢 RPG"),
                ProfileOperation(op="add", user_id="u2", key="特征2", value="喜欢 STG"),
            ]
        )
    )
    result = asyncio.run(staging.commit())

    assert result.status == "conflict"
    assert result.committed_count == 1
    assert result.changed_user_ids == {"u1"}
    assert result.conflicts == [
        {
            "user_id": "u2",
            "group_id": "g1",
            "snapshot_updated_at": None,
            "reason": "画像提交时检测到并发更新",
        }
    ]
    assert len(memory.batch_calls) == 1
    saved = json.loads(next(iter(redis.values.values())))
    assert sorted(saved["operations"]) == ["u2:特征2"]


def test_commit_partial_repository_error_clears_committed_user_staging() -> None:
    redis = _FakeRedis()
    memory = _FakeMemory()
    memory.partial_error_user_ids = {"u2"}
    staging = ProfileStaging(
        redis,  # type: ignore[arg-type]
        "session-1",
        "g1",
        memory,  # type: ignore[arg-type]
        ttl_seconds=3600,
    )

    asyncio.run(
        staging.stage(
            [
                ProfileOperation(op="add", user_id="u1", key="特征1", value="喜欢 RPG"),
                ProfileOperation(op="add", user_id="u2", key="特征2", value="喜欢 STG"),
            ]
        )
    )
    result = asyncio.run(staging.commit())

    assert result.status == "partial_error"
    assert result.committed_count == 1
    assert result.changed_user_ids == {"u1"}
    assert result.conflicts == [
        {
            "user_id": "u2",
            "group_id": "g1",
            "reason": "画像提交时数据库写入失败: 模拟单条写入失败",
        }
    ]
    assert len(memory.batch_calls) == 1
    saved = json.loads(next(iter(redis.values.values())))
    assert sorted(saved["operations"]) == ["u2:特征2"]
