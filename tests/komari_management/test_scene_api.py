"""Scene 管理 API 边界测试（ticket #32）。

验收目标：
- 场景读写改走判定插件顶层 `get_scene_admin_service()` 暴露的场景运维服务；
- 自建仓储旁路删除：判定插件未就绪时统一返回 503 服务未就绪；
- 管理插件内不再存在指向 komari_decision 内部子模块的 import（配置 Schema 豁免）。
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, cast

import pytest
from fastapi import FastAPI

from komari_bot.plugins.komari_management import scene_api
from komari_bot.plugins.komari_management.scene_api import (
    API_PREFIX,
    create_scene_router,
)

if TYPE_CHECKING:
    from typing import Any

    from nonebug import App
    from pytest import MonkeyPatch

    from komari_bot.management.management_audit import ManagementAuditEvent

_CREDENTIALS = (
    {
        "credential_id": "scene-operator",
        "token": "scene-token-0000000000",
        "permissions": ["*"],
    },
)

_SCENE_ROW: dict[str, Any] = {
    "scene_key": "NOISE",
    "scene_type": "fixed",
    "content_text": "噪声内容",
    "enabled": True,
    "order_index": 0,
    "content_hash": "hash-noise",
    "updated_at": "2026-08-07 00:00:00",
}


class _FakeSceneAdminService:
    """场景运维服务桩：记录调用并返回预设行。"""

    instances: ClassVar[list["_FakeSceneAdminService"]] = []

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = [dict(_SCENE_ROW)]
        self.list_calls: list[bool] = []
        self.get_calls: list[str] = []
        self.upsert_calls: list[dict[str, Any]] = []
        _FakeSceneAdminService.instances.append(self)

    async def list_scenes(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        self.list_calls.append(enabled_only)
        return [dict(row) for row in self.rows]

    async def get_scene_by_key(self, scene_key: str) -> dict[str, Any] | None:
        self.get_calls.append(scene_key)
        for row in self.rows:
            if row["scene_key"] == scene_key:
                return dict(row)
        return None

    async def upsert_scene(
        self,
        *,
        scene_key: str,
        scene_type: str,
        content_text: str,
        enabled: bool = True,
        order_index: int = 0,
    ) -> dict[str, Any]:
        call = {
            "scene_key": scene_key,
            "scene_type": scene_type,
            "content_text": content_text,
            "enabled": enabled,
            "order_index": order_index,
        }
        self.upsert_calls.append(call)
        row = dict(_SCENE_ROW)
        row.update(call)
        row["content_hash"] = "hash-updated"
        return row


def _build_app() -> FastAPI:
    async def _record_audit(event: ManagementAuditEvent) -> None:
        del event

    api_app = FastAPI()
    api_app.include_router(
        create_scene_router(
            api_token=_CREDENTIALS,
            audit_recorder=_record_audit,
        )
    )
    return api_app


def _read_headers() -> dict[str, str]:
    return {"Authorization": "Bearer scene-token-0000000000"}


def _write_headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer scene-token-0000000000",
        "X-Komari-Change-Reason": "验证场景写入",
        "X-Request-ID": "scene-request",
    }


def _patch_admin_service(
    monkeypatch: MonkeyPatch,
    admin: _FakeSceneAdminService | None,
) -> None:
    monkeypatch.setattr(scene_api, "get_scene_admin_service", lambda: admin)


@pytest.mark.asyncio
async def test_scene_list_uses_admin_service_when_ready(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    admin = _FakeSceneAdminService()
    _patch_admin_service(monkeypatch, admin)

    async with app.test_server(asgi=cast("Any", _build_app())) as ctx:
        client = ctx.get_client()
        response = await client.get(f"{API_PREFIX}/scenes", headers=_read_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["scene_key"] == "NOISE"
    assert payload["items"][0]["scene_type"] == "fixed"
    assert admin.list_calls == [False]


@pytest.mark.asyncio
async def test_scene_get_returns_detail_from_admin_service(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    admin = _FakeSceneAdminService()
    _patch_admin_service(monkeypatch, admin)

    async with app.test_server(asgi=cast("Any", _build_app())) as ctx:
        client = ctx.get_client()
        response = await client.get(
            f"{API_PREFIX}/scenes/NOISE",
            headers=_read_headers(),
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["scene_key"] == "NOISE"
    assert payload["content_text"] == "噪声内容"
    assert admin.get_calls == ["NOISE"]


@pytest.mark.asyncio
async def test_scene_api_returns_503_when_decision_plugin_not_ready(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    """判定插件未就绪时统一报服务未就绪，不再走自建仓储旁路。"""
    _patch_admin_service(monkeypatch, None)

    async with app.test_server(asgi=cast("Any", _build_app())) as ctx:
        client = ctx.get_client()
        listed = await client.get(f"{API_PREFIX}/scenes", headers=_read_headers())
        detail = await client.get(
            f"{API_PREFIX}/scenes/NOISE",
            headers=_read_headers(),
        )
        replaced = await client.put(
            f"{API_PREFIX}/scenes/NOISE",
            headers=_write_headers(),
            json={
                "scene_type": "fixed",
                "content_text": "新内容",
                "enabled": True,
                "order_index": 0,
            },
        )

    for response in (listed, detail, replaced):
        assert response.status_code == 503
        assert "未就绪" in response.json()["detail"]


@pytest.mark.asyncio
async def test_scene_put_delegates_to_admin_service(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    admin = _FakeSceneAdminService()
    _patch_admin_service(monkeypatch, admin)

    async with app.test_server(asgi=cast("Any", _build_app())) as ctx:
        client = ctx.get_client()
        response = await client.put(
            f"{API_PREFIX}/scenes/GREETING",
            headers=_write_headers(),
            json={
                "scene_type": "general",
                "content_text": "打招呼",
                "enabled": False,
                "order_index": 7,
            },
        )

    assert response.status_code == 200
    assert response.json()["scene_key"] == "GREETING"
    assert admin.upsert_calls == [
        {
            "scene_key": "GREETING",
            "scene_type": "general",
            "content_text": "打招呼",
            "enabled": False,
            "order_index": 7,
        }
    ]


@pytest.mark.asyncio
async def test_scene_patch_returns_404_when_scene_missing(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    admin = _FakeSceneAdminService()
    _patch_admin_service(monkeypatch, admin)

    async with app.test_server(asgi=cast("Any", _build_app())) as ctx:
        client = ctx.get_client()
        response = await client.patch(
            f"{API_PREFIX}/scenes/NOT_EXIST",
            headers=_write_headers(),
            json={"content_text": "更新"},
        )

    assert response.status_code == 404


def test_scene_api_module_has_no_repository_fallback() -> None:
    """自建仓储旁路已删除，服务解析只经判定插件顶层接口。"""
    assert not hasattr(scene_api, "_fallback_repository")
    assert not hasattr(scene_api, "_get_repository")
    assert hasattr(scene_api, "get_scene_admin_service")

    source = inspect.getsource(scene_api)
    assert "repositories.scene_repository" not in source
    assert "SceneRepository" not in source


def test_management_package_has_no_decision_internal_imports() -> None:
    """管理插件全包不得 import 判定插件内部子模块（配置 Schema 豁免）。"""
    package_dir = Path(scene_api.__file__).resolve().parent
    forbidden_prefixes = (
        "komari_decision.repositories",
        "komari_decision.services",
        "komari_decision.handlers",
    )
    offenders = [
        f"{module_file.name}: {forbidden}"
        for module_file in sorted(package_dir.rglob("*.py"))
        for forbidden in forbidden_prefixes
        if forbidden in module_file.read_text(encoding="utf-8")
    ]
    assert not offenders, f"管理插件存在判定插件深 import: {offenders}"
