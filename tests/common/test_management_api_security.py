"""统一管理 API 凭据与审计安全测试。"""

from __future__ import annotations

import json
import stat
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import Depends, FastAPI

from komari_bot.common.management_api import (
    ManagementPrincipal,
    create_bearer_auth_dependency,
)
from komari_bot.common.management_audit import (
    JsonlManagementAuditRecorder,
    hash_management_target,
    management_audit_span,
)

if TYPE_CHECKING:
    from pathlib import Path

    from nonebug import App


def _build_auth_app(credential_source: Any) -> FastAPI:
    read_auth = create_bearer_auth_dependency(
        credential_source,
        required_permission="config:read",
    )
    write_auth = create_bearer_auth_dependency(
        credential_source,
        required_permission="config:write",
    )
    api_app = FastAPI()

    @api_app.get("/read")
    async def read_config(
        principal: ManagementPrincipal = Depends(read_auth),  # noqa: FAST002
    ) -> dict[str, object]:
        return {
            "operator_id": principal.operator_id,
            "permissions": sorted(principal.permissions),
        }

    @api_app.post("/write")
    async def write_config(
        principal: ManagementPrincipal = Depends(write_auth),  # noqa: FAST002
    ) -> dict[str, str]:
        return {"operator_id": principal.operator_id}

    return api_app


@pytest.mark.asyncio
async def test_named_credentials_enforce_scope_and_scheduled_revocation(
    app: App,
) -> None:
    credentials = [
        {
            "credential_id": "dashboard-reader",
            "token": "reader-token-00000000",
            "permissions": ["config:read"],
        },
        {
            "credential_id": "config-operator",
            "token": "writer-token-00000000",
            "permissions": ["config:write"],
        },
        {
            "credential_id": "retired-operator",
            "token": "expired-token-0000000",
            "permissions": ["*"],
            "revoked_at": "2000-01-01T00:00:00Z",
        },
    ]

    async with app.test_server(
        asgi=cast("Any", _build_auth_app(credentials))
    ) as ctx:
        client = ctx.get_client()
        reader_read = await client.get(
            "/read",
            headers={"Authorization": "Bearer reader-token-00000000"},
        )
        reader_write = await client.post(
            "/write",
            headers={"Authorization": "Bearer reader-token-00000000"},
        )
        writer_read = await client.get(
            "/read",
            headers={"Authorization": "Bearer writer-token-00000000"},
        )
        expired = await client.get(
            "/read",
            headers={"Authorization": "Bearer expired-token-0000000"},
        )

    assert reader_read.status_code == 200
    assert reader_read.json()["operator_id"] == "dashboard-reader"
    assert reader_write.status_code == 403
    assert writer_read.status_code == 200
    assert writer_read.json()["operator_id"] == "config-operator"
    assert expired.status_code == 401


@pytest.mark.asyncio
async def test_legacy_and_dynamic_credentials_remain_compatible(app: App) -> None:
    state: dict[str, object] = {"credentials": "legacy-token"}
    api_app = _build_auth_app(lambda: state["credentials"])

    async with app.test_server(asgi=cast("Any", api_app)) as ctx:
        client = ctx.get_client()
        legacy = await client.get(
            "/read",
            headers={"Authorization": "Bearer legacy-token"},
        )
        state["credentials"] = [
            {
                "credential_id": "rotated-reader",
                "token": "rotated-token-000000",
                "permissions": ["config:read"],
            }
        ]
        old_after_rotation = await client.get(
            "/read",
            headers={"Authorization": "Bearer legacy-token"},
        )
        rotated = await client.get(
            "/read",
            headers={"Authorization": "Bearer rotated-token-000000"},
        )

    assert legacy.status_code == 200
    assert legacy.json() == {
        "operator_id": "legacy-api-token",
        "permissions": ["*"],
    }
    assert old_after_rotation.status_code == 401
    assert rotated.status_code == 200
    assert rotated.json()["operator_id"] == "rotated-reader"


@pytest.mark.asyncio
async def test_jsonl_audit_records_results_without_sensitive_payloads(
    tmp_path: Path,
) -> None:
    recorder = JsonlManagementAuditRecorder(tmp_path)
    principal = ManagementPrincipal(
        operator_id="release-operator",
        permissions=frozenset({"config:write"}),
    )

    async with management_audit_span(
        principal=principal,
        request_id="audit-success",
        reason="轮换配置",
        action="config.update_field",
        resource="komari_management",
        field_name="api_token",
        target_hash=hash_management_target("super-secret-token-canary"),
        recorder=recorder,
    ) as audit:
        audit.metadata["changed"] = True

    with pytest.raises(RuntimeError, match="sensitive-error-canary"):
        async with management_audit_span(
            principal=principal,
            request_id="audit-failure",
            reason="验证失败审计",
            action="config.update_field",
            resource="komari_management",
            field_name="api_token",
            target_hash=hash_management_target("super-secret-token-canary"),
            recorder=recorder,
        ):
            raise RuntimeError("sensitive-error-canary")

    log_files = list(tmp_path.glob("audit-*.jsonl"))
    assert len(log_files) == 1
    log_text = log_files[0].read_text(encoding="utf-8")
    events = [json.loads(line) for line in log_text.splitlines()]
    assert [event["outcome"] for event in events] == [
        "started",
        "succeeded",
        "started",
        "failed",
    ]
    assert events[1]["metadata"] == {"changed": True}
    assert events[-1]["status_code"] == 500
    assert events[-1]["error_code"] == "http_500"
    assert "super-secret-token-canary" not in log_text
    assert "sensitive-error-canary" not in log_text
    assert stat.S_IMODE(log_files[0].stat().st_mode) == 0o600
