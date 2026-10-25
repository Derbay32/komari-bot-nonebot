"""TSK-182 验收 —— Jev 判定 Prompt 经既有强类型管理接口读写。

经真实管理 API seam（``register_prompt_api`` + ``ManagedPromptResource`` +
``prompt_storage`` 校验/合并逻辑）验证新增 ``komari_decision`` Prompt 资源：

- 资源暴露且仅暴露四个正文字段（``role_description`` / ``reply_eligibility`` /
  ``retention_purpose`` / ``summary_recognition``）；
- PUT 一次修订覆盖全部四字段，PATCH 只改目标字段并保留其余字段；
- 过期/缺失/弱 ``If-Match`` 按既有规则被拒（409/422）且不改写已存储数据；
- 未知字段、缺失字段与空白正文由既有校验规则拒绝（422）；
- 既有的 ``prompt:read`` / ``prompt:write`` 权限分离在真实 HTTP 调用下生效。

测试缝只替换 ``prompt_storage.get_prompt_storage``（持久化外部依赖）：字段
白名单、完整性校验、正文预算、revision CAS 调用与路由错误映射全部走真实
生产代码。
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import nonebot
import pytest
from fastapi import FastAPI

from komari_bot.config import prompt_storage as storage_module
from komari_bot.config.prompt_storage import StoredPrompt
from komari_bot.plugins.komari_management.managed_resources import (
    ManagedPromptResource,
)
from komari_bot.plugins.komari_management.prompt_api import (
    API_PREFIX,
    register_prompt_api,
)

if TYPE_CHECKING:
    from nonebug import App

RESOURCE_ID = "komari_decision"
DISPLAY_NAME = "Komari Decision Prompt"
TABLE_NAME = "komari_prompt_komari_decision"

#: 契约规定的四个正文字段（来自 TSK-182 规格，不来自实现）。
CONTRACT_FIELDS = (
    "role_description",
    "reply_eligibility",
    "retention_purpose",
    "summary_recognition",
)

READER_TOKEN = "prompt-reader-token-000000"
WRITER_TOKEN = "prompt-writer-token-000000"


@dataclass
class _PromptStore:
    """单行 Prompt 表的最小持久化状态：正文快照与 revision。"""

    values: dict[str, str]
    revision: int = 1


class _FakePromptStorage:
    """存储缝替身：只模拟单行表读与 revision CAS，不做任何正文校验。

    字段白名单、完整性、正文预算与错误映射全部由真实生产代码承担；本替身
    仅在被要求写入时比对 revision 后保存，因此任何越权字段都无法在此被
    悄悄接受。
    """

    def __init__(self, store: _PromptStore) -> None:
        self.store = store

    def register_invalidator(self, _resource_id: str, _callback: object) -> None:
        return None

    def _to_stored(self) -> StoredPrompt:
        return StoredPrompt(
            resource_id=RESOURCE_ID,
            prompt_data=dict(self.store.values),
            revision=self.store.revision,
            updated_at=datetime.now(UTC),
        )

    async def fetch_async(self, _resource_id: str) -> StoredPrompt | None:
        return self._to_stored()

    async def update_if_unchanged_async(self, **_kwargs: object) -> None:
        # 管理路由的自动同步视作冲突：不写库，由调用方重读
        return None

    async def replace_if_revision_async(
        self,
        *,
        resource_id: str,
        prompt_data: dict[str, str],
        expected_revision: int,
    ) -> StoredPrompt | None:
        del resource_id
        if expected_revision != self.store.revision:
            return None
        self.store.values = dict(prompt_data)
        self.store.revision += 1
        return self._to_stored()

    async def update_field_if_revision_async(
        self,
        *,
        resource_id: str,
        field_name: str,
        value: str,
        expected_revision: int,
    ) -> StoredPrompt | None:
        del resource_id
        if expected_revision != self.store.revision:
            return None
        self.store.values[field_name] = value
        self.store.revision += 1
        return self._to_stored()

    async def upsert_async(
        self,
        *,
        resource_id: str,
        prompt_data: dict[str, str],
    ) -> StoredPrompt:
        del resource_id
        self.store.values = dict(prompt_data)
        self.store.revision += 1
        return self._to_stored()


def _marker_values() -> dict[str, str]:
    return {field: f"marker-{field}" for field in CONTRACT_FIELDS}


def _read_headers(token: str = READER_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _write_headers(request_id: str, revision: int | str | None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {WRITER_TOKEN}",
        "X-Komari-Change-Reason": "验证判定提示词变更",
        "X-Request-ID": request_id,
    }
    if revision is not None:
        headers["If-Match"] = f'"{revision}"'
    return headers


def _build_app(
    monkeypatch: pytest.MonkeyPatch,
    store: _PromptStore,
) -> FastAPI:
    storage = _FakePromptStorage(store)
    monkeypatch.setattr(storage_module, "get_prompt_storage", lambda: storage)

    async def _record_audit(_event: object) -> None:
        return None

    api_app = FastAPI()
    register_prompt_api(
        api_app,
        api_token=[
            {
                "credential_id": "prompt-reader",
                "token": READER_TOKEN,
                "permissions": ["prompt:read"],
            },
            {
                "credential_id": "prompt-writer",
                "token": WRITER_TOKEN,
                "permissions": ["prompt:write"],
            },
        ],
        allowed_origins=["https://ui.example.com"],
        resources=(
            ManagedPromptResource(
                resource_id=RESOURCE_ID,
                display_name=DISPLAY_NAME,
            ),
        ),
        audit_recorder=_record_audit,
    )
    return api_app


@pytest.fixture
def store() -> _PromptStore:
    return _PromptStore(values=_marker_values())


@pytest.mark.asyncio
async def test_decision_prompt_exposes_exactly_four_contract_fields(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    store: _PromptStore,
) -> None:
    """AC：资源列出且仅列出四个正文字段，详情返回全部正文与 revision。"""
    async with app.test_server(asgi=cast("Any", _build_app(monkeypatch, store))) as ctx:
        client = ctx.get_client()
        listed = await client.get(f"{API_PREFIX}/resources", headers=_read_headers())
        detail = await client.get(
            f"{API_PREFIX}/resources/{RESOURCE_ID}", headers=_read_headers()
        )

    assert listed.status_code == 200
    items = listed.json()["items"]
    assert [item["resource_id"] for item in items] == [RESOURCE_ID]
    item = items[0]
    assert item["display_name"] == DISPLAY_NAME
    assert item["fields"] == sorted(CONTRACT_FIELDS)
    assert item["file_path"] is None
    assert item["config_source"] == f"postgresql:{TABLE_NAME}:{RESOURCE_ID}"

    assert detail.status_code == 200
    payload = detail.json()
    assert payload["values"] == _marker_values()
    assert sorted(payload["values"]) == sorted(CONTRACT_FIELDS)
    assert payload["revision"] == 1


@pytest.mark.asyncio
async def test_decision_prompt_replace_covers_all_four_fields_in_one_revision(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    store: _PromptStore,
) -> None:
    """AC：PUT 用一次修订写入四个字段，revision 前进一位。"""
    replacement = {field: f"replaced-{field}" for field in CONTRACT_FIELDS}

    async with app.test_server(asgi=cast("Any", _build_app(monkeypatch, store))) as ctx:
        client = ctx.get_client()
        replaced = await client.put(
            f"{API_PREFIX}/resources/{RESOURCE_ID}",
            json=replacement,
            headers=_write_headers("prompt-replace", 1),
        )
        detail = await client.get(
            f"{API_PREFIX}/resources/{RESOURCE_ID}", headers=_read_headers()
        )

    assert replaced.status_code == 200
    assert replaced.json()["values"] == replacement
    assert replaced.json()["revision"] == 2
    assert store.values == replacement
    assert detail.json()["values"] == replacement


@pytest.mark.asyncio
async def test_decision_prompt_partial_patch_retains_untouched_fields(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    store: _PromptStore,
) -> None:
    """AC：PATCH 单字段只改目标字段，其余三个字段原样保留。"""
    async with app.test_server(asgi=cast("Any", _build_app(monkeypatch, store))) as ctx:
        client = ctx.get_client()
        patched = await client.patch(
            f"{API_PREFIX}/resources/{RESOURCE_ID}/fields/retention_purpose",
            json={"value": "新的保留用途说明"},
            headers=_write_headers("prompt-field-update", 1),
        )

    assert patched.status_code == 200
    values = patched.json()["values"]
    assert values["retention_purpose"] == "新的保留用途说明"
    for field in CONTRACT_FIELDS:
        if field == "retention_purpose":
            continue
        assert values[field] == f"marker-{field}", field
    assert patched.json()["revision"] == 2
    assert store.values["retention_purpose"] == "新的保留用途说明"


@pytest.mark.parametrize(
    ("if_match", "expected_status"),
    [
        ("1", 409),  # 过期 revision：真实 revision 已前进
        (None, 422),  # 缺少强 If-Match
        ('W/"2"', 422),  # 弱 ETag
        ('"not-a-revision"', 422),
    ],
)
@pytest.mark.asyncio
async def test_decision_prompt_writes_reject_stale_or_missing_revision(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    store: _PromptStore,
    if_match: str | None,
    expected_status: int,
) -> None:
    """AC：过期/缺失/非法 If-Match 按既有规则拒绝，且不改写已存储正文。"""
    store.revision = 2
    original = dict(store.values)

    async with app.test_server(asgi=cast("Any", _build_app(monkeypatch, store))) as ctx:
        client = ctx.get_client()
        headers = _write_headers("prompt-revision-reject", if_match)
        rejected_patch = await client.patch(
            f"{API_PREFIX}/resources/{RESOURCE_ID}/fields/role_description",
            json={"value": "不会写入"},
            headers=headers,
        )
        rejected_put = await client.put(
            f"{API_PREFIX}/resources/{RESOURCE_ID}",
            json=dict.fromkeys(CONTRACT_FIELDS, "不会写入"),
            headers=headers,
        )

    assert rejected_patch.status_code == expected_status
    assert rejected_put.status_code == expected_status
    assert store.values == original
    assert store.revision == 2


@pytest.mark.asyncio
async def test_decision_prompt_writes_reject_unknown_missing_and_blank_values(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    store: _PromptStore,
) -> None:
    """AC：未知字段、缺失字段与空白正文由既有规则拒绝，不写库。"""
    original = dict(store.values)

    async with app.test_server(asgi=cast("Any", _build_app(monkeypatch, store))) as ctx:
        client = ctx.get_client()
        unknown_put = await client.put(
            f"{API_PREFIX}/resources/{RESOURCE_ID}",
            json={**_marker_values(), "system_prompt": "越权字段"},
            headers=_write_headers("prompt-unknown-field", 1),
        )
        incomplete_put = await client.put(
            f"{API_PREFIX}/resources/{RESOURCE_ID}",
            json={
                field: f"only-{field}"
                for field in CONTRACT_FIELDS
                if field != "summary_recognition"
            },
            headers=_write_headers("prompt-incomplete", 1),
        )
        blank_patch = await client.patch(
            f"{API_PREFIX}/resources/{RESOURCE_ID}/fields/role_description",
            json={"value": "   "},
            headers=_write_headers("prompt-blank-field", 1),
        )
        cross_resource_patch = await client.patch(
            f"{API_PREFIX}/resources/{RESOURCE_ID}/fields/system_prompt",
            json={"value": "越权字段"},
            headers=_write_headers("prompt-cross-resource", 1),
        )

    assert unknown_put.status_code == 422
    assert incomplete_put.status_code == 422
    assert blank_patch.status_code == 422
    assert cross_resource_patch.status_code == 404
    assert store.values == original
    assert store.revision == 1


@pytest.mark.asyncio
async def test_decision_prompt_separates_read_and_write_permissions(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    store: _PromptStore,
) -> None:
    """AC：prompt:read 不授予 prompt:write，真实 HTTP 调用体现权限边界。

    既有权限蕴含方向是单向的（``prompt:write`` 蕴含 ``prompt:read``，反之
    不成立），因此读凭据不能写入、写凭据可读可写。
    """
    async with app.test_server(asgi=cast("Any", _build_app(monkeypatch, store))) as ctx:
        client = ctx.get_client()
        anonymous = await client.get(f"{API_PREFIX}/resources/{RESOURCE_ID}")
        reader_get = await client.get(
            f"{API_PREFIX}/resources/{RESOURCE_ID}", headers=_read_headers()
        )
        reader_put = await client.put(
            f"{API_PREFIX}/resources/{RESOURCE_ID}",
            json=dict.fromkeys(CONTRACT_FIELDS, "reader-写入"),
            headers={
                **_write_headers("prompt-reader-put", 1),
                "Authorization": f"Bearer {READER_TOKEN}",
            },
        )
        reader_patch = await client.patch(
            f"{API_PREFIX}/resources/{RESOURCE_ID}/fields/role_description",
            json={"value": "reader-写入"},
            headers={
                **_write_headers("prompt-reader-patch", 1),
                "Authorization": f"Bearer {READER_TOKEN}",
            },
        )
        writer_get = await client.get(
            f"{API_PREFIX}/resources/{RESOURCE_ID}",
            headers=_read_headers(WRITER_TOKEN),
        )
        writer_patch = await client.patch(
            f"{API_PREFIX}/resources/{RESOURCE_ID}/fields/summary_recognition",
            json={"value": "writer-写入"},
            headers=_write_headers("prompt-writer-patch", 1),
        )

    assert anonymous.status_code == 401
    assert reader_get.status_code == 200
    assert reader_put.status_code == 403
    assert reader_patch.status_code == 403
    # 既有单向蕴含：prompt:write 可读；prompt:read 不可写
    assert writer_get.status_code == 200
    assert writer_patch.status_code == 200
    assert writer_patch.json()["values"]["summary_recognition"] == "writer-写入"
    assert store.values["role_description"] == "marker-role_description"


# ---------------------------------------------------------------------------
# 生产装配：真实 komari_management/__init__.py 必须注册第四个 Prompt 资源。
#
# 测试套件把 ``komari_bot.plugins.komari_management`` 桩化（模块体不执行），
# 因此这里按既有集成测试的做法加载真实文件，取得真实
# ``_load_management_components`` 并检查 Prompt 资源装配。
# ---------------------------------------------------------------------------

_MGMT_DIR = (
    Path(__file__).resolve().parents[2] / "komari_bot" / "plugins" / "komari_management"
)
_REAL_MANAGEMENT_INIT = _MGMT_DIR / "__init__.py"

_SHIM_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "komari_knowledge": ("register_knowledge_api", "get_engine"),
    "agent_run_logger": ("register_agent_run_log_api", "get_agent_run_log_reader"),
    "user_ban": ("register_user_ban_api", "get_service"),
    "komari_chat": ("get_reply_fulfillment_ops_service",),
    "character_binding": (
        "register_character_binding_repair_api",
        "get_binding_repair_service",
    ),
    "komari_roulette": (
        "register_roulette_management_api",
        "get_roulette_observation",
    ),
}


class _RecordingDriver:
    def __init__(self) -> None:
        self.startup_hooks: list[object] = []
        self.shutdown_hooks: list[object] = []

    def on_startup(self, func: object) -> object:
        self.startup_hooks.append(func)
        return func

    def on_shutdown(self, func: object) -> object:
        self.shutdown_hooks.append(func)
        return func


def _stub_symbol(*_args: object, **_kwargs: object) -> None:
    return None


@pytest.fixture
def real_management_loader(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """执行真实 ``komari_management/__init__.py`` 并交出真实组件加载器。"""
    monkeypatch.setattr(nonebot, "get_driver", lambda: _RecordingDriver())
    for plugin_name, symbols in _SHIM_DEPENDENCIES.items():
        module = sys.modules.get(f"komari_bot.plugins.{plugin_name}")
        if module is None:
            continue
        for symbol in symbols:
            monkeypatch.setattr(module, symbol, _stub_symbol, raising=False)

    spec = importlib.util.spec_from_file_location(
        "komari_bot.plugins.komari_management",
        _REAL_MANAGEMENT_INIT,
        submodule_search_locations=[str(_MGMT_DIR)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "komari_bot.plugins.komari_management"
    spec.loader.exec_module(module)

    # 套件把 ``require`` 桩成白名单，不认识的插件名会抛错；这里只作声明记录。
    monkeypatch.setattr(module, "require", _stub_symbol)
    return SimpleNamespace(load_components=module._load_management_components)


def test_management_registers_decision_prompt_resource(
    real_management_loader: SimpleNamespace,
) -> None:
    """AC：真实管理装配把 komari_decision 注册为第四个 Prompt 资源。"""
    components = real_management_loader.load_components()

    prompt_resources = {
        resource.resource_id: resource for resource in components.prompt_resources
    }
    assert RESOURCE_ID in prompt_resources
    assert prompt_resources[RESOURCE_ID].display_name == DISPLAY_NAME
    assert set(prompt_resources) == {
        "komari_chat",
        "group_history_summary",
        "komari_decision",
    }
