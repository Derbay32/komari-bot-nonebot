"""回复履约受审计管理 API 的权限、内容与运维闭环验收。"""

from __future__ import annotations

import inspect
import json
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

import pytest
from fastapi import FastAPI

from komari_bot.plugins.komari_management import reply_fulfillment_api
from komari_bot.plugins.komari_management.reply_fulfillment_api import (
    API_PREFIX,
    create_reply_fulfillment_router,
)

if TYPE_CHECKING:
    from nonebug import App

    from komari_bot.management.management_audit import ManagementAuditEvent

_CREDENTIALS = (
    {
        "credential_id": "fulfillment-reader",
        "token": "fulfillment-read-token-0000",
        "permissions": ["reply_fulfillment:read"],
    },
    {
        "credential_id": "fulfillment-manager",
        "token": "fulfillment-manage-token-00",
        "permissions": ["reply_fulfillment:manage"],
    },
    {
        "credential_id": "unrelated-reader",
        "token": "unrelated-reader-token-000",
        "permissions": ["announce:read"],
    },
)

_SUMMARY = {
    "fulfillment_id": "reply-safe-id",
    "request_trace_id": "trace-safe-id",
    "trigger_message_id": "message-safe-id",
    "group_id": "group-safe-id",
    "status": "pending_confirmation",
    "reply_fingerprint": "a" * 64,
    "prepared_at": "2026-08-11T00:00:00+00:00",
    "send_started_at": "2026-08-11T00:00:01+00:00",
    "delivered_at": None,
    "platform_message_id": None,
    "not_delivered_at": None,
    "completed_at": None,
    "commitments": [
        {
            "commitment_type": "favorability_adjustment",
            "state": "FAILED",
            "attempt_count": 3,
            "next_retry_at": None,
            "last_error_code": "service_unavailable",
            "completed_at": None,
        }
    ],
}
_DETAIL = {
    **_SUMMARY,
    "reply_target_message_id": "message-target-safe-id",
    "reply_content": "pending-body-canary",
}


