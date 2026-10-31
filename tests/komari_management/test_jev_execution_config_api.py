"""TSK-182 验收 —— Jev 执行配置经既有统一设置接口读写。

经真实管理 API seam（``register_config_api`` + ``komari_decision`` 强类型
Schema + 配置管理器测试替身）验证新增 ``jev_execution`` 分区：

- 六个执行字段（``max_concurrent_decisions`` / ``max_queued_messages_per_group``
  / ``queue_timeout_seconds`` / ``reference_timeout_seconds`` /
  ``optional_reply_probability`` / ``decision_log_retention_days``）以契约默认值
  出现在读取响应中，并归属新的 ``jev_execution`` 分区（位于 ``jev_request``
  之后）；
- 保留字段（``bot_aliases``、五个 Jev 请求字段）取值不变；TSK-190 退役的
  38 个场景/总结字段与 ADR-0018 删除的停用开关不再出现在读取投影中；
- 非法容量、非法截止（零/负/非有限）、越界概率与越界保留天数在写入边界被
  拒绝（422），配置保持原样。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI

from komari_bot.config.typed_config import ensure_typed_config_model
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
from tests.komari_management.test_jev_config_api import (
    PersistingDecisionConfigManager,
)

if TYPE_CHECKING:
    from nonebug import App

    from komari_bot.plugins.komari_decision.config_schema import (
        KomariDecisionConfigSchema,
    )

RESOURCE_ID = "komari_decision"
READ_HEADERS = {"Authorization": "Bearer secret-token-00000000"}

#: 新增执行字段的契约默认值（来自 TSK-182 规格，不来自实现）。
JEV_EXECUTION_DEFAULTS: dict[str, object] = {
    "max_concurrent_decisions": 4,
    "max_queued_messages_per_group": 5,
    "queue_timeout_seconds": 3.0,
    "reference_timeout_seconds": 3.0,
    "optional_reply_probability": 0.5,
    "decision_log_retention_days": 7,
}


@pytest.fixture
def manager() -> PersistingDecisionConfigManager:
    """以真实强类型 Schema 默认值构造配置管理器替身。"""
    schema_type = ensure_typed_config_model(RESOURCE_ID)
    assert schema_type is not None, "komari_decision 强类型配置 Schema 必须可加载"
    return PersistingDecisionConfigManager(
        cast("type[KomariDecisionConfigSchema]", schema_type)()
    )


def _write_headers(request_id: str) -> dict[str, str]:
    return {
        "Authorization": "Bearer secret-token-00000000",
        "X-Komari-Change-Reason": "验证 Jev 执行配置变更",
        "X-Request-ID": request_id,
    }


def _build_app(manager: PersistingDecisionConfigManager) -> FastAPI:
    async def _record_audit(_event: object) -> None:
        return None

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


@pytest.mark.asyncio
async def test_jev_execution_defaults_are_exposed_through_management_get(
    app: App, manager: PersistingDecisionConfigManager
) -> None:
    """AC：六个执行字段以契约默认值出现在读取响应，并归属 jev_execution 分区。"""
    async with app.test_server(asgi=cast("Any", _build_app(manager))) as ctx:
        client = ctx.get_client()
        detail = await client.get(
            f"{API_PREFIX}/resources/{RESOURCE_ID}", headers=READ_HEADERS
        )

    assert detail.status_code == 200
    payload = detail.json()
    values = payload["values"]
    for field_name, expected in JEV_EXECUTION_DEFAULTS.items():
        assert values[field_name] == expected, field_name

    for field_name in JEV_EXECUTION_DEFAULTS:
        assert payload["field_metadata"][field_name] == {
            "secret": False,
            "apply_mode": "immediate",
            "section_id": "jev_execution",
        }, field_name
        assert payload["field_states"][field_name]["apply_mode"] == "immediate"
        assert payload["field_states"][field_name]["restart_required"] is False

    section_ids = [section["section_id"] for section in payload["sections"]]
    assert "jev_execution" in section_ids
    assert section_ids.index("jev_execution") > section_ids.index("jev_request")
    jev_execution_section = next(
        section
        for section in payload["sections"]
        if section["section_id"] == "jev_execution"
    )
    assert jev_execution_section["display_name"]


@pytest.mark.asyncio
async def test_jev_execution_defaults_keep_surviving_decision_fields(
    app: App, manager: PersistingDecisionConfigManager
) -> None:
    """AC：保留字段取值不变，退役字段不再出现在读取投影中。"""
    async with app.test_server(asgi=cast("Any", _build_app(manager))) as ctx:
        client = ctx.get_client()
        detail = await client.get(
            f"{API_PREFIX}/resources/{RESOURCE_ID}", headers=READ_HEADERS
        )

    assert detail.status_code == 200
    values = detail.json()["values"]
    assert "plugin_enable" not in values
    assert values["bot_aliases"] == ["小鞠", "小鞠知花", "komari"]
    assert values["jev_model"] == "jev-1.14.0"
    assert values["jev_http_timeout_seconds"] == 3.0
    assert values["jev_request_timeout_seconds"] == 3.0
    assert values["jev_api_key"] == "******"
    for retired in RETIRED_DECISION_CONFIG_FIELDS:
        assert retired not in values, retired


@pytest.mark.asyncio
async def test_jev_execution_field_writes_persist_through_management_patch(
    app: App, manager: PersistingDecisionConfigManager
) -> None:
    """AC：六个执行字段经既有字段写入接口保存并在读取响应中生效。"""
    written_values: dict[str, object] = {
        "max_concurrent_decisions": 2,
        "max_queued_messages_per_group": 8,
        "queue_timeout_seconds": 1.5,
        "reference_timeout_seconds": 2.5,
        "optional_reply_probability": 0.25,
        "decision_log_retention_days": 14,
    }

    async with app.test_server(asgi=cast("Any", _build_app(manager))) as ctx:
        client = ctx.get_client()
        for field_name, value in written_values.items():
            response = await client.patch(
                f"{API_PREFIX}/resources/{RESOURCE_ID}/fields/{field_name}",
                json={"value": value},
                headers=_write_headers(f"jev-execution-{field_name}"),
            )
            assert response.status_code == 200, field_name
            assert response.json()["values"][field_name] == value

        detail = await client.get(
            f"{API_PREFIX}/resources/{RESOURCE_ID}", headers=READ_HEADERS
        )

    assert detail.status_code == 200
    for field_name, value in written_values.items():
        assert detail.json()["values"][field_name] == value, field_name
    # 写入只影响被改字段，其他保留字段默认值原样保留
    assert "plugin_enable" not in detail.json()["values"]
    assert detail.json()["values"]["jev_request_timeout_seconds"] == 3.0


@pytest.mark.parametrize(
    ("field_name", "accepted_value", "rejected_value"),
    [
        ("max_concurrent_decisions", 1, 0),
        ("max_concurrent_decisions", 6, -1),
        ("max_queued_messages_per_group", 5, 0),
        ("max_queued_messages_per_group", 9, -5),
        ("queue_timeout_seconds", 0.25, 0.0),
        ("queue_timeout_seconds", 4.5, -1.5),
        ("queue_timeout_seconds", 4.5, "inf"),
        ("queue_timeout_seconds", 4.5, "nan"),
        ("reference_timeout_seconds", 2.25, 0),
        ("reference_timeout_seconds", 2.25, -0.5),
        ("reference_timeout_seconds", 2.25, "inf"),
        ("reference_timeout_seconds", 2.25, "nan"),
        ("optional_reply_probability", 0.0, -0.01),
        ("optional_reply_probability", 1.0, 1.01),
        ("optional_reply_probability", 0.75, "inf"),
        ("optional_reply_probability", 0.75, "nan"),
        ("decision_log_retention_days", 1, 0),
        ("decision_log_retention_days", 30, 31),
        ("decision_log_retention_days", 7, -1),
    ],
)
@pytest.mark.asyncio
async def test_jev_execution_field_writes_reject_out_of_range_values(
    app: App,
    manager: PersistingDecisionConfigManager,
    field_name: str,
    accepted_value: object,
    rejected_value: object,
) -> None:
    """AC：容量、截止、概率与保留天数越界值被拒绝且不落库。

    先写入一个边界内的合法值（证明该字段可寻址、被拒不是因字段缺失），再
    尝试越界值：必须 422，且已保存的值与配置快照都不变。
    """
    async with app.test_server(asgi=cast("Any", _build_app(manager))) as ctx:
        client = ctx.get_client()
        accepted = await client.patch(
            f"{API_PREFIX}/resources/{RESOURCE_ID}/fields/{field_name}",
            json={"value": accepted_value},
            headers=_write_headers(f"jev-execution-accept-{field_name}"),
        )
        assert accepted.status_code == 200, field_name
        persisted = manager.config.model_dump()
        response = await client.patch(
            f"{API_PREFIX}/resources/{RESOURCE_ID}/fields/{field_name}",
            json={"value": rejected_value},
            headers=_write_headers(f"jev-execution-invalid-{field_name}"),
        )

    assert response.status_code == 422
    assert field_name in response.json()["detail"]
    assert manager.config.model_dump() == persisted
