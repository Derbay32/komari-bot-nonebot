"""Komari Management Prompt 接口路由测试。

TSK-191：管理 API 对三个 Prompt 资源继续支持完整读取、替换、局部更新与
revision CAS（AC7）；字段白名单与完整性校验由 resource_id 对应强类型
Schema 决定，管理资源不再携带 Python 默认正文（AC2/AC3/AC8）。

测试缝只替换 ``prompt_storage.get_prompt_storage``（存储对象），路由层与
``prompt_storage`` 的字段校验/合并逻辑全部走真实生产代码。管理资源一律
经 ``make_managed_prompt_resource(resource_id, display_name)`` 按无
defaults 形态构造 —— 旧实现把 ``defaults`` 当作必填字段，因此本文件的
资源构造处整体是 TSK-191 的可解释 RED；实现后，字段集合来自 Schema、
PATCH/PUT 白名单来自 Schema 与跨资源字段拒绝用例负责钉住新契约。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest
from fastapi import FastAPI

from komari_bot.config import prompt_storage as storage_module
from komari_bot.config.prompt_storage import StoredPrompt
from komari_bot.plugins.komari_management.prompt_api import (
    API_PREFIX,
    register_prompt_api,
)
from tests.config.prompt_field_contract import (
    CROSS_RESOURCE_FOREIGN_FIELDS,
    PROMPT_RESOURCE_IDS,
    make_managed_prompt_resource,
    prompt_display_name,
    prompt_marker_values,
    prompt_resource_field_names,
    prompt_table_name,
)

if TYPE_CHECKING:
    from nonebug import App

    from komari_bot.management.management_audit import ManagementAuditEvent


@dataclass
class _PromptStore:
    values: dict[str, str]
    revision: int = 1


def _write_headers(request_id: str, revision: int) -> dict[str, str]:
    return {
        "Authorization": "Bearer secret-token-00000000",
        "X-Komari-Change-Reason": "验证提示词变更",
        "X-Request-ID": request_id,
        "If-Match": f'"{revision}"',
    }


def _read_headers() -> dict[str, str]:
    return {"Authorization": "Bearer secret-token-00000000"}


class _FakePromptStorage:
    """存储缝替身：只模拟单行表读写与 revision CAS，不做正文合并。

    字段白名单/完整性校验按强类型 Schema 执行（TSK-191 契约）。
    """

    def __init__(self, stores: dict[str, _PromptStore]) -> None:
        self._stores = stores

    def register_invalidator(self, _resource_id: str, _callback: object) -> None:
        return

    def _to_stored(self, resource_id: str) -> StoredPrompt:
        store = self._stores[resource_id]
        return StoredPrompt(
            resource_id=resource_id,
            prompt_data=dict(store.values),
            revision=store.revision,
            updated_at=datetime.now(UTC),
        )

    @staticmethod
    def _validate_write(resource_id: str, prompt_data: dict[str, str]) -> None:
        fields = prompt_resource_field_names(resource_id)
        unknown = sorted(set(prompt_data) - fields)
        if unknown:
            raise ValueError(f"存在未知提示词字段: {', '.join(unknown)}")  # noqa: TRY003
        missing = sorted(fields - set(prompt_data))
        if missing:
            raise ValueError(f"提示词写入缺少字段: {', '.join(missing)}")  # noqa: TRY003
        for name, value in prompt_data.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"提示词字段 {name} 必须是非空字符串")  # noqa: TRY003

    async def fetch_async(self, resource_id: str) -> StoredPrompt | None:
        return self._to_stored(resource_id)

    async def update_if_unchanged_async(self, **_kwargs: object) -> None:
        # 自动同步视为冲突：不写库，由调用方重读
        return None

    async def replace_if_revision_async(
        self,
        *,
        resource_id: str,
        prompt_data: dict[str, str],
        expected_revision: int,
    ) -> StoredPrompt | None:
        store = self._stores[resource_id]
        if expected_revision != store.revision:
            return None
        self._validate_write(resource_id, prompt_data)
        store.values = dict(prompt_data)
        store.revision += 1
        return self._to_stored(resource_id)

    async def update_field_if_revision_async(
        self,
        *,
        resource_id: str,
        field_name: str,
        value: str,
        expected_revision: int,
    ) -> StoredPrompt | None:
        store = self._stores[resource_id]
        if expected_revision != store.revision:
            return None
        if field_name not in prompt_resource_field_names(resource_id):
            raise ValueError(f"存在未知提示词字段: {field_name}")  # noqa: TRY003
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"提示词字段 {field_name} 必须是非空字符串")  # noqa: TRY003
        store.values[field_name] = value
        store.revision += 1
        return self._to_stored(resource_id)

    async def upsert_async(
        self,
        *,
        resource_id: str,
        prompt_data: dict[str, str],
    ) -> StoredPrompt:
        self._validate_write(resource_id, prompt_data)
        store = self._stores[resource_id]
        store.values = dict(prompt_data)
        store.revision += 1
        return self._to_stored(resource_id)


def _build_app(
    monkeypatch: pytest.MonkeyPatch,
    store: _PromptStore | None = None,
    *,
    stores: dict[str, _PromptStore] | None = None,
    audit_events: list[ManagementAuditEvent] | None = None,
    resource_ids: tuple[str, ...] = ("komari_chat",),
) -> FastAPI:
    if stores is None:
        assert store is not None, "必须提供 store 或 stores"
        stores = {resource_ids[0]: store}

    storage = _FakePromptStorage(stores)
    monkeypatch.setattr(storage_module, "get_prompt_storage", lambda: storage)

    async def _record_audit(event: ManagementAuditEvent) -> None:
        if audit_events is not None:
            audit_events.append(event)

    api_app = FastAPI()
    register_prompt_api(
        api_app,
        api_token=[
            {
                "credential_id": "prompt-api-operator",
                "token": "secret-token-00000000",
                "permissions": ["*"],
            }
        ],
        allowed_origins=["https://ui.example.com"],
        resources=tuple(
            make_managed_prompt_resource(
                resource_id,
                prompt_display_name(resource_id),
            )
            for resource_id in resource_ids
        ),
        audit_recorder=_record_audit,
    )
    return api_app


def _chat_store() -> _PromptStore:
    values = prompt_marker_values("komari_chat")
    values["system_prompt"] = "你好"
    values["memory_ack"] = "收到"
    return _PromptStore(values=values)


@pytest.mark.asyncio
async def test_prompt_routes_require_token_and_list_resources(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _chat_store()

    async with app.test_server(asgi=cast("Any", _build_app(monkeypatch, store))) as ctx:
        client = ctx.get_client()
        unauthorized = await client.get(f"{API_PREFIX}/resources")
        listed = await client.get(
            f"{API_PREFIX}/resources",
            headers=_read_headers(),
        )

    assert unauthorized.status_code == 401
    assert listed.status_code == 200
    assert listed.json()["items"][0]["resource_id"] == "komari_chat"
    assert listed.json()["items"][0]["file_path"] is None
    assert listed.json()["items"][0]["storage_key"] == "komari_chat"
    assert (
        listed.json()["items"][0]["config_source"]
        == "postgresql:komari_prompt_komari_chat:komari_chat"
    )


@pytest.mark.asyncio
async def test_prompt_routes_support_detail_replace_and_field_update(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _chat_store()

    async with app.test_server(asgi=cast("Any", _build_app(monkeypatch, store))) as ctx:
        client = ctx.get_client()
        detail = await client.get(
            f"{API_PREFIX}/resources/komari_chat", headers=_read_headers()
        )
        updated = await client.patch(
            f"{API_PREFIX}/resources/komari_chat/fields/system_prompt",
            json={"value": "新的系统提示词"},
            headers=_write_headers("prompt-field-update", 1),
        )
        replaced = await client.put(
            f"{API_PREFIX}/resources/komari_chat",
            json={
                "system_prompt": "完整替换",
                "memory_ack": "也替换",
                **{
                    field: f"填充-{field}"
                    for field in sorted(prompt_resource_field_names("komari_chat"))
                    if field not in {"system_prompt", "memory_ack"}
                },
            },
            headers=_write_headers("prompt-replace", 2),
        )

    assert detail.status_code == 200
    assert detail.json()["values"]["system_prompt"] == "你好"
    assert detail.json()["revision"] == 1
    assert (
        detail.json()["config_source"]
        == "postgresql:komari_prompt_komari_chat:komari_chat"
    )
    assert updated.status_code == 200
    assert updated.json()["values"]["system_prompt"] == "新的系统提示词"
    assert replaced.status_code == 200
    assert replaced.json()["values"]["memory_ack"] == "也替换"


@pytest.mark.asyncio
async def test_prompt_routes_report_validation_and_not_found(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _chat_store()

    async with app.test_server(asgi=cast("Any", _build_app(monkeypatch, store))) as ctx:
        client = ctx.get_client()
        missing_resource = await client.get(
            f"{API_PREFIX}/resources/missing",
            headers=_read_headers(),
        )
        missing_field = await client.patch(
            f"{API_PREFIX}/resources/komari_chat/fields/missing_field",
            json={"value": "anything"},
            headers=_write_headers("prompt-missing-field", 1),
        )
        invalid_replace = await client.put(
            f"{API_PREFIX}/resources/komari_chat",
            json={"unknown": "anything"},
            headers=_write_headers("prompt-invalid-replace", 1),
        )

    assert missing_resource.status_code == 404
    assert missing_field.status_code == 404
    assert invalid_replace.status_code == 422


@pytest.mark.asyncio
async def test_prompt_writes_require_matching_revision(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _chat_store()

    async with app.test_server(asgi=cast("Any", _build_app(monkeypatch, store))) as ctx:
        client = ctx.get_client()
        missing_header = await client.patch(
            f"{API_PREFIX}/resources/komari_chat/fields/system_prompt",
            json={"value": "不会写入"},
            headers={
                "Authorization": "Bearer secret-token-00000000",
                "X-Komari-Change-Reason": "验证缺少修订号",
            },
        )
        stale_revision = await client.patch(
            f"{API_PREFIX}/resources/komari_chat/fields/system_prompt",
            json={"value": "也不会写入"},
            headers=_write_headers("prompt-stale-revision", 0),
        )

    assert missing_header.status_code == 422
    assert stale_revision.status_code == 409
    assert store.values["system_prompt"] == "你好"
    assert store.revision == 1


@pytest.mark.asyncio
async def test_prompt_routes_expose_chat_behavior_fields_and_hide_removed_field(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC8：管理 API 暴露新增行为字段，不再暴露已删除的 output_instruction。"""
    from tests.config.chat_prompt_field_contract import (
        REMOVED_FIELD,
        REQUIRED_EXACT_FIELDS,
    )

    store = _chat_store()

    async with app.test_server(
        asgi=cast("Any", _build_app(monkeypatch, store)),
    ) as ctx:
        client = ctx.get_client()
        headers = _read_headers()
        listed = await client.get(f"{API_PREFIX}/resources", headers=headers)
        detail = await client.get(
            f"{API_PREFIX}/resources/komari_chat", headers=headers
        )

    fields = set(listed.json()["items"][0]["fields"])
    assert REMOVED_FIELD not in fields
    for field in REQUIRED_EXACT_FIELDS:
        assert field in fields, f"管理 API 必须暴露新字段 {field}"
    assert detail.status_code == 200
    assert REMOVED_FIELD not in detail.json()["values"]
    assert "tool_call_instruction" in detail.json()["values"]


