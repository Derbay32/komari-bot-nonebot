"""TSK-197 验收 5 —— 管理 API 只暴露新契约配置字段（无数据库）。

经真实管理 API seam（``register_config_api`` + ``komari_chat`` 强类型
Schema + ``StaticConfigManager``）验证：

- ``komari_chat`` 配置资源暴露的字段集合只含新契约：回复 Agent 预算
  （``agent_max_rounds`` / ``agent_max_tool_calls_per_round`` /
  ``agent_max_total_tool_calls``）、工具调用约束模式（``agent_tool_call_mode``）、
  图片理解模式（``image_understanding_mode``）与迁移后的 8 项图片下载预算
  （``vision_image_download_*``）；
- 已删除的旧字段（``vision_tool_enabled``、旧 ``output_instruction`` 聊天
  Prompt 字段、``reply_commit_*`` 旧 outbox 语言、死字段
  ``proactive_score_threshold``）不再出现；
- 新字段以 ``immediate`` 生效且非秘密（与 Schema 元数据一致）。

复用既有管理路由测试的 seam（``_StaticConfigManager`` / ``_build_app``
风格），不断言私有 helper。字段集合从强类型 Schema 派生。
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
from tests.komari_management.test_management_config_api_routes import (
    _StaticConfigManager,
)

if TYPE_CHECKING:
    from nonebug import App

#: TSK-197 后 komari_chat 配置资源的唯一合法字段集合（新契约）。
NEW_CHAT_CONFIG_FIELDS: set[str] = {
    "agent_max_rounds",
    "agent_max_tool_calls_per_round",
    "agent_max_total_tool_calls",
    "agent_tool_call_mode",
    "image_understanding_mode",
    "vision_image_download_max_count",
    "vision_image_download_max_bytes",
    "vision_image_download_total_max_bytes",
    "vision_image_download_max_pixels",
    "vision_image_download_concurrency",
    "vision_image_download_connect_timeout_seconds",
    "vision_image_download_read_timeout_seconds",
    "vision_image_download_total_timeout_seconds",
}

#: 协调式破坏升级后必须从管理面消失的旧字段。
REMOVED_CHAT_CONFIG_FIELDS: set[str] = {
    "vision_tool_enabled",  # 旧图片开关（TSK-194 已删）
    "output_instruction",  # 旧聊天 Prompt 输出协议（TSK-190 已删）
    "reply_commit_worker_interval_seconds",  # 0011 改名
    "reply_commit_batch_size",  # 0011 改名
    "reply_commit_lease_seconds",  # 0011 改名
    "reply_commit_max_attempts",  # 0011 改名
    "reply_commit_retry_base_seconds",  # 0011 改名
    "reply_commit_tombstone_retention_days",  # 0011 改名
    "proactive_score_threshold",  # 死字段（0004 随批删除）
}


def _build_chat_config_app(schema: type[Any]) -> FastAPI:
    """用真实 komari_chat Schema 注册配置资源（与既有路由测试同 seam）。"""
    manager = _StaticConfigManager(schema())

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
                resource_id="komari_chat",
                display_name="Komari Chat",
                manager_getter=lambda: manager,
            ),
        ),
        audit_recorder=_null_audit_recorder,
    )
    return api_app


async def _null_audit_recorder(event: object) -> None:
    del event


@pytest.mark.asyncio
async def test_config_api_exposes_only_new_chat_contract_fields(
    app: App,
) -> None:
    """AC5：管理 API 的 komari_chat 资源只暴露新字段，不再出现旧字段。"""
    schema = ensure_typed_config_model("komari_chat")
    assert schema is not None, "komari_chat 强类型配置 Schema 必须可加载"

    async with app.test_server(
        asgi=cast("Any", _build_chat_config_app(schema)),
    ) as ctx:
        client = ctx.get_client()
        headers = {"Authorization": "Bearer secret-token-00000000"}
        listed = await client.get(f"{API_PREFIX}/resources", headers=headers)
        detail = await client.get(
            f"{API_PREFIX}/resources/komari_chat", headers=headers
        )

    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    exposed = set(listed.json()["items"][0]["fields"])
    assert exposed >= NEW_CHAT_CONFIG_FIELDS, (
        f"管理 API 必须暴露新契约字段，缺失: {sorted(NEW_CHAT_CONFIG_FIELDS - exposed)}"
    )
    assert not (REMOVED_CHAT_CONFIG_FIELDS & exposed), (
        f"管理 API 不得暴露已删除旧字段: {sorted(REMOVED_CHAT_CONFIG_FIELDS & exposed)}"
    )

    assert detail.status_code == 200
    values = detail.json()["values"]
    assert not (REMOVED_CHAT_CONFIG_FIELDS & set(values)), (
        "资源详情值不得包含已删除旧字段"
    )
    # 新字段以数据库默认值出现在详情中
    assert values["agent_tool_call_mode"] == "required"
    assert values["image_understanding_mode"] == "delegated"
    assert values["agent_max_rounds"] == 10
    assert values["agent_max_tool_calls_per_round"] == 4
    assert values["agent_max_total_tool_calls"] == 20
    assert values["vision_image_download_max_count"] == 4
    assert values["vision_image_download_total_timeout_seconds"] == 45.0

    # 新字段生效语义：immediate 且非秘密
    field_metadata = detail.json()["field_metadata"]
    for field in sorted(NEW_CHAT_CONFIG_FIELDS):
        meta = field_metadata[field]
        assert meta["apply_mode"] == "immediate", field
        assert meta["secret"] is False, field