class _FakeOpsService:
    instances: ClassVar[list[_FakeOpsService]] = []

    def __init__(self) -> None:
        self.list_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []
        self.delivered_calls: list[tuple[str, str | None]] = []
        self.not_delivered_calls: list[str] = []
        self.resume_calls: list[tuple[str, str]] = []
        self.detail: dict[str, Any] | None = deepcopy(_DETAIL)
        self.delivered_error: BaseException | None = None
        self.not_delivered_error: BaseException | None = None
        self.resume_error: BaseException | None = None
        _FakeOpsService.instances.append(self)

    async def list_fulfillments(
        self,
        *,
        status: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        self.list_calls.append({"status": status, "limit": limit, "offset": offset})
        return {
            "items": [deepcopy(_SUMMARY)],
            "total": 1,
            "limit": limit,
            "offset": offset,
        }

    async def get_fulfillment(self, fulfillment_id: str) -> dict[str, Any] | None:
        self.get_calls.append(fulfillment_id)
        return deepcopy(self.detail)

    async def confirm_delivered(
        self,
        fulfillment_id: str,
        *,
        platform_message_id: str | None,
    ) -> dict[str, Any]:
        self.delivered_calls.append((fulfillment_id, platform_message_id))
        if self.delivered_error is not None:
            raise self.delivered_error
        return {
            "fulfillment_id": fulfillment_id,
            "status": "processing",
            "idempotent_replay": False,
            "platform_message_id": platform_message_id,
        }

    async def confirm_not_delivered(self, fulfillment_id: str) -> dict[str, Any]:
        self.not_delivered_calls.append(fulfillment_id)
        if self.not_delivered_error is not None:
            raise self.not_delivered_error
        return {
            "fulfillment_id": fulfillment_id,
            "status": "not_delivered",
            "idempotent_replay": False,
            "reservation_released": True,
        }

    async def resume_commitment(
        self,
        fulfillment_id: str,
        *,
        commitment_type: str,
    ) -> dict[str, Any]:
        self.resume_calls.append((fulfillment_id, commitment_type))
        if self.resume_error is not None:
            raise self.resume_error
        return {
            "fulfillment_id": fulfillment_id,
            "status": "processing",
            "commitment_type": commitment_type,
            "state": "PENDING",
        }


def _headers(token: str, *, audit: bool = False) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if audit:
        headers.update(
            {
                "X-Komari-Change-Reason": "人工核对回复送达事实",
                "X-Request-ID": "fulfillment-request-1",
            }
        )
    return headers


def _build_app(
    service: _FakeOpsService | None,
    audit_events: list[ManagementAuditEvent],
) -> FastAPI:
    async def _record(event: ManagementAuditEvent) -> None:
        audit_events.append(event)

    api_app = FastAPI()
    api_app.include_router(
        create_reply_fulfillment_router(
            api_token=_CREDENTIALS,
            service_getter=lambda: service,
            audit_recorder=_record,
        )
    )
    return api_app


@pytest.mark.asyncio
async def test_read_permission_projects_safe_list_and_pending_detail(app: App) -> None:
    service = _FakeOpsService()
    events: list[ManagementAuditEvent] = []

    async with app.test_server(asgi=cast("Any", _build_app(service, events))) as ctx:
        client = ctx.get_client()
        listed = await client.get(
            f"{API_PREFIX}/fulfillments?status=pending_confirmation&limit=25&offset=5",
            headers=_headers("fulfillment-read-token-0000"),
        )
        detail = await client.get(
            f"{API_PREFIX}/fulfillments/reply-safe-id",
            headers=_headers("fulfillment-read-token-0000"),
        )

    assert listed.status_code == 200
    assert listed.json()["items"] == [_SUMMARY]
    assert service.list_calls == [
        {"status": "pending_confirmation", "limit": 25, "offset": 5}
    ]
    assert detail.status_code == 200
    assert detail.json() == _DETAIL
    serialized = json.dumps(
        {"list": listed.json(), "detail": detail.json()},
        ensure_ascii=False,
    )
    for forbidden in (
        "trigger_user_id",
        "bot_self_id",
        "adapter_name",
        "lease_owner",
        "payload",
        "raw-user-canary",
        "internal-owner-canary",
    ):
        assert forbidden not in serialized
    assert events == []


@pytest.mark.asyncio
async def test_management_permission_can_read_and_unrelated_permission_is_denied(
    app: App,
) -> None:
    service = _FakeOpsService()
    events: list[ManagementAuditEvent] = []

    async with app.test_server(asgi=cast("Any", _build_app(service, events))) as ctx:
        client = ctx.get_client()
        no_token = await client.get(f"{API_PREFIX}/fulfillments")
        unrelated = await client.get(
            f"{API_PREFIX}/fulfillments",
            headers=_headers("unrelated-reader-token-000"),
        )
        manager_read = await client.get(
            f"{API_PREFIX}/fulfillments",
            headers=_headers("fulfillment-manage-token-00"),
        )
        reader_write = await client.post(
            f"{API_PREFIX}/fulfillments/reply-safe-id/confirm-delivered",
            headers=_headers("fulfillment-read-token-0000", audit=True),
            json={"platform_message_id": "platform-safe-id"},
        )

    assert no_token.status_code == 401
    assert unrelated.status_code == 403
    assert manager_read.status_code == 200
    assert reader_write.status_code == 403


@pytest.mark.asyncio
async def test_write_requires_reason_and_explicit_request_id(app: App) -> None:
    service = _FakeOpsService()
    events: list[ManagementAuditEvent] = []
    base = _headers("fulfillment-manage-token-00")

    async with app.test_server(asgi=cast("Any", _build_app(service, events))) as ctx:
        client = ctx.get_client()
        missing_both = await client.post(
            f"{API_PREFIX}/fulfillments/reply-safe-id/confirm-not-delivered",
            headers=base,
        )
        missing_request_id = await client.post(
            f"{API_PREFIX}/fulfillments/reply-safe-id/confirm-not-delivered",
            headers={**base, "X-Komari-Change-Reason": "核对未送达"},
        )
        invalid_request_id = await client.post(
            f"{API_PREFIX}/fulfillments/reply-safe-id/confirm-not-delivered",
            headers={
                **base,
                "X-Komari-Change-Reason": "核对未送达",
                "X-Request-ID": "invalid request id",
            },
        )

    assert missing_both.status_code == 400
    assert missing_request_id.status_code == 400
    assert "X-Request-ID" in missing_request_id.json()["detail"]
    assert invalid_request_id.status_code == 422
    assert service.not_delivered_calls == []
    assert events == []


@pytest.mark.asyncio
async def test_write_routes_delegate_and_emit_safe_audit_events(app: App) -> None:
    service = _FakeOpsService()
    events: list[ManagementAuditEvent] = []
    headers = _headers("fulfillment-manage-token-00", audit=True)

    async with app.test_server(asgi=cast("Any", _build_app(service, events))) as ctx:
        client = ctx.get_client()
        delivered = await client.post(
            f"{API_PREFIX}/fulfillments/reply-safe-id/confirm-delivered",
            headers=headers,
            json={"platform_message_id": "platform-safe-id"},
        )
        not_delivered = await client.post(
            f"{API_PREFIX}/fulfillments/reply-safe-id/confirm-not-delivered",
            headers={**headers, "X-Request-ID": "fulfillment-request-2"},
        )
        resumed = await client.post(
            f"{API_PREFIX}/fulfillments/reply-safe-id/commitments/favorability_adjustment/resume",
            headers={**headers, "X-Request-ID": "fulfillment-request-3"},
        )

    assert [delivered.status_code, not_delivered.status_code, resumed.status_code] == [
        200,
        200,
        200,
    ]
    assert service.delivered_calls == [("reply-safe-id", "platform-safe-id")]
    assert service.not_delivered_calls == ["reply-safe-id"]
    assert service.resume_calls == [
        ("reply-safe-id", "favorability_adjustment")
    ]
    assert [event.outcome for event in events] == [
        "started",
        "succeeded",
        "started",
        "succeeded",
        "started",
        "succeeded",
    ]
    assert {event.operator_id for event in events} == {"fulfillment-manager"}
    assert {event.action for event in events} == {
        "reply_fulfillment.confirm_delivered",
        "reply_fulfillment.confirm_not_delivered",
        "reply_fulfillment.resume_commitment",
    }
    assert all(event.target_hash for event in events)
    audit_text = json.dumps(
        [event.to_dict() for event in events],
        ensure_ascii=False,
    )
    for forbidden in (
        "reply-safe-id",
        "pending-body-canary",
        "message-safe-id",
        "group-safe-id",
        "platform-safe-id",
        "fulfillment-manage-token-00",
    ):
        assert forbidden not in audit_text


@pytest.mark.asyncio
async def test_conflict_not_found_validation_and_failed_audit_are_safe(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _FakeOpsService()
    events: list[ManagementAuditEvent] = []
    headers = _headers("fulfillment-manage-token-00", audit=True)
    conflict = reply_fulfillment_api.ReplyFulfillmentOpsConflictError(
        "raw-conflict-body-canary"
    )
    service.delivered_error = conflict

    async with app.test_server(asgi=cast("Any", _build_app(service, events))) as ctx:
        response = await ctx.get_client().post(
            f"{API_PREFIX}/fulfillments/reply-safe-id/confirm-delivered",
            headers=headers,
            json={"platform_message_id": "platform-conflict-canary"},
        )

    assert response.status_code == 409
    assert [event.outcome for event in events] == ["started", "failed"]
    assert events[-1].status_code == 409
    assert events[-1].error_code == "http_409"
    audit_text = json.dumps([event.to_dict() for event in events], ensure_ascii=False)
    assert "raw-conflict-body-canary" not in audit_text
    assert "platform-conflict-canary" not in audit_text

    service.delivered_error = reply_fulfillment_api.ReplyFulfillmentOpsNotFoundError(
        "missing-canary"
    )
    events.clear()
    async with app.test_server(asgi=cast("Any", _build_app(service, events))) as ctx:
        missing = await ctx.get_client().post(
            f"{API_PREFIX}/fulfillments/reply-missing/confirm-delivered",
            headers=headers,
            json={"platform_message_id": None},
        )
    assert missing.status_code == 404

    service.resume_error = reply_fulfillment_api.ReplyFulfillmentOpsValidationError(
        "invalid-canary"
    )
    events.clear()
    async with app.test_server(asgi=cast("Any", _build_app(service, events))) as ctx:
        invalid = await ctx.get_client().post(
            f"{API_PREFIX}/fulfillments/reply-safe-id/commitments/dynamic/resume",
            headers=headers,
        )
    assert invalid.status_code == 422
    del monkeypatch


@pytest.mark.asyncio
async def test_api_returns_503_when_ops_service_is_not_ready(app: App) -> None:
    events: list[ManagementAuditEvent] = []
    async with app.test_server(asgi=cast("Any", _build_app(None, events))) as ctx:
        client = ctx.get_client()
        listed = await client.get(
            f"{API_PREFIX}/fulfillments",
            headers=_headers("fulfillment-read-token-0000"),
        )
        written = await client.post(
            f"{API_PREFIX}/fulfillments/reply-safe-id/confirm-not-delivered",
            headers=_headers("fulfillment-manage-token-00", audit=True),
        )

    assert listed.status_code == 503
    assert written.status_code == 503
    assert [event.outcome for event in events] == ["started", "failed"]


def test_management_api_only_uses_komari_chat_top_level_seam() -> None:
    source = inspect.getsource(reply_fulfillment_api)
    assert "from komari_bot.plugins.komari_chat." not in source
    assert "import komari_bot.plugins.komari_chat." not in source
    assert "ReplyFulfillmentRepository" not in source
    assert hasattr(reply_fulfillment_api, "get_reply_fulfillment_ops_service")

    package_dir = Path(reply_fulfillment_api.__file__).resolve().parent
    offenders = [
        module_file.name
        for module_file in sorted(package_dir.rglob("*.py"))
        if any(
            forbidden in module_file.read_text(encoding="utf-8")
            for forbidden in (
                "komari_chat.repositories",
                "komari_chat.services",
                "komari_chat.handlers",
            )
        )
    ]
    assert offenders == []