@pytest.mark.asyncio
async def test_prompt_routes_update_new_field_and_reject_removed_field(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC8：可更新新增字段；已删除字段不可寻址（404）。"""
    from tests.config.chat_prompt_field_contract import REMOVED_FIELD

    store = _chat_store()

    async with app.test_server(
        asgi=cast("Any", _build_app(monkeypatch, store)),
    ) as ctx:
        client = ctx.get_client()
        updated = await client.patch(
            f"{API_PREFIX}/resources/komari_chat/fields/tool_call_instruction",
            json={"value": "新的工具调用行为指引"},
            headers=_write_headers("prompt-tool-call-update", store.revision),
        )
        removed = await client.patch(
            f"{API_PREFIX}/resources/komari_chat/fields/{REMOVED_FIELD}",
            json={"value": "不会写入"},
            headers=_write_headers("prompt-removed-field", store.revision + 1),
        )

    assert updated.status_code == 200
    assert updated.json()["values"]["tool_call_instruction"] == "新的工具调用行为指引"
    assert removed.status_code == 404
    assert store.values["tool_call_instruction"] == "新的工具调用行为指引"


def _all_resource_stores() -> dict[str, _PromptStore]:
    return {
        resource_id: _PromptStore(values=prompt_marker_values(resource_id))
        for resource_id in PROMPT_RESOURCE_IDS
    }


@pytest.mark.asyncio
async def test_prompt_routes_cover_all_three_resources_full_workflow(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC7/AC9：三个 Prompt 资源均支持完整读取、替换、局部更新与 CAS。"""
    stores = _all_resource_stores()

    async with app.test_server(
        asgi=cast(
            "Any",
            _build_app(
                monkeypatch,
                stores=stores,
                resource_ids=PROMPT_RESOURCE_IDS,
            ),
        )
    ) as ctx:
        client = ctx.get_client()
        listed = await client.get(f"{API_PREFIX}/resources", headers=_read_headers())

        listed_items = {item["resource_id"]: item for item in listed.json()["items"]}
        assert set(listed_items) == set(PROMPT_RESOURCE_IDS)

        for resource_id in PROMPT_RESOURCE_IDS:
            fields = sorted(prompt_resource_field_names(resource_id))
            item = listed_items[resource_id]
            assert item["fields"] == fields, (
                f"{resource_id} 管理字段集必须等于强类型 Schema 字段集"
            )
            assert item["file_path"] is None, (
                f"{resource_id} 不得暴露运行时文件回读（file_path 必须为 null）"
            )
            assert item["config_source"] == (
                f"postgresql:{prompt_table_name(resource_id)}:{resource_id}"
            )

            detail = await client.get(
                f"{API_PREFIX}/resources/{resource_id}",
                headers=_read_headers(),
            )
            assert detail.status_code == 200
            assert detail.json()["values"][fields[0]] == f"marker-{fields[0]}"
            assert detail.json()["revision"] == 1

            updated = await client.patch(
                f"{API_PREFIX}/resources/{resource_id}/fields/{fields[0]}",
                json={"value": f"p-{fields[0]}"},
                headers=_write_headers(f"{resource_id}-field-update", 1),
            )
            assert updated.status_code == 200
            assert updated.json()["values"][fields[0]] == f"p-{fields[0]}"
            assert updated.json()["revision"] == 2

            stale_patch = await client.patch(
                f"{API_PREFIX}/resources/{resource_id}/fields/{fields[0]}",
                json={"value": "不写入"},
                headers=_write_headers(f"{resource_id}-stale-patch", 1),
            )
            assert stale_patch.status_code == 409

            replace_payload = {
                field: f"put-{field}" for field in fields
            }
            replaced = await client.put(
                f"{API_PREFIX}/resources/{resource_id}",
                json=replace_payload,
                headers=_write_headers(f"{resource_id}-replace", 2),
            )
            assert replaced.status_code == 200
            assert replaced.json()["values"] == replace_payload
            assert replaced.json()["revision"] == 3

            stale_put = await client.put(
                f"{API_PREFIX}/resources/{resource_id}",
                json=replace_payload,
                headers=_write_headers(f"{resource_id}-stale-put", 1),
            )
            assert stale_put.status_code == 409

            no_match = await client.patch(
                f"{API_PREFIX}/resources/{resource_id}/fields/{fields[0]}",
                json={"value": "不会写入"},
                headers={
                    "Authorization": "Bearer secret-token-00000000",
                    "X-Komari-Change-Reason": "验证缺少修订号",
                },
            )
            assert no_match.status_code == 422


@pytest.mark.asyncio
async def test_prompt_replace_rejects_incomplete_payload(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC7：PUT 必须完整校验字段集合（缺字段 422，不靠默认正文回填）。

    当前实现用 defaults 回填缺失字段（``validate_prompt_values`` 从默认
    字典补齐），缺字段的 PUT 仍被当作完整载荷写入并返回 200，因此本用例
    是 TSK-191 的可解释 RED。
    """
    stores = _all_resource_stores()

    async with app.test_server(
        asgi=cast(
            "Any",
            _build_app(
                monkeypatch,
                stores=stores,
                resource_ids=PROMPT_RESOURCE_IDS,
            ),
        )
    ) as ctx:
        client = ctx.get_client()
        for resource_id in PROMPT_RESOURCE_IDS:
            fields = sorted(prompt_resource_field_names(resource_id))
            response = await client.put(
                f"{API_PREFIX}/resources/{resource_id}",
                json={field: f"put-{field}" for field in fields[:-1]},
                headers=_write_headers(f"{resource_id}-incomplete-put", 1),
            )
            assert response.status_code == 422, (
                f"{resource_id} PUT 必须完整校验字段集合"
                "（缺字段不得由 Python 默认正文回填）"
            )


@pytest.mark.asyncio
async def test_prompt_list_exposes_schema_fields(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3：管理资源不携带 defaults 时，字段集仍完整来自强类型 Schema。

    实现不得再从 ``resource.defaults`` 推导字段；本用例在资源构造契约
    落地后继续钉住列表字段集 == resource_id 对应 Schema 字段集。
    """
    stores = _all_resource_stores()

    async with app.test_server(
        asgi=cast(
            "Any",
            _build_app(
                monkeypatch,
                stores=stores,
                resource_ids=PROMPT_RESOURCE_IDS,
            ),
        )
    ) as ctx:
        client = ctx.get_client()
        listed = await client.get(f"{API_PREFIX}/resources", headers=_read_headers())

    items = {item["resource_id"]: item for item in listed.json()["items"]}
    for resource_id in PROMPT_RESOURCE_IDS:
        expected = sorted(prompt_resource_field_names(resource_id))
        assert items[resource_id]["fields"] == expected, (
            f"{resource_id} 管理字段集必须来自强类型 Schema，而非 defaults"
        )


@pytest.mark.asyncio
async def test_prompt_patch_accepts_schema_field(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3：无 defaults 资源下 PATCH 仍按 Schema 白名单寻址字段。

    实现不得按 ``resource.defaults`` 判断字段是否存在——白名单必须来自
    resource_id 对应强类型 Schema。
    """
    stores = _all_resource_stores()

    async with app.test_server(
        asgi=cast(
            "Any",
            _build_app(
                monkeypatch,
                stores=stores,
                resource_ids=PROMPT_RESOURCE_IDS,
            ),
        )
    ) as ctx:
        client = ctx.get_client()
        for resource_id in PROMPT_RESOURCE_IDS:
            field = sorted(prompt_resource_field_names(resource_id))[0]
            response = await client.patch(
                f"{API_PREFIX}/resources/{resource_id}/fields/{field}",
                json={"value": "schema-白名单更新"},
                headers=_write_headers(f"{resource_id}-schema-patch", 1),
            )
            assert response.status_code == 200, (
                f"{resource_id} 的 Schema 字段 {field} 必须可寻址"
                "（白名单来自强类型 Schema，而非 defaults）"
            )


@pytest.mark.asyncio
async def test_prompt_replace_accepts_full_schema_payload(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3：无 defaults 资源下 PUT 完整 Schema 载荷通过校验并写入。

    实现不得以 defaults 键集做白名单——字段集必须来自 resource_id 对应
    强类型 Schema。
    """
    stores = _all_resource_stores()

    async with app.test_server(
        asgi=cast(
            "Any",
            _build_app(
                monkeypatch,
                stores=stores,
                resource_ids=PROMPT_RESOURCE_IDS,
            ),
        )
    ) as ctx:
        client = ctx.get_client()
        for resource_id in PROMPT_RESOURCE_IDS:
            payload = prompt_marker_values(resource_id)
            response = await client.put(
                f"{API_PREFIX}/resources/{resource_id}",
                json=payload,
                headers=_write_headers(f"{resource_id}-schema-put", 1),
            )
            assert response.status_code == 200, (
                f"{resource_id} 完整 Schema 字段载荷必须通过 PUT 校验"
                "（白名单来自强类型 Schema，而非 defaults）"
            )


@pytest.mark.parametrize(
    ("resource_id", "foreign_field"),
    sorted(CROSS_RESOURCE_FOREIGN_FIELDS.items()),
)
@pytest.mark.asyncio
async def test_prompt_replace_rejects_cross_resource_field(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    resource_id: str,
    foreign_field: str,
) -> None:
    """AC3(TSK-191)：A 资源字段注入 B 资源 PUT 必须 422 且不写库。

    白名单是 resource_id 对应 Schema；三资源字段的全局 union 会误收，
    因此本用例是 TSK-191 的可解释 RED。
    """
    stores = _all_resource_stores()

    async with app.test_server(
        asgi=cast(
            "Any",
            _build_app(
                monkeypatch,
                stores=stores,
                resource_ids=PROMPT_RESOURCE_IDS,
            ),
        )
    ) as ctx:
        client = ctx.get_client()
        payload = prompt_marker_values(resource_id)
        payload[foreign_field] = "跨资源字段值"
        response = await client.put(
            f"{API_PREFIX}/resources/{resource_id}",
            json=payload,
            headers=_write_headers(f"{resource_id}-cross-put", 1),
        )

    assert response.status_code == 422
    assert stores[resource_id].revision == 1, "拒绝的载荷不得修改 revision"


@pytest.mark.parametrize(
    ("resource_id", "foreign_field"),
    sorted(CROSS_RESOURCE_FOREIGN_FIELDS.items()),
)
@pytest.mark.asyncio
async def test_prompt_field_address_rejects_cross_resource_field(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    resource_id: str,
    foreign_field: str,
) -> None:
    """AC3(TSK-191)：跨资源字段不可通过 PATCH 寻址（404），且不写库。"""
    stores = _all_resource_stores()

    async with app.test_server(
        asgi=cast(
            "Any",
            _build_app(
                monkeypatch,
                stores=stores,
                resource_ids=PROMPT_RESOURCE_IDS,
            ),
        )
    ) as ctx:
        client = ctx.get_client()
        response = await client.patch(
            f"{API_PREFIX}/resources/{resource_id}/fields/{foreign_field}",
            json={"value": "不会写入"},
            headers=_write_headers(f"{resource_id}-cross-patch", 1),
        )

    assert response.status_code == 404
    assert stores[resource_id].revision == 1
