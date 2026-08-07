"""user_ban 管理 API 测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI

from komari_bot.plugins.user_ban import api
from komari_bot.plugins.user_ban.models import (
    BanListPage,
    BanMutationResult,
    BanRecord,
    BanScope,
    BanTargetScope,
    UserBanStatus,
)
from komari_bot.plugins.user_ban.service import BanServiceUnavailableError

if TYPE_CHECKING:
    from nonebug import App

    from komari_bot.management.management_audit import ManagementAuditEvent


def _record(
    user_id: str,
    scope: BanScope,
    *,
    reason: str | None = "刷屏广告",
    expires_at: datetime | None = None,
) -> BanRecord:
    timestamp = datetime(2026, 7, 13, 20, 0, tzinfo=UTC)
    return BanRecord(
        user_id=user_id,
        ban_scope=scope,
        operator_id=api.MANAGEMENT_API_OPERATOR_ID,
        reason=reason,
        expires_at=expires_at,
        created_at=timestamp,
        updated_at=timestamp,
    )


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.records: tuple[BanRecord, ...] = (_record("10086", "chat"),)

    async def list_bans(self, **kwargs: object) -> BanListPage:
        self.calls.append(("list", dict(kwargs)))
        return BanListPage(
            items=(UserBanStatus(user_id="10086", records=self.records),),
            total=1,
            limit=cast("int", kwargs["limit"]),
            offset=cast("int", kwargs["offset"]),
        )

    async def get_status(self, user_id: str) -> UserBanStatus:
        self.calls.append(("status", {"user_id": user_id}))
        return UserBanStatus(user_id=user_id, records=self.records)

    async def ban_user(self, **kwargs: object) -> BanMutationResult:
        self.calls.append(("ban", dict(kwargs)))
        user_id = cast("str", kwargs["user_id"])
        target_scope = cast("BanTargetScope", kwargs["target_scope"])
        scopes: tuple[BanScope, ...] = (
            ("chat", "command") if target_scope == "all" else (target_scope,)
        )
        records = tuple(
            _record(
                user_id,
                scope,
                reason=cast("str | None", kwargs["reason"]),
                expires_at=cast("datetime | None", kwargs["expires_at"]),
            )
            for scope in scopes
        )
        self.records = records
        return BanMutationResult(
            status=UserBanStatus(user_id=user_id, records=records),
            target_scope=target_scope,
            changed=True,
            mutation_kind="created",
            affected_records=records,
        )

    async def unban_user(self, **kwargs: object) -> BanMutationResult:
        self.calls.append(("unban", dict(kwargs)))
        user_id = cast("str", kwargs["user_id"])
        target_scope = cast("BanTargetScope", kwargs["target_scope"])
        removed = self.records
        self.records = ()
        return BanMutationResult(
            status=UserBanStatus(user_id=user_id),
            target_scope=target_scope,
            changed=True,
            mutation_kind="removed",
            affected_records=removed,
        )


class _FailingService(_Service):
    async def get_status(self, user_id: str) -> UserBanStatus:
        del user_id
        raise BanServiceUnavailableError("数据库离线")


class _Bot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call_api(self, name: str, **kwargs: object) -> object:
        self.calls.append((name, dict(kwargs)))
        return {"message_id": 1}


def _app(
    service: object,
    audit_events: list[ManagementAuditEvent] | None = None,
) -> FastAPI:
    async def _record_audit(event: ManagementAuditEvent) -> None:
        if audit_events is not None:
            audit_events.append(event)

    app = FastAPI()
    api.register_user_ban_api(
        app,
        api_token=[
            {
                "credential_id": "user-ban-operator",
                "token": "secret-token-00000000",
                "permissions": ["*"],
            }
        ],
        allowed_origins=[],
        service_getter=lambda: cast("Any", service),
        audit_recorder=_record_audit,
    )
    return app


def _write_headers(request_id: str) -> dict[str, str]:
    return {
        "Authorization": "Bearer secret-token-00000000",
        "X-Komari-Change-Reason": "处理违规用户",
        "X-Request-ID": request_id,
    }


@pytest.mark.asyncio
async def test_api_requires_token_and_supports_list_and_status(
    app: App,
) -> None:
    service = _Service()
    headers = {"Authorization": "Bearer secret-token-00000000"}

    async with app.test_server(asgi=cast("Any", _app(service))) as ctx:
        client = ctx.get_client()
        unauthorized = await client.get(f"{api.API_PREFIX}/bans")
        listed = await client.get(
            f"{api.API_PREFIX}/bans?scope=chat&page=1&page_size=20",
            headers=headers,
        )
        status = await client.get(
            f"{api.API_PREFIX}/bans/10086",
            headers=headers,
        )

    assert unauthorized.status_code == 401
    assert listed.status_code == 200
    assert listed.json()["items"][0]["records"][0]["reason"] == "刷屏广告"
    assert status.status_code == 200
    assert status.json()["active_scopes"] == ["chat"]


@pytest.mark.asyncio
async def test_api_creates_and_deletes_ban_with_notification_result(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _Service()
    bot = _Bot()
    audit_events: list[ManagementAuditEvent] = []
    monkeypatch.setattr(api, "get_first_available_bot", lambda: bot)

    async with app.test_server(
        asgi=cast("Any", _app(service, audit_events))
    ) as ctx:
        client = ctx.get_client()
        created = await client.post(
            f"{api.API_PREFIX}/bans",
            headers=_write_headers("user-ban-create"),
            json={
                "user_id": "10086",
                "scope": "all",
                "duration": "7d",
                "reason": " 多次刷屏 ",
            },
        )
        deleted = await client.delete(
            f"{api.API_PREFIX}/bans/10086/all",
            headers=_write_headers("user-ban-delete"),
        )

    assert created.status_code == 200
    created_payload = created.json()
    assert created_payload["action"] == "created"
    assert created_payload["status"]["active_scopes"] == ["chat", "command"]
    assert created_payload["status"]["records"][0]["reason"] == "多次刷屏"
    assert created_payload["notification"] == {
        "attempted": True,
        "sent": True,
        "error": None,
    }
    ban_call = next(call for call in service.calls if call[0] == "ban")
    assert ban_call[1]["operator_id"] == "user-ban-operator"
    assert isinstance(ban_call[1]["expires_at"], datetime)

    assert deleted.status_code == 200
    assert deleted.json()["action"] == "removed"
    assert deleted.json()["notification"]["sent"] is True
    assert [name for name, _ in bot.calls] == ["send_private_msg", "send_private_msg"]
    assert [event.outcome for event in audit_events] == [
        "started",
        "succeeded",
        "started",
        "succeeded",
    ]
    assert {event.operator_id for event in audit_events} == {"user-ban-operator"}
    assert {event.request_id for event in audit_events} == {
        "user-ban-create",
        "user-ban-delete",
    }
    serialized_audit = str([event.to_dict() for event in audit_events])
    assert "10086" not in serialized_audit
    assert "多次刷屏" not in serialized_audit


@pytest.mark.asyncio
async def test_api_validates_input_and_reports_offline_notification(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _Service()
    monkeypatch.setattr(api, "get_first_available_bot", lambda: None)
    async with app.test_server(asgi=cast("Any", _app(service))) as ctx:
        client = ctx.get_client()
        invalid = await client.post(
            f"{api.API_PREFIX}/bans",
            headers=_write_headers("user-ban-invalid"),
            json={"user_id": "010086", "scope": "chat", "duration": "1y"},
        )
        created = await client.post(
            f"{api.API_PREFIX}/bans",
            headers=_write_headers("user-ban-offline"),
            json={"user_id": "10086", "scope": "chat"},
        )

    assert invalid.status_code == 422
    assert created.status_code == 200
    assert created.json()["notification"] == {
        "attempted": False,
        "sent": False,
        "error": "Bot 不在线，无法发送私信",
    }


@pytest.mark.asyncio
async def test_api_maps_storage_failure_to_503(app: App) -> None:
    headers = {"Authorization": "Bearer secret-token-00000000"}
    async with app.test_server(asgi=cast("Any", _app(_FailingService()))) as ctx:
        response = await ctx.get_client().get(
            f"{api.API_PREFIX}/bans/10086",
            headers=headers,
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "用户封禁存储暂不可用"
