"""Scene 管理 API 仓库准备测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

import pytest

from komari_bot.plugins.komari_management import scene_api


class _FakeSceneRepository:
    instances: ClassVar[list["_FakeSceneRepository"]] = []

    def __init__(self, pg_pool: object) -> None:
        self.pg_pool = pg_pool
        self.schema_ready = False
        self.schema_body_count = 0
        _FakeSceneRepository.instances.append(self)

    async def ensure_schema(self) -> None:
        if self.schema_ready:
            return
        self.schema_body_count += 1
        self.schema_ready = True


@pytest.mark.asyncio
async def test_prepare_repository_reuses_fallback_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    pg_pool = object()
    memory_plugin = SimpleNamespace(
        get_plugin_manager=lambda: SimpleNamespace(pg_pool=pg_pool),
    )

    def _fake_require(plugin_name: str) -> object:
        if plugin_name == "komari_memory":
            return memory_plugin
        raise AssertionError(plugin_name)

    def _fake_get_plugin(plugin_name: str) -> object | None:
        if plugin_name == "komari_decision":
            return None
        raise AssertionError(plugin_name)

    monkeypatch.setattr(scene_api, "get_plugin", _fake_get_plugin)
    monkeypatch.setattr(scene_api, "require", _fake_require)
    monkeypatch.setattr(scene_api, "SceneRepository", _FakeSceneRepository)
    monkeypatch.setattr(scene_api, "_fallback_repository", None)
    _FakeSceneRepository.instances.clear()

    first = await scene_api._prepare_repository()
    second = await scene_api._prepare_repository()

    assert first is second
    assert len(_FakeSceneRepository.instances) == 1
    assert _FakeSceneRepository.instances[0].pg_pool is pg_pool
    assert _FakeSceneRepository.instances[0].schema_body_count == 1


@pytest.mark.asyncio
async def test_prepare_repository_prefers_decision_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    decision_repository = _FakeSceneRepository(object())
    decision_plugin = SimpleNamespace(
        get_plugin_manager=lambda: SimpleNamespace(scene_repository=decision_repository),
    )

    def _fake_get_plugin(plugin_name: str) -> object | None:
        if plugin_name == "komari_decision":
            return SimpleNamespace(module=decision_plugin)
        raise AssertionError(plugin_name)

    monkeypatch.setattr(scene_api, "get_plugin", _fake_get_plugin)
    monkeypatch.setattr(scene_api, "_fallback_repository", None)

    repository = await scene_api._prepare_repository()

    assert repository is decision_repository
    assert decision_repository.schema_body_count == 1
