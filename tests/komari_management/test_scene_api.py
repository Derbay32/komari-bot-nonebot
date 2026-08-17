"""Scene 管理 API 边界测试。

验收目标：
- 场景读写只走判定插件顶层暴露的场景运维服务；
- 判定插件未就绪时优先返回 503，就绪后的领域校验失败映射为 422；
- 管理适配器不重复裁决 required-fixed 规则，也不自建仓储旁路；
- 管理插件内不再存在指向 komari_decision 内部子模块的 import（配置 Schema 豁免）。
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, cast

import pytest
from fastapi import FastAPI

from komari_bot.plugins.komari_decision.services.scene_sync_service import (
    SceneSyncResult,
)
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

_SYNC_RESULT = SceneSyncResult(
    set_id=9,
    created=True,
    reused_existing_set=False,
    inserted_count=3,
    ready_count=2,
    pending_count=1,
)


class _FakeSceneAdminService:
    """场景运维服务桩：记录调用并返回预设行。"""

    instances: ClassVar[list["_FakeSceneAdminService"]] = []

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = [dict(_SCENE_ROW)]
        self.list_calls: list[bool] = []
        self.get_calls: list[str] = []
        self.upsert_calls: list[dict[str, Any]] = []
        self.upsert_error: ValueError | None = None
        self.sync_calls = 0
        self.sync_error: Exception | None = None
        self.sync_result: SceneSyncResult | None = None
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
        if self.upsert_error is not None:
            raise self.upsert_error
        row = dict(_SCENE_ROW)
        row.update(call)
        row["content_hash"] = "hash-updated"
        return row

    async def sync_scenes(self) -> SceneSyncResult:
        self.sync_calls += 1
        if self.sync_error is not None:
            raise self.sync_error
        if self.sync_result is None:
            message = "sync_result 未设置"
            raise AssertionError(message)
        return self.sync_result


class _RecordingAuditRecorder:
    """捕获管理审计事件的记录器。"""

    def __init__(self) -> None:
        self.events: list[ManagementAuditEvent] = []

    async def __call__(self, event: ManagementAuditEvent) -> None:
        self.events.append(event)


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
async def test_scene_put_returns_503_before_required_fixed_validation(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    _patch_admin_service(monkeypatch, None)

    async with app.test_server(asgi=cast("Any", _build_app())) as ctx:
        client = ctx.get_client()
        response = await client.put(
            f"{API_PREFIX}/scenes/NOISE",
            headers=_write_headers(),
            json={
                "scene_type": "general",
                "content_text": "非法改型",
                "enabled": False,
                "order_index": 0,
            },
        )

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
async def test_scene_put_maps_ready_service_domain_validation_to_422(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    admin = _FakeSceneAdminService()
    admin.upsert_error = ValueError("领域拒绝 required-fixed 改型")
    _patch_admin_service(monkeypatch, admin)

    async with app.test_server(asgi=cast("Any", _build_app())) as ctx:
        client = ctx.get_client()
        response = await client.put(
            f"{API_PREFIX}/scenes/NOISE",
            headers=_write_headers(),
            json={
                "scene_type": "general",
                "content_text": "非法改型",
                "enabled": True,
                "order_index": 0,
            },
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "领域拒绝 required-fixed 改型"
    assert admin.upsert_calls == [
        {
            "scene_key": "NOISE",
            "scene_type": "general",
            "content_text": "非法改型",
            "enabled": True,
            "order_index": 0,
        }
    ]


@pytest.mark.asyncio
async def test_scene_patch_keeps_successful_update_response(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    admin = _FakeSceneAdminService()
    _patch_admin_service(monkeypatch, admin)

    async with app.test_server(asgi=cast("Any", _build_app())) as ctx:
        client = ctx.get_client()
        response = await client.patch(
            f"{API_PREFIX}/scenes/NOISE",
            headers=_write_headers(),
            json={"content_text": "更新后的噪声内容", "order_index": 9},
        )

    assert response.status_code == 200
    assert response.json()["content_text"] == "更新后的噪声内容"
    assert response.json()["order_index"] == 9
    assert admin.upsert_calls == [
        {
            "scene_key": "NOISE",
            "scene_type": "fixed",
            "content_text": "更新后的噪声内容",
            "enabled": True,
            "order_index": 9,
        }
    ]


@pytest.mark.asyncio
async def test_scene_patch_maps_ready_service_domain_validation_to_422(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    admin = _FakeSceneAdminService()
    admin.upsert_error = ValueError("领域拒绝 required-fixed 禁用")
    _patch_admin_service(monkeypatch, admin)

    async with app.test_server(asgi=cast("Any", _build_app())) as ctx:
        client = ctx.get_client()
        response = await client.patch(
            f"{API_PREFIX}/scenes/NOISE",
            headers=_write_headers(),
            json={"enabled": False},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "领域拒绝 required-fixed 禁用"
    assert admin.upsert_calls == [
        {
            "scene_key": "NOISE",
            "scene_type": "fixed",
            "content_text": "噪声内容",
            "enabled": False,
            "order_index": 0,
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


@pytest.mark.asyncio
async def test_scene_sync_returns_200_with_strict_payload(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    """TSK-179: 成功 200，payload 无 triggered 且 7 个字段全部非 null。"""
    admin = _FakeSceneAdminService()
    admin.sync_result = _SYNC_RESULT
    _patch_admin_service(monkeypatch, admin)

    async with app.test_server(asgi=cast("Any", _build_app())) as ctx:
        client = ctx.get_client()
        response = await client.post(f"{API_PREFIX}/sync", headers=_write_headers())

    assert response.status_code == 200
    payload = response.json()
    assert "triggered" not in payload
    for key in (
        "set_id",
        "created",
        "reused_existing_set",
        "inserted_count",
        "ready_count",
        "pending_count",
        "detail",
    ):
        assert key in payload, f"成功 payload 缺少 {key}"
        assert payload[key] is not None, f"成功 payload 字段 {key} 为 null"
    assert admin.sync_calls == 1


@pytest.mark.asyncio
async def test_scene_sync_returns_503_when_service_not_ready(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    """TSK-179: 服务未就绪 503，禁止 200+false 形态。"""
    _patch_admin_service(monkeypatch, None)

    async with app.test_server(asgi=cast("Any", _build_app())) as ctx:
        client = ctx.get_client()
        response = await client.post(f"{API_PREFIX}/sync", headers=_write_headers())

    assert response.status_code == 503
    assert "未就绪" in response.json()["detail"]


@pytest.mark.asyncio
async def test_scene_sync_maps_value_error_to_422(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    """TSK-179: 就绪后领域校验错误映射为 422。"""
    admin = _FakeSceneAdminService()
    admin.sync_error = ValueError("无效场景定义")
    _patch_admin_service(monkeypatch, admin)

    async with app.test_server(asgi=cast("Any", _build_app())) as ctx:
        client = ctx.get_client()
        response = await client.post(f"{API_PREFIX}/sync", headers=_write_headers())

    assert response.status_code == 422
    assert response.json()["detail"] == "无效场景定义"
    assert admin.sync_calls == 1


@pytest.mark.asyncio
async def test_scene_sync_maps_unexpected_exception_to_500(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    """TSK-179: 非校验异常映射为 500。"""
    admin = _FakeSceneAdminService()
    admin.sync_error = RuntimeError("boom")
    _patch_admin_service(monkeypatch, admin)

    async with app.test_server(asgi=cast("Any", _build_app())) as ctx:
        client = ctx.get_client()
        response = await client.post(f"{API_PREFIX}/sync", headers=_write_headers())

    assert response.status_code == 500
    assert admin.sync_calls == 1


def test_scene_sync_response_schema_is_strict_seven_fields() -> None:
    """TSK-179: 公开 schema 恰好 7 个 required 字段，无 triggered/optional 形态。"""
    fields = scene_api.SceneSyncResponse.model_fields
    assert set(fields) == {
        "set_id",
        "created",
        "reused_existing_set",
        "inserted_count",
        "ready_count",
        "pending_count",
        "detail",
    }
    assert all(field.is_required() for field in fields.values())
    assert "triggered" not in fields


def test_scene_sync_endpoint_avoids_plugin_manager_and_sync_implementation() -> None:
    """TSK-179: /sync 只经 get_scene_admin_service 调 sync_scenes，禁止旁路实现。"""
    source = inspect.getsource(scene_api)
    assert "get_plugin_manager" not in source
    assert 'require("komari_decision")' not in source
    assert "scene_sync" not in source
    assert "build_scene_set" not in source


@pytest.mark.asyncio
async def test_scene_sync_success_records_scene_sync_audit_span(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    """TSK-179: 成功事件归属 scene.sync span 且含 succeeded outcome。"""
    admin = _FakeSceneAdminService()
    admin.sync_result = _SYNC_RESULT
    _patch_admin_service(monkeypatch, admin)
    recorder = _RecordingAuditRecorder()
    api_app = FastAPI()
    api_app.include_router(
        create_scene_router(api_token=_CREDENTIALS, audit_recorder=recorder)
    )

    async with app.test_server(asgi=cast("Any", api_app)) as ctx:
        client = ctx.get_client()
        response = await client.post(f"{API_PREFIX}/sync", headers=_write_headers())

    assert response.status_code == 200
    assert recorder.events
    assert {event.action for event in recorder.events} == {"scene.sync"}
    assert all(
        event.resource == "komari_decision_scene_set" for event in recorder.events
    )
    assert {event.outcome for event in recorder.events} == {"started", "succeeded"}


@pytest.mark.asyncio
async def test_scene_sync_failure_records_failed_scene_sync_audit_span(
    app: App,
    monkeypatch: MonkeyPatch,
) -> None:
    """TSK-179: 异常事件仍归属 scene.sync span 且含 failed outcome。"""
    admin = _FakeSceneAdminService()
    admin.sync_error = RuntimeError("boom")
    _patch_admin_service(monkeypatch, admin)
    recorder = _RecordingAuditRecorder()
    api_app = FastAPI()
    api_app.include_router(
        create_scene_router(api_token=_CREDENTIALS, audit_recorder=recorder)
    )

    async with app.test_server(asgi=cast("Any", api_app)) as ctx:
        client = ctx.get_client()
        response = await client.post(f"{API_PREFIX}/sync", headers=_write_headers())

    assert response.status_code == 500
    assert recorder.events
    assert {event.action for event in recorder.events} == {"scene.sync"}
    assert all(
        event.resource == "komari_decision_scene_set" for event in recorder.events
    )
    assert "failed" in {event.outcome for event in recorder.events}
