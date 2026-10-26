"""Komari Management 与 NoneBot FastAPI 驱动集成测试。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import nonebot
import pytest
from pydantic import BaseModel

from komari_bot.plugins.character_binding.management_api import (
    register_character_binding_repair_api,
)
from komari_bot.plugins.group_admission import register_group_admission_api
from komari_bot.plugins.komari_decision import register_decision_api
from komari_bot.plugins.komari_help.api import register_help_api
from komari_bot.plugins.komari_knowledge.api import register_knowledge_api
from komari_bot.plugins.komari_management.api_runtime import (
    ManagementApiComponents,
    register_management_api_for_driver,
)
from komari_bot.plugins.komari_management.managed_resources import (
    ManagedConfigResource,
)
from komari_bot.plugins.komari_search.api import register_search_api
from komari_bot.plugins.user_ban.api import register_user_ban_api
from tests.config.prompt_field_contract import (
    make_managed_prompt_resource,
    prompt_display_name,
)

if TYPE_CHECKING:
    from nonebug import App


class _FakeLogger:
    def __init__(self) -> None:
        self.info_messages: list[str] = []
        self.warning_messages: list[str] = []

    def info(self, message: str, *args: object) -> None:
        self.info_messages.append(message % args if args else message)

    def warning(self, message: str, *args: object) -> None:
        self.warning_messages.append(message % args if args else message)


class _DummyConfigModel(BaseModel):
    plugin_enable: bool = True


class _DummyConfigManager:
    @property
    def config_source(self) -> str:
        return "postgres:komari_plugin_configs/komari_management"

    async def get_async(self) -> BaseModel:
        return _DummyConfigModel()

    async def update_field_async(self, field_name: str, value: object) -> BaseModel:
        del field_name, value
        return await self.get_async()

    async def reload_async(self) -> BaseModel:
        return await self.get_async()


def _noop_roulette_registrar(*_args: object, **_kwargs: object) -> None:
    """Dummy roulette management registrar for this integration fixture."""


def _noop_roulette_observation_getter() -> None:
    """Dummy roulette observation getter for this integration fixture."""


def _build_components() -> ManagementApiComponents:
    return ManagementApiComponents(
        register_group_admission_api=register_group_admission_api,
        register_knowledge_api=register_knowledge_api,
        knowledge_engine_getter=lambda: None,
        register_help_api=register_help_api,
        help_engine_getter=lambda: None,
        register_search_api=register_search_api,
        register_user_ban_api=register_user_ban_api,
        user_ban_service_getter=lambda: None,
        reply_fulfillment_service_getter=lambda: None,
        register_character_binding_repair_api=register_character_binding_repair_api,
        character_binding_repair_service_getter=lambda: None,
        register_roulette_management_api=_noop_roulette_registrar,
        roulette_observation_getter=_noop_roulette_observation_getter,
        register_decision_api=register_decision_api,
        config_resources=(
            ManagedConfigResource(
                resource_id="komari_management",
                display_name="Komari Management",
                manager_getter=lambda: _DummyConfigManager(),
            ),
        ),
        prompt_resources=(
            make_managed_prompt_resource(
                "komari_chat",
                prompt_display_name("komari_chat"),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_nonebot_fastapi_driver_exposes_docs_and_management_routes(
    app: App,
) -> None:
    driver = nonebot.get_driver()
    logger = _FakeLogger()

    registered = register_management_api_for_driver(
        driver=driver,
        config=SimpleNamespace(
            plugin_enable=True,
            api_credentials=[
                {
                    "credential_id": "test-operator",
                    "token": "secret-token-00000000",
                    "permissions": ["*"],
                }
            ],
            api_allowed_origins=[],
        ),
        component_loader=_build_components,
        logger=logger,
    )

    assert registered is True

    async with app.test_server() as ctx:
        client = ctx.get_client()
        docs = await client.get("/api/docs")
        schema_response = await client.get("/api/openapi.json")

    assert docs.status_code == 200
    assert schema_response.status_code == 200

    schema = schema_response.json()
    assert "/api/v2/komari-knowledge/knowledge" in schema["paths"]
    assert "/api/v2/komari-help/help" in schema["paths"]
    assert "/api/v2/komari-memory/conversations" not in schema["paths"]
    assert "/api/v2/agent-run-logs/runs" not in schema["paths"]
    assert "/api/v2/komari-search/provider-descriptors" in schema["paths"]
    assert "/api/v2/komari-management-config/resources" in schema["paths"]
    assert "/api/v2/komari-management-prompt/resources" in schema["paths"]
    assert "/api/v2/komari-user-bans/bans" in schema["paths"]
    assert "/api/v2/reply-fulfillments/fulfillments" in schema["paths"]
    assert "/api/v2/character-bindings/repair/diagnose" in schema["paths"]
    assert "/api/llm-provider/v1/reply-logs" not in schema["paths"]
    tag_names = {
        tag
        for operations in schema["paths"].values()
        for operation in operations.values()
        for tag in operation.get("tags", [])
    }
    assert {
        "komari-knowledge",
        "komari-help",
        "komari-search",
        "komari-management-config",
        "komari-management-prompt",
        "komari-user-bans",
        "reply-fulfillments",
        "character-binding-repair",
    } <= tag_names
    assert "komari-memory" not in tag_names


# ---------------------------------------------------------------------------
# TSK-279 C2: the *production* loader must carry the real roulette symbols.
#
# The suite shims ``komari_bot.plugins.komari_management`` (``__file__`` is
# ``None``) so its module body cannot be imported normally.  The fixture below
# deliberately loads the real file so the assertions exercise the actual
# ``_load_management_components`` wiring instead of a hand-built component
# object.  It does **not** pin the ``from import`` shape.
# ---------------------------------------------------------------------------

_REAL_MANAGEMENT_INIT = (
    Path(__file__).resolve().parents[2]
    / "komari_bot"
    / "plugins"
    / "komari_management"
    / "__init__.py"
)

#: Symbols the production loader reads off plugin tops the suite shims.
_LOADER_DEPENDENCY_SYMBOLS: dict[str, tuple[str, ...]] = {
    "komari_knowledge": ("register_knowledge_api", "get_engine"),
    "user_ban": ("register_user_ban_api", "get_service"),
    "komari_chat": ("get_reply_fulfillment_ops_service",),
    "character_binding": (
        "register_character_binding_repair_api",
        "get_binding_repair_service",
    ),
}


def _loader_registrar_stub(*_args: object, **_kwargs: object) -> None:
    """Stand-in registrar for plugins the loader only stores, never calls here."""


def _loader_getter_stub() -> None:
    """Stand-in getter for plugins the loader only stores, never calls here."""


class _RecordingDriver:
    """Fake NoneBot driver that only records lifecycle-hook registration."""

    def __init__(self) -> None:
        self.startup_hooks: list[object] = []
        self.shutdown_hooks: list[object] = []

    def on_startup(self, func: object) -> object:
        self.startup_hooks.append(func)
        return func

    def on_shutdown(self, func: object) -> object:
        self.shutdown_hooks.append(func)
        return func


@pytest.fixture
def real_management_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    """Execute the production ``komari_management/__init__.py`` loader.

    Patches a recording driver and the plugin-top symbols the loader reads, then
    loads the real module file and hands back the *production*
    ``_load_management_components`` plus the recorded ``require`` declarations.
    """

    fake_driver = _RecordingDriver()
    monkeypatch.setattr(nonebot, "get_driver", lambda: fake_driver)

    for plugin_name, symbols in _LOADER_DEPENDENCY_SYMBOLS.items():
        module = sys.modules.get(f"komari_bot.plugins.{plugin_name}")
        if module is None:
            continue
        for symbol in symbols:
            stub: object = (
                _loader_registrar_stub
                if symbol.startswith("register_")
                else _loader_getter_stub
            )
            monkeypatch.setattr(module, symbol, stub, raising=False)

    spec = importlib.util.spec_from_file_location(
        "komari_bot.plugins.komari_management",
        _REAL_MANAGEMENT_INIT,
        submodule_search_locations=[str(_REAL_MANAGEMENT_INIT.parent)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "komari_bot.plugins.komari_management"
    spec.loader.exec_module(module)

    required_plugins: list[str] = []

    # ``nonebot.plugin.require`` is globally stubbed by the suite to a whitelist
    # that has no ``komari_help`` / ``komari_roulette`` entry.  The production
    # loader only uses ``require`` as a load declaration (its return value is
    # discarded), so record every name and stub the loading itself; the real
    # symbols the loader reads below still come from the real package tops.
    def _require(name: str) -> None:
        required_plugins.append(name)

    monkeypatch.setattr(module, "require", _require)
    return SimpleNamespace(
        load_components=module._load_management_components,
        required_plugins=required_plugins,
    )


def test_production_loader_has_no_agent_run_logger_components(
    real_management_loader: SimpleNamespace,
) -> None:
    """TSK-200：真实 loader 不再装配 Agent Run 路由、读取器或配置资源。"""

    components = real_management_loader.load_components()

    assert "agent_run_logger" not in real_management_loader.required_plugins
    resource_ids = {resource.resource_id for resource in components.config_resources}
    assert "agent_run_logger" not in resource_ids
    fields = components.__dataclass_fields__
    assert "register_agent_run_log_api" not in fields
    assert "agent_run_log_reader_getter" not in fields


def test_production_loader_carries_real_roulette_symbols(
    real_management_loader: SimpleNamespace,
) -> None:
    """真实 loader 必须把真实 roulette 两符号传入管理组件（行为证据）。"""

    import komari_bot.plugins.komari_roulette as roulette_plugin

    components = real_management_loader.load_components()

    assert "komari_roulette" in real_management_loader.required_plugins
    assert "komari_memory" not in real_management_loader.required_plugins
    assert (
        components.register_roulette_management_api
        is roulette_plugin.register_roulette_management_api
    )
    assert (
        components.roulette_observation_getter
        is roulette_plugin.get_roulette_observation
    )


# ---------------------------------------------------------------------------
# TSK-186: the *production* loader must carry the real decision registrar.
#
# The suite shims ``komari_bot.plugins.komari_management`` (``__file__`` is
# ``None``), so its module body cannot be imported normally.  The assertions
# below therefore execute the real file through ``real_management_loader`` and
# check the actual ``_load_management_components`` wiring, not a hand-built
# component object.
# ---------------------------------------------------------------------------


def test_production_loader_carries_real_decision_registrar(
    real_management_loader: SimpleNamespace,
) -> None:
    """真实 loader 必须把真实顶层 decision registrar 传入管理组件（行为证据）。"""

    import komari_bot.plugins.komari_decision as decision_plugin

    components = real_management_loader.load_components()

    assert "komari_decision" in real_management_loader.required_plugins
    assert components.register_decision_api is decision_plugin.register_decision_api
