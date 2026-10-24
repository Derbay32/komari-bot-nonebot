"""TSK-181 验收 —— Jev 请求配置经既有统一设置接口读写。

经真实管理 API seam（``register_config_api`` + ``komari_decision`` 强类型
Schema + 配置管理器测试替身）验证：

- 五个请求字段（``jev_base_url`` / ``jev_api_key`` / ``jev_model`` /
  ``jev_http_timeout_seconds`` / ``jev_request_timeout_seconds``）经既有字段写入
  接口保存，并在读取响应中只返回秘密字段掩码；
- TSK-190 后只剩 ``jev_request`` 与 ``jev_execution`` 两个分区，保留字段
  （含新初始化默认启用的 ``plugin_enable``）取值不变；退役字段不再出现；
- 非法截止、空模型、畸形连接地址与畸形凭据在写入边界被拒绝：422 响应、
  审计输出与读取投影都不得回显被拒值（凭据 canary 不得出现），配置保持原样。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI

from komari_bot.config.typed_config import ensure_typed_config_model
from komari_bot.plugins.komari_decision.services.jev_request import (
    configured_jev_request,
)
from komari_bot.plugins.komari_management.config_api import (
    API_PREFIX,
    register_config_api,
)
from komari_bot.plugins.komari_management.managed_resources import (
    ManagedConfigResource,
)
from tests.db.tsk190_legacy_config_support import (
    RETIRED_DECISION_CONFIG_FIELDS,
)
from tests.komari_decision.test_jev_request import (
    CHOICE_QUESTION,
    NOUL_QUESTION,
    ScriptedTypesafeTransport,
    state_payload,
    system_one_payload,
)

if TYPE_CHECKING:
    from nonebug import App

    from komari_bot.plugins.komari_decision.config_schema import (
        KomariDecisionConfigSchema,
    )

RESOURCE_ID = "komari_decision"
READ_HEADERS = {"Authorization": "Bearer secret-token-00000000"}
DUMMY_API_KEY = "tsk-181-dummy-key-0001"
SAVED_BASE_URL = "https://jev-gateway.example.invalid/decision"


class PersistingDecisionConfigManager:
    """按真实管理器语义保存配置的测试替身（字段级 Pydantic 校验 + 重载）。"""

    def __init__(self, config: KomariDecisionConfigSchema) -> None:
        self.config = config
        self.config_source = "postgresql:komari_decision_config:komari_decision"
        self.reload_count = 0

    async def get_async(self) -> KomariDecisionConfigSchema:
        return self.config

    async def update_field_async(
        self, field_name: str, value: object
    ) -> KomariDecisionConfigSchema:
        schema = type(self.config)
        data = self.config.model_dump()
        if field_name not in data:
            msg = f"未知的配置字段: {field_name}"
            raise ValueError(msg)
        data[field_name] = value
        self.config = schema(**data)
        return self.config

    async def reload_async(self) -> KomariDecisionConfigSchema:
        self.reload_count += 1
        return self.config


def _write_headers(request_id: str) -> dict[str, str]:
    return {
        "Authorization": "Bearer secret-token-00000000",
        "X-Komari-Change-Reason": "验证 Jev 请求配置变更",
        "X-Request-ID": request_id,
    }


def _build_app(
    manager: PersistingDecisionConfigManager,
    audit_events: list[object] | None = None,
) -> FastAPI:
    async def _record_audit(event: object) -> None:
        if audit_events is not None:
            audit_events.append(event)

    api_app = FastAPI()
    register_config_api(
        api_app,
        api_token=[
            {
                "credential_id": "config-api-operator",
                "token": "secret-token-00000000",
                "permissions": ["*"],
            }
        ],
        allowed_origins=["https://ui.example.com"],
        resources=(
            ManagedConfigResource(
                resource_id=RESOURCE_ID,
                display_name="Komari Decision",
                manager_getter=lambda: manager,
            ),
        ),
        audit_recorder=_record_audit,
    )
    return api_app


@pytest.fixture
def schema_type() -> type[KomariDecisionConfigSchema]:
    loaded = ensure_typed_config_model(RESOURCE_ID)
    assert loaded is not None, "komari_decision 强类型配置 Schema 必须可加载"
    return cast("type[KomariDecisionConfigSchema]", loaded)


@pytest.fixture
def manager(
    schema_type: type[KomariDecisionConfigSchema],
) -> PersistingDecisionConfigManager:
    return PersistingDecisionConfigManager(schema_type())


@pytest.mark.asyncio
async def test_jev_fields_are_exposed_with_secret_masking(
    app: App, manager: PersistingDecisionConfigManager
) -> None:
    """AC：配置读取只返回秘密掩码，新字段默认值与生效语义来自 Schema。"""
    async with app.test_server(asgi=cast("Any", _build_app(manager))) as ctx:
        client = ctx.get_client()
        detail = await client.get(
            f"{API_PREFIX}/resources/{RESOURCE_ID}", headers=READ_HEADERS
        )

    assert detail.status_code == 200
    payload = detail.json()
    values = payload["values"]
    assert values["jev_base_url"] == ""
    assert values["jev_api_key"] == "******"
    assert values["jev_model"] == "jev-1.14.0"
    assert values["jev_http_timeout_seconds"] == 3.0
    assert values["jev_request_timeout_seconds"] == 3.0
    # TSK-190：新初始化配置默认启用，退役字段不再返回
    assert values["plugin_enable"] is True
    for retired in RETIRED_DECISION_CONFIG_FIELDS:
        assert retired not in values, retired

    field_metadata = payload["field_metadata"]
    assert field_metadata["jev_api_key"] == {
        "secret": True,
        "apply_mode": "immediate",
        "section_id": "jev_request",
    }
    for field_name in (
        "jev_base_url",
        "jev_model",
        "jev_http_timeout_seconds",
        "jev_request_timeout_seconds",
    ):
        assert field_metadata[field_name] == {
            "secret": False,
            "apply_mode": "immediate",
            "section_id": "jev_request",
        }, field_name

    assert payload["field_states"]["jev_api_key"]["configured_value"] == "******"
    assert payload["field_states"]["jev_api_key"]["effective_value"] == "******"
    section_ids = [section["section_id"] for section in payload["sections"]]
    # TSK-190：只剩 Jev 两个分区，场景与总结分区不再暴露
    assert section_ids == ["jev_request", "jev_execution"]
    jev_section = next(
        section
        for section in payload["sections"]
        if section["section_id"] == "jev_request"
    )
    assert jev_section["display_name"]


@pytest.mark.asyncio
async def test_jev_field_writes_persist_and_stay_masked(
    app: App, manager: PersistingDecisionConfigManager
) -> None:
    """AC：统一入口写入后保存新值，秘密字段在响应中仍只返回掩码。"""
    async with app.test_server(asgi=cast("Any", _build_app(manager))) as ctx:
        client = ctx.get_client()
        written_url = await client.patch(
            f"{API_PREFIX}/resources/{RESOURCE_ID}/fields/jev_base_url",
            json={"value": SAVED_BASE_URL},
            headers=_write_headers("jev-config-url"),
        )
        written_key = await client.patch(
            f"{API_PREFIX}/resources/{RESOURCE_ID}/fields/jev_api_key",
            json={"value": DUMMY_API_KEY},
            headers=_write_headers("jev-config-key"),
        )
        written_model = await client.patch(
            f"{API_PREFIX}/resources/{RESOURCE_ID}/fields/jev_model",
            json={"value": "jev-1.14.0"},
            headers=_write_headers("jev-config-model"),
        )
        detail = await client.get(
            f"{API_PREFIX}/resources/{RESOURCE_ID}", headers=READ_HEADERS
        )

    assert written_url.status_code == 200
    assert written_key.status_code == 200
    assert written_model.status_code == 200
    assert written_key.json()["values"]["jev_api_key"] == "******"
    assert DUMMY_API_KEY not in written_key.text
    assert detail.json()["values"]["jev_base_url"] == SAVED_BASE_URL
    assert detail.json()["values"]["jev_api_key"] == "******"
    assert detail.json()["values"]["jev_model"] == "jev-1.14.0"
    assert DUMMY_API_KEY not in detail.text
    # 掩码只作用于读取投影，保存值本身仍是可用凭据
    assert manager.config.jev_api_key == DUMMY_API_KEY
    assert manager.config.jev_base_url == SAVED_BASE_URL


@pytest.mark.parametrize(
    ("field_name", "rejected_value"),
    [
        ("jev_request_timeout_seconds", 0),
        ("jev_request_timeout_seconds", -1.5),
        ("jev_request_timeout_seconds", "inf"),
        ("jev_request_timeout_seconds", "nan"),
        ("jev_http_timeout_seconds", 0),
        ("jev_http_timeout_seconds", "inf"),
        ("jev_model", "   "),
        ("jev_model", ""),
    ],
)
@pytest.mark.asyncio
async def test_jev_field_writes_reject_invalid_values(
    app: App,
    manager: PersistingDecisionConfigManager,
    field_name: str,
    rejected_value: object,
) -> None:
    """AC：非法截止与空模型在校验层被拒绝，配置保持原样。"""
    original = manager.config.model_dump()

    async with app.test_server(asgi=cast("Any", _build_app(manager))) as ctx:
        client = ctx.get_client()
        response = await client.patch(
            f"{API_PREFIX}/resources/{RESOURCE_ID}/fields/{field_name}",
            json={"value": rejected_value},
            headers=_write_headers(f"jev-invalid-{field_name}"),
        )

    assert response.status_code == 422
    assert manager.config.model_dump() == original


@pytest.mark.parametrize(
    ("field_name", "rejected_value", "canary"),
    [
        (
            "jev_base_url",
            "https://jev-user:tsk181-key-canary@jev.example.invalid",
            "tsk181-key-canary",
        ),
        (
            "jev_base_url",
            "https://jev.example.invalid?token=tsk181-query-canary",
            "tsk181-query-canary",
        ),
        (
            "jev_base_url",
            "https://jev.example.invalid#tsk181-fragment-canary",
            "tsk181-fragment-canary",
        ),
        ("jev_base_url", "/tsk181-relative-canary", "tsk181-relative-canary"),
        ("jev_base_url", "ftp://tsk181-scheme-canary.invalid", "tsk181-scheme"),
        (
            "jev_base_url",
            "https://jev.example.invalid:tsk181-port-canary",
            "tsk181-port-canary",
        ),
        (
            "jev_base_url",
            "https://jev.example.invalid/tsk181\r\nX-Injected: canary",
            "tsk181",
        ),
        (
            "jev_base_url",
            "https://jev example.invalid/tsk181-space-canary",
            "tsk181-space-canary",
        ),
        ("jev_api_key", "tsk181 key with space", "tsk181 key"),
        ("jev_api_key", "tsk181-ключ-canary", "tsk181-ключ-canary"),
        ("jev_api_key", "tsk181-control\x07canary", "tsk181-control"),
    ],
)
@pytest.mark.asyncio
async def test_jev_field_writes_reject_malformed_connection_values(
    app: App,
    manager: PersistingDecisionConfigManager,
    field_name: str,
    rejected_value: str,
    canary: str,
) -> None:
    """AC：畸形地址/凭据在写入边界被拒，422 与读取投影都不回显秘密 canary。"""
    original = manager.config.model_dump()
    audit_events: list[object] = []

    async with app.test_server(
        asgi=cast("Any", _build_app(manager, audit_events))
    ) as ctx:
        client = ctx.get_client()
        response = await client.patch(
            f"{API_PREFIX}/resources/{RESOURCE_ID}/fields/{field_name}",
            json={"value": rejected_value},
            headers=_write_headers(f"jev-malformed-{field_name}"),
        )
        detail = await client.get(
            f"{API_PREFIX}/resources/{RESOURCE_ID}", headers=READ_HEADERS
        )

    assert response.status_code == 422
    rejection = response.json()["detail"]
    assert field_name in rejection  # 规则信息保留字段名
    assert canary not in rejection
    assert "input_value" not in rejection
    assert canary not in response.text
    assert detail.status_code == 200
    assert canary not in detail.text
    assert canary not in json.dumps(
        [cast("Any", event).to_dict() for event in audit_events], ensure_ascii=False
    )
    assert manager.config.model_dump() == original


@pytest.mark.asyncio
async def test_saved_config_drives_real_sdk_request(
    app: App, manager: PersistingDecisionConfigManager
) -> None:
    """AC：保存后的统一配置直接驱动 SDK 请求（地址、模型、凭据、Choice+Noul）。"""
    async with app.test_server(asgi=cast("Any", _build_app(manager))) as ctx:
        client = ctx.get_client()
        for field_name, value in (
            ("jev_base_url", SAVED_BASE_URL),
            ("jev_api_key", DUMMY_API_KEY),
            ("jev_model", "jev-1.14.0"),
        ):
            written = await client.patch(
                f"{API_PREFIX}/resources/{RESOURCE_ID}/fields/{field_name}",
                json={"value": value},
                headers=_write_headers(f"jev-saved-{field_name}"),
            )
            assert written.status_code == 200

    transport = ScriptedTypesafeTransport(system_one_payload)
    async with configured_jev_request(
        manager.config, transport=transport
    ) as sdk_client:
        answer = await sdk_client.system_one(
            state=state_payload(),
            questions={"chat": CHOICE_QUESTION, "summary": NOUL_QUESTION},
        )

    assert len(transport.requests) == 1
    sent = transport.requests[0]
    assert str(sent.url) == f"{SAVED_BASE_URL}/v1/systemone"
    assert sent.headers["authorization"] == f"Bearer {DUMMY_API_KEY}"
    assert transport.request_json()["model"] == "jev-1.14.0"
    assert answer.choices["chat"].choice in {"reply", "silent"}
    assert 0.0 <= answer.nouls["summary"].noul <= 1.0
    assert transport.closed is True
